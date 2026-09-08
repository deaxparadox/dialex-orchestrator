"""SQLAlchemy Core helpers against `generated_tables.py` (decision 9) —
same shape as `dialex/consultations/queries.py`."""

from datetime import date, datetime, timezone

from sqlalchemy import insert, select

from ...core.db import engine
from ...core.generated_tables import t_cofounder_chat_cofoundersession, t_cofounder_chat_cofounderturn


def _serialize(row: dict) -> dict:
    return {
        key: value.isoformat() if isinstance(value, (datetime, date)) else value
        for key, value in row.items()
    }


async def get_session(session_id: int) -> dict | None:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(t_cofounder_chat_cofoundersession).where(
                    t_cofounder_chat_cofoundersession.c.id == session_id
                )
            )
        ).mappings().first()
        return _serialize(dict(row)) if row else None


async def insert_session(user_id: int) -> int:
    async with engine.begin() as conn:
        result = await conn.execute(
            insert(t_cofounder_chat_cofoundersession).values(
                user_id=user_id,
                title="New chat",
                created_at=datetime.now(timezone.utc),
            )
        )
        return result.inserted_primary_key[0]


async def get_turns(session_id: int) -> list[dict]:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                select(t_cofounder_chat_cofounderturn)
                .where(t_cofounder_chat_cofounderturn.c.session_id == session_id)
                .order_by(t_cofounder_chat_cofounderturn.c.turn_number)
            )
        ).mappings().all()
        return [_serialize(dict(row)) for row in rows]


async def insert_turn(session_id: int, turn_number: int, speaker: str, content: str) -> int:
    async with engine.begin() as conn:
        result = await conn.execute(
            insert(t_cofounder_chat_cofounderturn).values(
                session_id=session_id,
                turn_number=turn_number,
                speaker=speaker,
                content=content,
                created_at=datetime.now(timezone.utc),
            )
        )
        return result.inserted_primary_key[0]
