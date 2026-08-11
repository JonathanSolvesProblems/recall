"""CockroachDB connection handling.

Two things here are not optional against a distributed SQL database and are
the most common way an otherwise-correct application misbehaves under load or
during a failover:

1. Retry on serialization failure. CockroachDB runs SERIALIZABLE by default,
   so a transaction can be aborted with SQLSTATE 40001 and the client is
   expected to replay it. The database is not broken when this happens; a
   client that does not retry is.

2. Multiple hosts in the connection string. psycopg tries them in order, so
   losing the region an app was talking to costs one reconnect rather than an
   outage. No load balancer sits in front of this.
"""

from __future__ import annotations

import logging
import os
import random
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from unsay.config import settings

log = logging.getLogger(__name__)

T = TypeVar("T")

# SQLSTATE 40001. CockroachDB asks the client to replay the transaction.
SERIALIZATION_FAILURE = "40001"

# Raised while a range is leaderless, e.g. in the seconds after a region is
# removed. Also worth replaying rather than surfacing to a user.
CONNECTION_STATES = {"08000", "08003", "08006", "08001", "08004", "57P01"}

_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        # Sized from the environment so a serverless deployment can keep the
        # pool tiny. A frozen Lambda execution environment holds its
        # connections open while doing nothing, and CockroachDB Cloud Basic
        # caps concurrent connections, so the 2..16 default is wrong there.
        _pool = ConnectionPool(
            conninfo=settings().unsay_dsn,
            min_size=int(os.environ.get("UNSAY_POOL_MIN", "2")),
            max_size=int(os.environ.get("UNSAY_POOL_MAX", "16")),
            kwargs={"row_factory": dict_row, "application_name": "unsay"},
            open=True,
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    with pool().connection() as conn:
        yield conn


def run_in_txn(
    fn: Callable[[psycopg.Connection], T],
    *,
    max_attempts: int = 8,
    label: str = "txn",
) -> T:
    """Run ``fn`` inside a transaction, replaying it on retryable errors.

    Backoff is exponential with jitter. Jitter matters more than usual here:
    many agents contending on the same memory rows will otherwise retry in
    lockstep and keep colliding.
    """
    last: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            with connection() as conn:
                with conn.transaction():
                    return fn(conn)
        except psycopg.errors.Error as exc:
            state = getattr(exc, "sqlstate", None)
            retryable = state == SERIALIZATION_FAILURE or state in CONNECTION_STATES
            if not retryable or attempt == max_attempts:
                raise
            last = exc
            delay = min(0.05 * (2 ** (attempt - 1)), 2.0)
            delay += random.uniform(0, delay)
            log.warning(
                "%s: retryable %s on attempt %d/%d, replaying in %.2fs",
                label, state, attempt, max_attempts, delay,
            )
            time.sleep(delay)

    assert last is not None
    raise last


def query(sql: str, params: tuple[Any, ...] | dict[str, Any] | None = None) -> list[dict]:
    with connection() as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else []


def query_as_of(
    sql: str,
    hlc: str,
    params: tuple[Any, ...] | dict[str, Any] | None = None,
) -> list[dict]:
    """Read the cluster exactly as it stood at a past HLC timestamp.

    Only valid inside the garbage-collection window (25 hours as configured in
    sql/001_bootstrap.sql). Outside it, reconstruct the same state from the
    bitemporal columns on `fact` instead, which is exact and unbounded. See
    ``unsay.memory.facts_as_believed_at``.

    AS OF SYSTEM TIME does not accept placeholders, so the timestamp is
    interpolated. It is validated as a decimal literal first.
    """
    try:
        float(hlc)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"refusing to interpolate non-numeric HLC: {hlc!r}") from exc

    # SET TRANSACTION AS OF SYSTEM TIME is only meaningful as the first
    # statement of an explicit transaction, so the read runs inside one.
    with connection() as conn:
        with conn.transaction():
            conn.execute(f"SET TRANSACTION AS OF SYSTEM TIME {hlc}")
            cur = conn.execute(sql, params)
            return cur.fetchall() if cur.description else []
