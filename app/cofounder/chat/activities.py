"""Plain Temporal Activities for the cofounder chat workflow (spec 0044) —
same shape as `dialex/consultations/activities.py`. `generate_cofounder_reply` is a
single plain LLM call, no LangGraph graph, no tools — the real
`entrepreneur_graph`/tools port is Phase 1b+, its own spec."""

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from temporalio import activity

from ...core.config import settings
from ...core.observability import bind_cofounder_context
from . import queries

# Placeholder behavior for Phase 1a — the real multi-agent graph (router,
# ideation/roadmap/image-generation nodes, RAG/search/places tools)
# replaces this entirely in Phase 1b. Not the product's real personality.
_PLACEHOLDER_SYSTEM_PROMPT = (
    "You are Brunda, an AI co-founder assistant. (Placeholder behavior for "
    "Phase 1a — the real multi-agent graph replaces this in Phase 1b.)"
)

_llm = ChatOpenAI(model="gpt-4o-mini", api_key=settings.openai_api_key)


@activity.defn
async def persist_cofounder_turn(session_id: int, turn_number: int, speaker: str, content: str) -> int:
    bind_cofounder_context(cofounder_session_id=session_id)
    return await queries.insert_turn(session_id, turn_number, speaker, content)


@activity.defn
async def fetch_cofounder_turns(session_id: int) -> list[dict]:
    bind_cofounder_context(cofounder_session_id=session_id)
    return await queries.get_turns(session_id)


@activity.defn
async def generate_cofounder_reply(session_id: int, turns: list[dict]) -> str:
    """`turns` already includes the just-persisted user message (the
    workflow calls persist_cofounder_turn before fetch_cofounder_turns, same order as
    ConsultationWorkflow) — the last entry *is* the current message, not
    something to append again."""
    bind_cofounder_context(cofounder_session_id=session_id)
    messages = [SystemMessage(_PLACEHOLDER_SYSTEM_PROMPT)]
    for turn in turns:
        messages.append(HumanMessage(turn["content"]) if turn["speaker"] == "user" else AIMessage(turn["content"]))

    response = await _llm.ainvoke(messages)
    return response.content


ALL_ACTIVITIES = [
    persist_cofounder_turn,
    fetch_cofounder_turns,
    generate_cofounder_reply,
]
