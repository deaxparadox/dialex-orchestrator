"""LangGraph StateGraph for the cofounder agent's routing (spec 0045,
ported from the original `entrepreneur_graph`). Registered with Temporal's
LangGraph plugin so each node runs as a real Activity — same mechanism
`dialex/consultations/graphs.py` already uses.

Verified against the original source (`ai/graphs/enterpreneur_graph.py`):
the router only ever routes to 3 real destinations (`entrepreneur_ideation_agent`,
`entrepreneur_roadmap_agent`, `image_generation_agent`) — two other nodes
in the original (`entrepreneur_chat_node`, `entrepreneur_overview_agent`)
are wired into the graph but never reachable from the router's conditional
edges, dead code, not ported.

Ideation (spec 0046) is fully real too now — a create_react_agent bound to
2 honest Bubble.io placeholders + a fully-ported market-research tool.
Roadmap still needs Pinecone RAG — its own later spec, since every new
external dependency needs its own approval."""

from datetime import timedelta
from typing import Literal, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import create_react_agent
from openai import AsyncOpenAI
from pydantic import BaseModel
from temporalio.common import RetryPolicy

from ...core.config import settings
from ...core.observability import bind_cofounder_context
from . import queries
from .tools.bubble_placeholders import get_bubble_entreprenurs, get_bubble_freelancers_v2
from .tools.market_research import market_research_tool

COFOUNDER_GRAPH = "cofounder-graph"

_NODE_TIMEOUT = timedelta(seconds=60)
_NODE_RETRY = RetryPolicy(initial_interval=timedelta(seconds=1), maximum_attempts=3)
# The ideation node runs a real ReAct tool-calling loop (3 search queries +
# concurrent page fetches with their own retry/backoff, per
# tools/market_research.py) — the 60s default is too tight for that
# (spec 0046).
_IDEATION_NODE_TIMEOUT = timedelta(seconds=180)

# Ported from ai/prompts/entrepreneur_router_prompt.py's routing rules —
# only the JSON-schema-return instruction is dropped, since
# .with_structured_output() enforces the schema at the LLM-call boundary
# instead of asking nicely and manually parsing the result (see spec 0045).
_ROUTER_SYSTEM_PROMPT = """You are the Entrepreneurial Router Agent.

Your job is just to decide whether a user's query should go to:
1. The Startup Ideation Agent (node: entrepreneur_ideation_agent) — handles
   general user query, brainstorming, idea validation, and open-ended
   entrepreneurial discussions.
2. The Startup Roadmap Agent (node: entrepreneur_roadmap_agent) — handles
   generating structured startup build-up strategies (like 7-step
   roadmaps).
3. The Image generation agent (node: image_generation_agent) — handles
   image or logo generation for a startup, or any image.

Don't answer the user's query yourself — just choose which agent should
process it.

Routing rules:
- If the user asks for a roadmap, plan, 7-step strategy, or execution
  roadmap, route to entrepreneur_roadmap_agent.
- If the user asks for an image, logo, or visual, route to
  image_generation_agent.
- If the user seems to be brainstorming, validating ideas, asking
  open-ended questions, or seeking guidance, route to
  entrepreneur_ideation_agent.
- If unclear, default to entrepreneur_ideation_agent."""


class CofounderGraphState(TypedDict):
    session_id: int
    turns: list[dict]  # already includes the current user message (spec 0044's convention)
    router_response: dict | None
    reply: str | None
    image_id: int | None  # a CofounderGeneratedImage row id, never the raw bytes (see below)


class RouterDecision(BaseModel):
    intent: str
    recommended_node: Literal[
        "entrepreneur_ideation_agent", "entrepreneur_roadmap_agent", "image_generation_agent"
    ]
    reasoning: str


def _transcript(turns: list[dict]) -> str:
    return "\n".join(f"{t['speaker']}: {t['content']}" for t in turns)


async def _route(state: CofounderGraphState) -> dict:
    bind_cofounder_context(cofounder_session_id=state["session_id"])
    llm = ChatOpenAI(model="gpt-4o-mini", api_key=settings.openai_api_key)
    decision: RouterDecision = await llm.with_structured_output(RouterDecision).ainvoke(
        [
            SystemMessage(_ROUTER_SYSTEM_PROMPT),
            HumanMessage(f"Conversation so far:\n{_transcript(state['turns'])}"),
        ]
    )
    return {"router_response": decision.model_dump()}


async def _route_conditional(state: CofounderGraphState) -> str:
    # Must be async, not sync — LangChain's runnable-coercion falls back to
    # a background-thread executor for sync callables under ainvoke(), and
    # Temporal's workflow sandbox forbids spawning real OS threads (found
    # the hard way in dialex/consultations/graphs.py, spec 0009).
    node = state["router_response"]["recommended_node"]
    if node == "image_generation_agent":
        return "image"
    if node == "entrepreneur_ideation_agent":
        return "ideation"
    return "not_available"


async def _image_generation_agent(state: CofounderGraphState) -> dict:
    """Ported from ai/tools/image_generation.py's image_gen() — a direct
    image-generation call, the user's latest message passed straight through
    as the prompt, no LLM-crafted prompt-refinement step (the original's own
    prompt-refinement import was commented out, unused).

    The original targeted dall-e-3 — verified directly against this
    project's real OpenAI account that it no longer exists there; only the
    newer gpt-image-* family is available (user's choice: the gpt-image-1
    standard tier). That family always returns base64, never a URL
    (verified against the OpenAI Python SDK's own docs) — and a full
    base64 image is far too large for a Temporal Activity result (a real
    `ServerError: Complete result exceeds size limit` hit during this
    spec's own verification, since this whole node runs as one Activity).
    So the image is persisted directly here, in the same Activity, to its
    own table (never CofounderTurn.content, which flows through Temporal's
    turn-history channel on every future turn) — only a small row id
    crosses back through the graph/Workflow."""
    bind_cofounder_context(cofounder_session_id=state["session_id"])
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    latest_message = state["turns"][-1]["content"]
    response = await client.images.generate(
        model="gpt-image-1", prompt=latest_message, n=1, size="1024x1024", output_format="png"
    )
    image_id = await queries.insert_image(state["session_id"], response.data[0].b64_json)
    return {"reply": "Here's what I generated:", "image_id": image_id}


# Ported from ai/prompts/entrepreneur_ideation_prompt.py — the substantive
# guidance is verbatim. Drops the original's "always wrap the answer in
# {type, data} JSON" instruction: verified that envelope is consumed by
# nothing downstream even in the original (the only parse attempt is a bare
# try/except: pass that silently keeps the raw string on any failure), so
# it carries no behavior — a deliberate, minor adaptation (spec 0046).
_IDEATION_SYSTEM_PROMPT = """You are the Startup Ideation Agent — part of The Entrepreneur Lab Virtual Co-Founder system.

Your job is to guide users through brainstorming, validating, and refining startup ideas.
Act like an experienced founder, incubator mentor, and idea strategist.

Core Instructions:
- If the user seems unsure or their intent is unclear, ask clarifying questions before responding.
- Encourage exploration: ask about the user's interests, skills, and the problems they see.
- Validate startup ideas for market fit, problem clarity, and differentiation.
- Respond in plain, conversational text — no JSON, no special formatting required.

Additional Guidelines:
- Keep tone collaborative, insightful, and curious.
- Focus on helping the user think clearly and validate before building.
- Do not generate roadmaps or 7-step plans — those belong to the Startup Roadmap Agent."""


async def _ideation_agent(state: CofounderGraphState) -> dict:
    """Ported from ai/graphs/enterpreneur_graph.py's entrepreneur_ideation_agent
    + ai/llm/openai.py's ideation_llm_chat — a langgraph.prebuilt
    create_react_agent bound to the 3 tools that exist right now (2 honest
    Bubble.io placeholders + the fully-ported market research pipeline).
    The react agent's entire multi-step tool-calling loop runs inside this
    one call, matching the original's own granularity — no special handling
    needed since this whole node already runs as one Temporal Activity."""
    bind_cofounder_context(cofounder_session_id=state["session_id"])
    llm = ChatOpenAI(model="gpt-4o-mini", api_key=settings.openai_api_key)
    agent = create_react_agent(
        llm, tools=[get_bubble_entreprenurs, get_bubble_freelancers_v2, market_research_tool]
    )

    messages = [SystemMessage(_IDEATION_SYSTEM_PROMPT)]
    for turn in state["turns"]:
        messages.append(HumanMessage(turn["content"]) if turn["speaker"] == "user" else AIMessage(turn["content"]))

    result = await agent.ainvoke({"messages": messages})
    return {"reply": result["messages"][-1].content}


async def _not_available(state: CofounderGraphState) -> dict:
    return {
        "reply": "I can't help with roadmap planning yet in this early version — that's coming in a later phase."
    }


def build_cofounder_graph() -> StateGraph:
    g = StateGraph(CofounderGraphState)
    node_opts = {
        "execute_in": "activity",
        "start_to_close_timeout": _NODE_TIMEOUT,
        "retry_policy": _NODE_RETRY,
    }
    ideation_node_opts = {**node_opts, "start_to_close_timeout": _IDEATION_NODE_TIMEOUT}
    g.add_node("route", _route, metadata=node_opts)
    g.add_node("image_generation_agent", _image_generation_agent, metadata=node_opts)
    g.add_node("ideation_agent", _ideation_agent, metadata=ideation_node_opts)
    g.add_node("not_available", _not_available, metadata=node_opts)
    g.add_edge(START, "route")
    g.add_conditional_edges(
        "route",
        _route_conditional,
        {"image": "image_generation_agent", "ideation": "ideation_agent", "not_available": "not_available"},
    )
    g.add_edge("image_generation_agent", END)
    g.add_edge("ideation_agent", END)
    g.add_edge("not_available", END)
    return g
