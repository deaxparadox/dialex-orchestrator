"""One-time (but safely re-runnable) ingestion of the cofounder agent's
training corpus into Pinecone (spec 0048). Not part of request-serving
code — run manually:

    python -m scripts.ingest_cofounder_training_data

Creates the integrated index if it doesn't already exist (Pinecone embeds
each chunk's text server-side — no separate OpenAI embedding call, unlike
the original's bring-your-own-embedding approach), then chunks and
upserts the 6 training .docx files. Re-running overwrites existing chunk
IDs rather than duplicating them (each chunk's id is
"<filename>:<chunk-index>", stable across runs).
"""

import asyncio
import logging
from pathlib import Path

from docx import Document
from pinecone import AsyncPinecone

from app.core.config import settings
from app.cofounder.chat.tools.pinecone_rag import NAMESPACE

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TRAINING_DATA_DIR = Path(__file__).resolve().parent.parent / "app/cofounder/chat/data/training"
_CHUNK_CHAR_LIMIT = 1000


def _chunk_paragraphs(paragraphs: list[str], limit: int = _CHUNK_CHAR_LIMIT) -> list[str]:
    """Accumulates paragraphs into ~limit-character chunks — simple
    paragraph-accumulation, not a single blob per file, for reasonable
    retrieval granularity on these modest-sized (22-143KB) documents."""
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for para in paragraphs:
        if current_len + len(para) > limit and current:
            chunks.append("\n".join(current))
            current, current_len = [], 0
        current.append(para)
        current_len += len(para)
    if current:
        chunks.append("\n".join(current))
    return chunks


def _extract_chunks(docx_path: Path) -> list[str]:
    doc = Document(docx_path)
    paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    return _chunk_paragraphs(paragraphs)


async def main() -> None:
    async with AsyncPinecone(api_key=settings.pinecone_api_key) as pc:
        existing = [idx.name async for idx in pc.indexes.list()]
        if settings.pinecone_index_name not in existing:
            logger.info("Creating integrated index %r", settings.pinecone_index_name)
            # pc.indexes.create(spec=IntegratedSpec(...)) is rejected by the
            # installed SDK (10.0.0) — its own error message pointed at this
            # replacement API, create_for_model, from a 2026-07 change more
            # recent than what the docs described. Verified against the
            # library itself, not guessed.
            await pc.indexes.create_for_model(
                name=settings.pinecone_index_name,
                cloud="aws",
                region="us-east-1",
                embed={"model": "multilingual-e5-large", "field_map": {"text": "text"}},
            )
        else:
            logger.info("Index %r already exists", settings.pinecone_index_name)

        description = await pc.indexes.describe(settings.pinecone_index_name)
        index = await pc.index(host=description.host)

        async with index:
            docx_files = sorted(TRAINING_DATA_DIR.glob("*.docx"))
            if not docx_files:
                raise RuntimeError(f"No .docx files found in {TRAINING_DATA_DIR}")

            total_chunks = 0
            for docx_path in docx_files:
                chunks = _extract_chunks(docx_path)
                if not chunks:
                    logger.warning("No text extracted from %s", docx_path.name)
                    continue
                records = [
                    {"_id": f"{docx_path.stem}:{i}", "text": chunk}
                    for i, chunk in enumerate(chunks)
                ]
                await index.upsert_records(namespace=NAMESPACE, records=records)
                logger.info("Ingested %d chunks from %s", len(records), docx_path.name)
                total_chunks += len(records)

            logger.info("Done — %d total chunks across %d files", total_chunks, len(docx_files))


if __name__ == "__main__":
    asyncio.run(main())
