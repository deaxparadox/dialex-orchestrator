"""Ported from ai/tools/pinecone.py's query_pinecone_tool + services/pinecone.py
(spec 0048) — real-time retrieval from the cofounder training corpus.

The original queried Pinecone by first calling OpenAI's embeddings API
itself, then uploading/querying a raw vector (services/pinecone.py's
embed_query + client_pinecone_index.query(vector=..., ...), a plain
synchronous SDK call made without await inside an async function). This
uses Pinecone's newer integrated-index feature instead (user's call,
spec 0048): the index embeds text server-side, so there's no separate
embedding call to make, and the fully-async client (AsyncPinecone,
verified against the SDK's own docs) means no blocking-call workaround is
needed either — a genuine simplification over the original, not just a
port. Also fixes the original's own real, verified issue: query_pinecone
was defined twice in the source, and only the second definition (which
returns list[dict] with a bare "text" key) was ever actually reachable —
this matches that real, reachable shape, not the first, dead one."""

import logging

from langchain_core.tools import tool
from pinecone import AsyncPinecone

from ....core.config import settings

logger = logging.getLogger(__name__)

NAMESPACE = "cofounder-training"


@tool("query_pinecone_tool", return_direct=False)
async def query_pinecone_tool(question: str | None = None) -> list[dict] | str:
    """
    Use this tool to retrieve external resources, examples, supporting data,
    market validation, and reference material from the training corpus.
    Always call this tool before producing a roadmap to enrich the output
    with relevant resources.

    Args:
        question: Question to query the training corpus.

    Returns: List of {"text": ...} matches from the corpus.
    """
    if not question:
        return "Invalid question"

    logger.info("Query pinecone: %s", question)

    async with AsyncPinecone(api_key=settings.pinecone_api_key) as pc:
        description = await pc.indexes.describe(settings.pinecone_index_name)
        index = await pc.index(host=description.host)
        async with index:
            results = await index.search(
                namespace=NAMESPACE, inputs={"text": question}, top_k=5
            )

    data = [{"text": hit.fields.get("text")} for hit in results.result.hits]
    logger.debug("Pinecone data: %s", data)
    return data
