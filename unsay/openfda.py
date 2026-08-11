"""openFDA client.

Two endpoints matter:

  /drug/enforcement.json  recall notices, roughly 17.8k records back to 2004,
                          refreshed weekly. Carries recall_initiation_date and
                          termination_date, which map directly onto the valid
                          time of a safety claim.

  /drug/label.json        Structured Product Labeling, including boxed
                          warnings and contraindications. `effective_time`
                          marks when a revision took effect, and `spl_set_id`
                          is stable across revisions, which together give the
                          version chain a label needs.

No key is required. A free key raises the rate limit and is worth setting for
a full backfill: https://open.fda.gov/apis/authentication/
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import httpx

from unsay.config import settings

log = logging.getLogger(__name__)

BASE = "https://api.fda.gov"
PAGE = 100
# openFDA refuses skip beyond 25000; past that you must paginate by search key.
MAX_SKIP = 25_000


def _params(extra: dict[str, Any]) -> dict[str, Any]:
    p = dict(extra)
    key = settings().openfda_api_key
    if key:
        p["api_key"] = key
    return p


def _get(client: httpx.Client, path: str, params: dict[str, Any]) -> dict:
    resp = client.get(f"{BASE}{path}", params=_params(params), timeout=30.0)
    if resp.status_code == 404:
        # openFDA returns 404 for "no matches", which is not an error here.
        return {"results": [], "meta": {"results": {"total": 0}}}
    resp.raise_for_status()
    return resp.json()


def total(path: str, search: str) -> int:
    with httpx.Client() as client:
        data = _get(client, path, {"search": search, "limit": 1})
        return int(data.get("meta", {}).get("results", {}).get("total", 0))


def paginate(path: str, search: str, limit: int | None = None) -> Iterator[dict]:
    """Yield records for a search, stopping at openFDA's skip ceiling."""
    fetched = 0
    skip = 0
    with httpx.Client() as client:
        while skip < MAX_SKIP:
            want = PAGE if limit is None else min(PAGE, limit - fetched)
            if want <= 0:
                return
            data = _get(client, path, {"search": search, "limit": want, "skip": skip})
            rows = data.get("results", [])
            if not rows:
                return
            for row in rows:
                yield row
                fetched += 1
                if limit is not None and fetched >= limit:
                    return
            skip += len(rows)


def recalls(since: str = "2020-01-01", limit: int | None = None) -> Iterator[dict]:
    """Drug recall notices with a report date on or after ``since``."""
    # openFDA range syntax is Lucene. The separator must reach the server as a
    # literal space; writing "+TO+" here gets percent-encoded into %2BTO%2B by
    # the client and the API answers 500.
    since_c = since.replace("-", "")
    search = f"report_date:[{since_c} TO 29991231]"
    yield from paginate("/drug/enforcement.json", search, limit)


def boxed_warnings(limit: int | None = None) -> Iterator[dict]:
    """Labels that carry a boxed warning, the strongest warning the FDA issues."""
    yield from paginate("/drug/label.json", "_exists_:boxed_warning", limit)
