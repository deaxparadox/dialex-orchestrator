"""`CofounderWorkflow` — same long-running, Update-driven shape as
`ConsultationWorkflow` (spec 0044): parks for the session's lifetime,
`submit_message` handles each new turn and returns the reply directly
(verified against the original source: its reply is already synchronous,
its WebSocket is a separate status tracker, not the content path — no
WebSocket in this phase).

The actual reply is now a real LangGraph graph (spec 0045) — router +
image-generation path — run via `graph(COFOUNDER_GRAPH)`, the same
mechanism `ConsultationWorkflow` uses for its own graph. `COFOUNDER_GRAPH`'s
name string is redeclared here rather than imported from `graphs.py` —
importing that module would pull langchain/langgraph/openai into the
workflow's deterministic-execution path for no reason, same reasoning
`dialex/consultations/workflows.py` already follows. Only `worker.py`
needs the actual graph-builder function.

Known, deliberate limitation: no terminal state — every session's workflow
runs forever. Fine for proving the mechanism; revisit once there's a real
reason to end a chat session."""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.contrib.langgraph import graph

with workflow.unsafe.imports_passed_through():
    from . import activities

COFOUNDER_GRAPH = "cofounder-graph"
COFOUNDER_STRUCTURED_GRAPH = "cofounder-structured-graph"

_ACTIVITY_TIMEOUT = timedelta(seconds=30)
_ACTIVITY_RETRY = RetryPolicy(initial_interval=timedelta(seconds=1), maximum_attempts=3)


@workflow.defn
class CofounderWorkflow:
    def __init__(self) -> None:
        self._turn_count = 0

    @workflow.run
    async def run(self, session_id: int) -> None:
        # Parked for the session's entire interactive lifetime — a workflow
        # only accepts Updates while running, so returning early would mean
        # submit_message simply can't reach it (same reasoning as
        # ConsultationWorkflow, ADR 0005 decision 2).
        await workflow.wait_condition(lambda: False)

    @workflow.update
    async def submit_message(self, session_id: int, text: str) -> dict:
        self._turn_count += 1
        await workflow.execute_activity(
            activities.persist_cofounder_turn,
            args=[session_id, self._turn_count, "user", text],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_ACTIVITY_RETRY,
        )

        turns = await workflow.execute_activity(
            activities.fetch_cofounder_turns,
            session_id,
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_ACTIVITY_RETRY,
        )

        result = await graph(COFOUNDER_GRAPH).compile().ainvoke(
            {"session_id": session_id, "turns": turns, "router_response": None, "reply": None, "image_id": None}
        )

        self._turn_count += 1
        await workflow.execute_activity(
            activities.persist_cofounder_turn,
            args=[session_id, self._turn_count, "agent", result["reply"]],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_ACTIVITY_RETRY,
        )
        # image_id, not a URL — the Workflow/graph stay decoupled from HTTP
        # routing concerns; router.py builds the actual served URL.
        return {"reply": result["reply"], "image_id": result.get("image_id")}

    @workflow.update
    async def submit_structured_message(self, session_id: int, text: str, step: int) -> dict:
        """Same persist-then-fetch-then-invoke-then-persist shape as
        submit_message, but runs `structured_graph` (spec 0049) and tags
        both turns with `step` — the original's second, client-driven
        endpoint (`ai/views/structured_agent.py`), reusing this same
        session/workflow rather than a separate one (see spec 0049's
        persistence design: one turn history, structured and free-form
        turns told apart only by the `step` column)."""
        self._turn_count += 1
        await workflow.execute_activity(
            activities.persist_cofounder_turn,
            args=[session_id, self._turn_count, "user", text, step],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_ACTIVITY_RETRY,
        )

        turns = await workflow.execute_activity(
            activities.fetch_cofounder_turns,
            session_id,
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_ACTIVITY_RETRY,
        )

        result = await graph(COFOUNDER_STRUCTURED_GRAPH).compile().ainvoke(
            {"session_id": session_id, "turns": turns, "step": step, "reply": None}
        )

        self._turn_count += 1
        await workflow.execute_activity(
            activities.persist_cofounder_turn,
            args=[session_id, self._turn_count, "agent", result["reply"], step],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_ACTIVITY_RETRY,
        )
        return {"reply": result["reply"]}
