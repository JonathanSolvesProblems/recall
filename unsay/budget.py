"""Spend controls for a publicly reachable demo.

The demo URL has no authentication, because judges must be able to open it. So
anything behind it that costs money per request is an open tap, and a hackathon
demo is exactly the sort of thing a curious visitor will click forty times.

Three defences, cheapest first:

1. Do not spend at all where the answer is already known. A sweep re-decides
   twelve answers to the same question against the same evidence and gets the
   same reply twelve times; a reset re-asks a question whose answer has not
   changed. Both are memoised.

2. A hard daily ceiling on model calls, counted in the database rather than in
   process memory, because Lambda has many short-lived processes and an
   in-memory counter would reset constantly.

3. A per-caller rate limit, so one visitor cannot consume the day's ceiling in
   a minute and leave the next judge with a dead demo.

The ceiling degrades honestly: past it the API says the demo has reached its
daily budget and when it resets, rather than erroring or silently returning
something fabricated.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from unsay.db import run_in_txn

log = logging.getLogger(__name__)


class BudgetExceeded(RuntimeError):
    """Raised when the demo has spent its allowance for the day."""


def _limit() -> int:
    return int(os.environ.get("UNSAY_DAILY_MODEL_CALLS", "200"))


def _per_caller_limit() -> int:
    return int(os.environ.get("UNSAY_CALLER_HOURLY_CALLS", "40"))


DDL = """
CREATE TABLE IF NOT EXISTS model_usage (
    day      DATE   NOT NULL,
    caller   STRING NOT NULL,
    hour     INT2   NOT NULL,
    calls    INT8   NOT NULL DEFAULT 0,
    CONSTRAINT pk_model_usage PRIMARY KEY (day, caller, hour)
)
"""


def ensure_table() -> None:
    run_in_txn(lambda c: c.execute(DDL), label="usage_ddl")


def charge(caller: str = "anon", n: int = 1) -> None:
    """Record ``n`` model calls, refusing once a ceiling is reached.

    Counted before the call rather than after. Charging afterwards means a
    burst of concurrent requests all pass the check and then all spend, which
    is precisely the case the ceiling exists for.
    """
    now = datetime.now(timezone.utc)
    day, hour = now.date(), now.hour
    daily_cap, caller_cap = _limit(), _per_caller_limit()

    def work(conn) -> None:
        today = conn.execute(
            "SELECT coalesce(sum(calls), 0) AS n FROM model_usage WHERE day = %s", (day,)
        ).fetchone()["n"]
        if today + n > daily_cap:
            raise BudgetExceeded(
                f"This demo has used its budget of {daily_cap} model calls for today. "
                f"It resets at 00:00 UTC. Everything else on the page still works, "
                f"and the code and full results are in the repository."
            )

        mine = conn.execute(
            "SELECT coalesce(sum(calls), 0) AS n FROM model_usage "
            " WHERE day = %s AND caller = %s AND hour = %s",
            (day, caller, hour),
        ).fetchone()["n"]
        if mine + n > caller_cap:
            raise BudgetExceeded(
                f"You have used {caller_cap} model calls this hour, which is the "
                f"per-visitor limit that keeps the demo alive for the next person. "
                f"It resets on the hour."
            )

        conn.execute(
            """
            INSERT INTO model_usage (day, caller, hour, calls) VALUES (%s, %s, %s, %s)
            ON CONFLICT (day, caller, hour) DO UPDATE SET calls = model_usage.calls + %s
            """,
            (day, caller, hour, n, n),
        )

    run_in_txn(work, label="charge_budget")


def spent_today() -> dict:
    from unsay.db import query

    row = query(
        "SELECT coalesce(sum(calls), 0) AS n FROM model_usage WHERE day = current_date()"
    )[0]
    return {"used": int(row["n"]), "limit": _limit()}
