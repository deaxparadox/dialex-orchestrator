"""LangGraph StateGraph for the cofounder agent's client-driven 7-step
flow (spec 0049, ported from the original `entrepreneur_structured_graph`)
— a second, separate graph from `graphs.py`'s router graph, run via the
same Temporal LangGraph plugin mechanism.

Unlike the router graph, the step isn't decided by an LLM — the caller
picks it directly (`state["step"]`, 1-7), matching the original's
`entrepreneur_conditional_node`. Every step reuses the exact same
`ideation_llm_chat` react agent already ported (`graphs.build_ideation_agent`),
swapping in a step-specific system prompt from `step_prompts.py` — this
matches the original exactly (it also reuses one shared agent instance
across both graphs, only the prompt changes per call).

Persistence deliberately does NOT reintroduce LangGraph's own checkpointer
(the original's `entrepreneur_structured_graph` and `entrepreneur_graph`
share one `AsyncPostgresSaver`, keyed only by a client-supplied `chat_id`
string, with no separation between the two graphs' identically-named
`messages` channel). This port already replaced that mechanism with
Temporal + a plain `CofounderTurn` table (Phases 1a-1e); this phase just
adds a nullable `step` column to that same table. `get_query()`'s
first-message-in-step-vs-continuing decision is reproduced here as a query
over turn history instead of message metadata, scoped to structured turns
only (`step IS NOT NULL`) — free-form and structured turns share a session
on purpose but don't share context, avoiding the original's own
shared-thread collision risk.

One deliberate correction, not a faithful bug-for-bug port: the original's
`get_query()` "continuing the same step" branch sends `[system, *state
messages (already ending with the current turn), current_query]` — the
current user message twice in a row, since `user_query_node` had already
appended it to `messages` before this branch appends it again. That's a
genuine duplication bug in the original, not intentional behavior (nothing
downstream depends on the repeat) — this port sends the turn history
(which already ends with the current turn) exactly once."""

from datetime import timedelta
from typing import TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from temporalio.common import RetryPolicy

from .graphs import build_ideation_agent
from .step_prompts import (
    STEP_1_SYSTEM_PROMPT,
    STEP_2_SYSTEM_PROMPT,
    STEP_3_SYSTEM_PROMPT,
    STEP_4_SYSTEM_PROMPT,
    STEP_5_SYSTEM_PROMPT,
    STEP_6_SYSTEM_PROMPT,
    STEP_7_SYSTEM_PROMPT,
)

COFOUNDER_STRUCTURED_GRAPH = "cofounder-structured-graph"

_STEP_PROMPTS = {
    1: STEP_1_SYSTEM_PROMPT,
    2: STEP_2_SYSTEM_PROMPT,
    3: STEP_3_SYSTEM_PROMPT,
    4: STEP_4_SYSTEM_PROMPT,
    5: STEP_5_SYSTEM_PROMPT,
    6: STEP_6_SYSTEM_PROMPT,
    7: STEP_7_SYSTEM_PROMPT,
}


class StructuredGraphState(TypedDict):
    session_id: int
    turns: list[dict]  # full session history (spec 0044's convention) — includes the current user turn
    step: int
    reply: str | None


def _strip_json_fence(text: str) -> str:
    """Ported from ai/graphs/enterpreneur_graph.py's remove_language_annotation
    — the only thing entrepreneur_structured_graph.py actually calls from
    that module (its `get_messages_history` import is dead code there,
    never invoked, not ported)."""
    return text.replace("```json", "").replace("```", "")


def _build_query(turns: list[dict], step: int, prompt: str) -> list:
    """Reproduces get_query()'s behavior against turn history instead of
    LangGraph message metadata (see module docstring)."""
    structured_turns = [t for t in turns if t.get("step") is not None]
    history = structured_turns[:-1]  # everything before the current turn

    if not history or history[-1]["step"] != step:
        current = structured_turns[-1]
        return [SystemMessage(prompt), HumanMessage(current["content"])]

    return [SystemMessage(prompt)] + [
        HumanMessage(t["content"]) if t["speaker"] == "user" else AIMessage(t["content"])
        for t in structured_turns
    ]


_NODE_NAMES = {n: f"step_{n}" for n in _STEP_PROMPTS}

# Same generous timeout class as the router graph's ideation node
# (graphs.py's _IDEATION_NODE_TIMEOUT) — every step node runs the same
# react-agent tool-calling loop.
_STEP_NODE_TIMEOUT = timedelta(seconds=180)
_STEP_NODE_RETRY = RetryPolicy(initial_interval=timedelta(seconds=1), maximum_attempts=3)


async def _route_to_step(state: StructuredGraphState) -> str:
    # Async, not sync — see graphs.py's _route_conditional comment: LangChain's
    # runnable-coercion falls back to a real OS thread for sync callables
    # under ainvoke(), which Temporal's workflow sandbox forbids.
    return _NODE_NAMES[state["step"]]


async def _run_step(state: StructuredGraphState, prompt: str) -> dict:
    agent = build_ideation_agent()
    query = _build_query(state["turns"], state["step"], prompt)
    result = await agent.ainvoke({"messages": query})
    reply = _strip_json_fence(result["messages"][-1].content)
    return {"reply": reply}


# Temporal's LangGraph plugin derives each node's Activity task id from the
# node function's own qualname and requires it to be a real module-level
# function — a closure-returning factory fails at worker startup with
# "closures/local functions are not supported" (found the hard way here).
# So these 7 near-identical wrappers are a real technical requirement, not
# just fidelity to the original's own 7 separate functions.
async def _step_1(state: StructuredGraphState) -> dict:
    return await _run_step(state, STEP_1_SYSTEM_PROMPT)


async def _step_2(state: StructuredGraphState) -> dict:
    return await _run_step(state, STEP_2_SYSTEM_PROMPT)


async def _step_3(state: StructuredGraphState) -> dict:
    return await _run_step(state, STEP_3_SYSTEM_PROMPT)


async def _step_4(state: StructuredGraphState) -> dict:
    return await _run_step(state, STEP_4_SYSTEM_PROMPT)


async def _step_5(state: StructuredGraphState) -> dict:
    return await _run_step(state, STEP_5_SYSTEM_PROMPT)


async def _step_6(state: StructuredGraphState) -> dict:
    return await _run_step(state, STEP_6_SYSTEM_PROMPT)


async def _step_7(state: StructuredGraphState) -> dict:
    return await _run_step(state, STEP_7_SYSTEM_PROMPT)


_NODE_FUNCS = {1: _step_1, 2: _step_2, 3: _step_3, 4: _step_4, 5: _step_5, 6: _step_6, 7: _step_7}


def build_structured_graph() -> StateGraph:
    g = StateGraph(StructuredGraphState)
    node_opts = {
        "execute_in": "activity",
        "start_to_close_timeout": _STEP_NODE_TIMEOUT,
        "retry_policy": _STEP_NODE_RETRY,
    }
    for n, func in _NODE_FUNCS.items():
        g.add_node(_NODE_NAMES[n], func, metadata=node_opts)
    # path_map is {return value -> node name}; _route_to_step already
    # returns the target node's own name, so this is an identity map.
    g.add_conditional_edges(START, _route_to_step, {name: name for name in _NODE_NAMES.values()})
    for name in _NODE_NAMES.values():
        g.add_edge(name, END)
    return g
