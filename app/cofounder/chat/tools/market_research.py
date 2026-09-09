"""Ported from ai/tools/web_search.py's market_research_tool (spec 0046) —
real-time market research via DuckDuckGo search + page scraping, ported
near-verbatim including its retry/decompression/rate-limiting hardening.

Two fixes over the original, both verified (not assumed):
- The original called `langchain_community.tools.DuckDuckGoSearchResults`,
  which wraps the now-renamed `duckduckgo_search` package (renamed to
  `ddgs`) and has a real, currently-open compatibility issue with it
  (langchain-ai/langchain#31892), on top of a longer-running, separate
  DuckDuckGo-scraping fragility pattern in the same library. This bypasses
  that wrapper entirely and calls `ddgs`'s own documented API directly.
- `ddgs`'s real result-dict keys are `title`/`href`/`body` (verified against
  its actual README) — the original's `item.get("link") or item.get("url")`
  assumed a different shape from LangChain's wrapper output and would have
  silently found nothing against ddgs's own raw output.
"""

import asyncio
import logging
import random
import zlib
import gzip
from typing import Optional

import aiohttp
from bs4 import BeautifulSoup
from ddgs import DDGS
from langchain_core.tools import tool

logger = logging.getLogger(__name__)


try:
    import brotli
    BROTLI_AVAILABLE = True
except ImportError:
    BROTLI_AVAILABLE = False
    logger.warning("Brotli not installed. Install with: pip install brotli")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
]


def get_headers() -> dict:
    """Generate realistic browser headers with safe Accept-Encoding."""
    accept_encoding = "gzip, deflate"
    if BROTLI_AVAILABLE:
        accept_encoding += ", br"

    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": accept_encoding,
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }


def manual_decompress(content: bytes, encoding: str) -> bytes:
    """Manually decompress content when automatic decompression fails."""
    if not encoding:
        return content

    encoding = encoding.lower().strip()

    try:
        if encoding == "gzip":
            try:
                return gzip.decompress(content)
            except gzip.BadGzipFile:
                return zlib.decompress(content, zlib.MAX_WBITS | 16)

        elif encoding == "deflate":
            try:
                return zlib.decompress(content, -zlib.MAX_WBITS)
            except zlib.error:
                return zlib.decompress(content)

        elif encoding == "br":
            if BROTLI_AVAILABLE:
                return brotli.decompress(content)
            else:
                logger.warning("Brotli content received but brotli not installed")
                return content

        elif encoding in ("identity", "none"):
            return content

        else:
            logger.warning(f"Unknown encoding: {encoding}, returning raw content")
            return content

    except Exception as e:
        logger.warning(f"Decompression failed for {encoding}: {e}, returning raw content")
        return content


async def safe_read_response(response: aiohttp.ClientResponse) -> str:
    """Safely read and decode response with fallback decompression."""
    content_encoding = response.headers.get("Content-Encoding", "").lower()
    content_type = response.headers.get("Content-Type", "")

    charset = "utf-8"
    if "charset=" in content_type:
        try:
            charset = content_type.split("charset=")[-1].split(";")[0].strip()
        except Exception:
            charset = "utf-8"

    try:
        text = await response.text(encoding=charset)

        if text and len(text) > 100:
            non_printable_ratio = sum(1 for c in text[:200] if ord(c) > 127 or ord(c) < 32) / 200
            if non_printable_ratio > 0.3:
                raise ValueError("Content appears to be binary/compressed")

        return text

    except (UnicodeDecodeError, ValueError, aiohttp.ClientPayloadError) as e:
        logger.warning(f"Auto-decode failed: {e}, attempting manual decompression")

        try:
            raw_content = await response.read()
            decompressed = manual_decompress(raw_content, content_encoding)

            for encoding in [charset, "utf-8", "latin-1", "cp1252"]:
                try:
                    return decompressed.decode(encoding, errors="replace")
                except (UnicodeDecodeError, LookupError):
                    continue

            return decompressed.decode("utf-8", errors="replace")

        except Exception as inner_e:
            logger.error(f"Manual decompression also failed: {inner_e}")
            return ""


async def fetch_page_text(
    url: str,
    session: aiohttp.ClientSession,
    max_retries: int = 2,
    timeout: int = 15,
    max_chars: int = 400,
) -> str:
    """Safely fetches and cleans a webpage with retry logic and robust decompression."""
    for attempt in range(max_retries + 1):
        try:
            logger.info(f"Fetching page (attempt {attempt + 1}): {url}")

            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=timeout),
                headers=get_headers(),
                allow_redirects=True,
                ssl=False,
                auto_decompress=True,
            ) as response:

                if response.status == 429:
                    wait_time = 2 ** attempt
                    logger.warning(f"Rate limited, waiting {wait_time}s...")
                    await asyncio.sleep(wait_time)
                    continue

                if response.status in (403, 404):
                    logger.warning(f"Access error ({response.status}) for {url}")
                    return ""

                response.raise_for_status()

                html = await safe_read_response(response)

                if not html:
                    logger.warning(f"Empty content from {url}")
                    continue

                soup = BeautifulSoup(html, "html.parser")

                for tag in soup(["script", "style", "noscript", "nav", "footer",
                                "header", "aside", "iframe", "form", "svg", "meta", "link"]):
                    tag.extract()

                main_content = (
                    soup.find("main") or
                    soup.find("article") or
                    soup.find(class_=["content", "main-content", "post-content", "article-body"]) or
                    soup.find(id=["content", "main", "article"]) or
                    soup.body
                )

                text = main_content.get_text(separator=" ", strip=True) if main_content else soup.get_text(separator=" ", strip=True)
                cleaned_text = " ".join(text.split())

                return cleaned_text[:max_chars]

        except aiohttp.ClientPayloadError as e:
            logger.warning(f"Payload/decompression error for {url}: {e}")

            if attempt < max_retries:
                try:
                    headers = get_headers()
                    headers["Accept-Encoding"] = "identity"

                    async with session.get(
                        url,
                        timeout=aiohttp.ClientTimeout(total=timeout),
                        headers=headers,
                        allow_redirects=True,
                        ssl=False,
                    ) as retry_response:
                        retry_response.raise_for_status()
                        html = await retry_response.text()

                        soup = BeautifulSoup(html, "html.parser")
                        for tag in soup(["script", "style", "noscript", "nav", "footer",
                                        "header", "aside", "iframe", "form"]):
                            tag.extract()

                        text = soup.get_text(separator=" ", strip=True)
                        return " ".join(text.split())[:max_chars]

                except Exception as retry_e:
                    logger.warning(f"Retry without compression also failed: {retry_e}")

        except asyncio.TimeoutError:
            logger.warning(f"Timeout fetching {url} (attempt {attempt + 1})")
        except aiohttp.ContentTypeError as e:
            logger.warning(f"Content type error for {url}: {e}")
            return ""
        except aiohttp.ClientError as e:
            logger.warning(f"Client error fetching {url}: {e} (attempt {attempt + 1})")
        except Exception as e:
            logger.error(f"Unexpected error fetching {url}: {e}")
            break

        if attempt < max_retries:
            await asyncio.sleep(1 * (attempt + 1))

    return ""


async def fetch_multiple_pages(urls: list[dict], session: aiohttp.ClientSession, max_per_category: int = 2) -> list[dict]:
    """Fetch multiple pages concurrently with rate limiting."""
    semaphore = asyncio.Semaphore(3)

    async def fetch_with_semaphore(item: dict) -> Optional[dict]:
        async with semaphore:
            # ddgs's real key is "href" (verified against its README) — the
            # original assumed "link"/"url", which don't exist on ddgs's own
            # raw output and would have silently matched nothing.
            url = item.get("href")
            title = item.get("title", "No Title")

            if not url:
                return None

            await asyncio.sleep(random.uniform(0.1, 0.3))
            content = await fetch_page_text(url, session)

            if content and len(content) > 50:
                return {
                    "title": title,
                    "url": url,
                    "content": content,
                }
            return None

    tasks = [fetch_with_semaphore(item) for item in urls[:max_per_category]]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    valid_results = []
    for r in results:
        if r and not isinstance(r, Exception):
            valid_results.append(r)
        elif isinstance(r, Exception):
            logger.warning(f"Task exception: {r}")

    return valid_results


def generate_market_queries(base_query: str) -> list[str]:
    """Generate targeted search queries for market analysis."""
    return [
        f"{base_query} market size TAM 2024",
        f"{base_query} trends growth forecast",
        f"{base_query} competitors landscape",
    ]


@tool("market_research_tool", return_direct=False)
async def market_research_tool(query: str) -> dict:
    """
    Real-time market analysis tool using DuckDuckGo + intelligent scraping.

    Performs multi-query search for comprehensive market insights including:
    - Market size (TAM/SAM/SOM)
    - Industry trends and growth
    - Competitive landscape

    Args:
        query: User query (e.g., 'gamified recycling education apps')

    Returns:
        dict with query, categories, and total_results
    """
    logger.info(f"Starting market research for: {query}")

    queries = generate_market_queries(query)

    categories = {
        "market_size": [],
        "trends": [],
        "competitors": [],
    }
    category_keys = list(categories.keys())

    connector = aiohttp.TCPConnector(
        limit=10,
        limit_per_host=3,
        force_close=True,
        enable_cleanup_closed=True,
    )

    timeout = aiohttp.ClientTimeout(total=30, connect=10)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        for idx, search_query in enumerate(queries):
            try:
                logger.info(f"Searching: {search_query}")

                if idx > 0:
                    await asyncio.sleep(1.5)

                # DDGS().text() is synchronous — run off the event loop so it
                # doesn't block other Activities on this worker process.
                search_results = await asyncio.to_thread(
                    lambda q=search_query: DDGS().text(q, max_results=5)
                )

                if not search_results:
                    logger.warning(f"No results for: {search_query}")
                    continue

                logger.info(f"Found {len(search_results)} results for: {search_query}")

                fetched_results = await fetch_multiple_pages(search_results, session, max_per_category=1)

                category_key = category_keys[idx] if idx < len(category_keys) else "market_size"
                categories[category_key].extend(fetched_results)

                total_results_so_far = sum(len(v) for v in categories.values())
                if total_results_so_far >= 3:
                    logger.info(f"Reached data threshold with {total_results_so_far} results, stopping search")
                    break

            except Exception as e:
                logger.error(f"Error searching '{search_query}': {e}")
                continue

    total_results = sum(len(v) for v in categories.values())
    logger.info(f"Market research complete. Total results: {total_results}")

    return {
        "query": query,
        "categories": {
            k: v[:2] for k, v in categories.items()
        },
        "total_results": min(total_results, 6),
    }
