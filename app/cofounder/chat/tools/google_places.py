"""Ported from ai/tools/google_place_search.py's search_place_and_rating_v2
+ services/google/place.py (spec 0047) — direct HTTP calls to Google's
Places API (New), verified still current against its live docs. Only the
`_v2` tool is ported: the original also has a non-`_v2` `search_place_and_rating`
that's never actually bound to the agent's tool list, matching the
"only port what's reachable" discipline already used for the dead graph
nodes in spec 0045."""

import logging

import aiohttp
from langchain_core.tools import tool

from ....core.config import settings

logger = logging.getLogger(__name__)


async def search_place(place: str) -> str | None:
    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": settings.google_api_key,
        "X-Goog-FieldMask": "places.id,places.displayName,places.formattedAddress",
    }
    payload = {"textQuery": place}

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as response:
            data = await response.json()

    logger.debug("Searched places: %s", data)

    if places := data.get("places"):
        return places[0]["id"]
    return None


async def get_place_details(place_id: str) -> dict:
    url = f"https://places.googleapis.com/v1/places/{place_id}"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": settings.google_api_key,
        "X-Goog-FieldMask": "id,displayName,formattedAddress,rating,userRatingCount,reviews,websiteUri",
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as response:
            data = await response.json()

    logger.debug("Place detail: %s", data)
    return data


@tool
async def search_place_and_rating_v2(place: str) -> dict:
    """
    Search for businesses, professionals, and services using Google Places API.

    **WHEN TO USE THIS TOOL:**
    - After get_bubble_freelancers returns empty or no relevant matches
    - When user explicitly asks to "search Google" or "find online"
    - To find external professionals (lawyers, consultants, etc.) in specific locations
    - To get highly-rated businesses with reviews and contact information

    **BEST FOR FINDING:**
    - Legal professionals: "employment lawyer in Bangalore"
    - Law firms: "contract review attorney in Mumbai"
    - Specialized lawyers: "non-compete agreement lawyer in Delhi"
    - Any local business or professional service

    **SEARCH TIPS:**
    - Be specific: Include profession + location in query
    - Good: "employment lawyer in Bangalore"
    - Bad: "lawyer" (too generic)
    - Include specialization if user mentioned it: "IP lawyer in Bangalore"

    **WHAT YOU GET BACK:**
    - Business/professional name and address
    - Google ratings and review count
    - Phone number and website
    - Business hours and other details
    - Returns empty dict {} if nothing found

    **TYPICAL WORKFLOW:**
    1. User asks: "Find me a lawyer in Bangalore"
    2. First call: get_bubble_freelancers() → Empty
    3. Then call: search_place_and_rating("employment lawyer in Bangalore")
    4. Present the results professionally

    **MULTIPLE RESULTS:**
    - This tool returns ONE result per call
    - To show multiple options, call this tool 2-3 times with slightly different queries:
      * Call 1: "employment lawyer Bangalore"
      * Call 2: "contract attorney Bangalore"
      * Call 3: "law firm Bangalore"
    - This gives users variety to choose from

    Args:
        place: Search query for the business/professional you're looking for.
               Should include BOTH what you're searching for AND location.
               Examples:
               - "employment lawyer in Bangalore"
               - "contract review attorney Mumbai"
               - "intellectual property lawyer Delhi"

    Returns:
        Dictionary with business details (name, address, rating, contact info).
        Returns empty dict {} if no results found.

    **HANDLING EMPTY RESULTS:**
    If this returns {}, try:
    1. Broader search: "lawyer in Bangalore" instead of "employment lawyer in Bangalore"
    2. Different phrasing: "attorney" instead of "lawyer"
    3. If still empty, suggest online legal services (LegalZoom, Rocket Lawyer, etc.)
    """
    logger.debug("Searching for place: %s", place)
    place_id = await search_place(place)

    if not place_id:
        return {}

    logger.debug("Get details for %s with id %s", place, place_id)
    data = await get_place_details(place_id)

    logger.debug("Place data: %s", data)
    return data
