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

Only the image-generation path is fully real in this phase — ideation
(needs DuckDuckGo/Google Places/Pinecone RAG) and roadmap (needs Pinecone
RAG) are each their own later spec, since every new external dependency
needs its own approval."""

from datetime import timedelta
from typing import Literal, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from openai import AsyncOpenAI
from pydantic import BaseModel
from temporalio.common import RetryPolicy

from ...core.config import settings
from ...core.observability import bind_cofounder_context
from . import queries

COFOUNDER_GRAPH = "cofounder-graph"

_NODE_TIMEOUT = timedelta(seconds=60)
_NODE_RETRY = RetryPolicy(initial_interval=timedelta(seconds=1), maximum_attempts=3)

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
    return "image" if node == "image_generation_agent" else "not_available"


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


async def _not_available(state: CofounderGraphState) -> dict:
    return {
        "reply": (
            "I can only generate images so far in this early version — "
            "ideation and roadmap planning are coming in a later phase."
        )
    }


def build_cofounder_graph() -> StateGraph:
    g = StateGraph(CofounderGraphState)
    node_opts = {
        "execute_in": "activity",
        "start_to_close_timeout": _NODE_TIMEOUT,
        "retry_policy": _NODE_RETRY,
    }
    g.add_node("route", _route, metadata=node_opts)
    g.add_node("image_generation_agent", _image_generation_agent, metadata=node_opts)
    g.add_node("not_available", _not_available, metadata=node_opts)
    g.add_edge(START, "route")
    g.add_conditional_edges(
        "route", _route_conditional, {"image": "image_generation_agent", "not_available": "not_available"}
    )
    g.add_edge("image_generation_agent", END)
    g.add_edge("not_available", END)
    return g
