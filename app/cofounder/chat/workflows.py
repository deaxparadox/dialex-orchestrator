"""`CofounderWorkflow` — same long-running, Update-driven shape as
`ConsultationWorkflow` (spec 0044): parks for the session's lifetime,
`submit_message` handles each new turn and returns the reply directly
(verified against the original source: its reply is already synchronous,
its WebSocket is a separate status tracker, not the content path — no
WebSocket in this phase).

Known, deliberate limitation: no terminal state — every session's workflow
runs forever. Fine for proving the mechanism; revisit once there's a real
reason to end a chat session."""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from . import activities

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

        reply = await workflow.execute_activity(
            activities.generate_cofounder_reply,
            args=[session_id, turns],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_ACTIVITY_RETRY,
        )

        self._turn_count += 1
        await workflow.execute_activity(
            activities.persist_cofounder_turn,
            args=[session_id, self._turn_count, "agent", reply],
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_ACTIVITY_RETRY,
        )
        return {"reply": reply}
