"""Honest placeholders for the original's Bubble.io-only tools (spec 0045's
TODO, spec 0046). `ai/tools/enterpreneur_search.py`'s get_bubble_entreprenurs/
get_bubble_freelancers_v2 queried the client's own Bubble.io-hosted
entrepreneur/freelancer directory directly (settings.BUBBLE_BASE_URL) — no
equivalent data source exists in this platform. Per the user's explicit
call, these stay bound to the agent (not removed) so the tool list's shape
matches the original, but return a clearly-marked "not connected" result
rather than fabricating data or crashing — the agent can reason around a
tool it knows is unavailable."""

from langchain_core.tools import tool

_NOT_CONNECTED = {
    "available": False,
    "message": (
        "This directory lookup isn't connected to a real data source yet in "
        "this platform — the original relied on a specific client's own "
        "Bubble.io-hosted database, which doesn't exist here."
    ),
}


@tool
async def get_bubble_entreprenurs() -> dict:
    """Look up entrepreneurs in the platform's directory. Not yet connected
    to a real data source in this platform."""
    return _NOT_CONNECTED


@tool
async def get_bubble_freelancers_v2(email: str | None = None) -> dict:
    """Look up freelancers in the platform's directory, optionally by email.
    Not yet connected to a real data source in this platform."""
    return _NOT_CONNECTED
