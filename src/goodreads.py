"""Read community ratings from Goodreads' public ISBN search."""

import json
import logging
from dataclasses import dataclass
from http.client import HTTPException
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)
SEARCH_URL = "https://www.goodreads.com/book/auto_complete"


@dataclass(frozen=True)
class GoodreadsRating:
    average: float
    votes: int


NO_RATING = GoodreadsRating(0.0, 0)


def fetch_rating(isbn: str) -> GoodreadsRating | None:
    """Return a rating, NO_RATING for a confirmed miss, or None on failure."""
    query = urlencode({"format": "json", "q": isbn})
    request = Request(f"{SEARCH_URL}?{query}", headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=5) as response:
            matches = json.load(response)
    except (OSError, HTTPException, ValueError) as error:
        logger.warning("Goodreads rating unavailable for ISBN %s: %s", isbn, error)
        return None

    if not isinstance(matches, list):
        return None
    if not matches:
        return NO_RATING
    if len(matches) != 1:
        return None
    match = matches[0]
    if not isinstance(match, dict):
        return None
    votes = match.get("ratingsCount")
    average = match.get("avgRating")
    if type(votes) is not int or not 0 <= votes < 2**63:
        return None
    if votes == 0:
        return NO_RATING
    if type(average) not in (int, float, str):
        return None
    try:
        average = float(average)
    except OverflowError, ValueError:
        return None
    if 0 < average <= 5:
        return GoodreadsRating(average, votes)
    return None
