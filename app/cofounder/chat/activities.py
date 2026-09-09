"""Plain Temporal Activities for the cofounder chat workflow (spec 0044) —
same shape as `dialex/consultations/activities.py`. The actual reply
generation is a LangGraph graph (spec 0045, `graphs.py`), run via
`temporalio.contrib.langgraph`'s plugin, not a plain activity here — these
two only handle persistence."""

from temporalio import activity

from ...core.observability import bind_cofounder_context
from . import queries


@activity.defn
async def persist_cofounder_turn(
    session_id: int, turn_number: int, speaker: str, content: str, step: int | None = None
) -> int:
    bind_cofounder_context(cofounder_session_id=session_id)
    return await queries.insert_turn(session_id, turn_number, speaker, content, step)


@activity.defn
async def fetch_cofounder_turns(session_id: int) -> list[dict]:
    bind_cofounder_context(cofounder_session_id=session_id)
    return await queries.get_turns(session_id)


ALL_ACTIVITIES = [
    persist_cofounder_turn,
    fetch_cofounder_turns,
]
