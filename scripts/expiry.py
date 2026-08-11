"""Proof that MVCC replay has a horizon and bitemporal reconstruction does not.

    UNSAY_ALLOW_FAKE_EMBEDDINGS=1 python scripts/expiry.py

The obvious way to build "what did the agent know when it decided" on
CockroachDB is to store the read timestamp and replay it with
AS OF SYSTEM TIME. It is elegant, it needs no extra tables, and it is correct
right up until the garbage collector moves past the timestamp you saved.

CockroachDB's own documentation says so plainly: gc.ttlseconds "is not meant
to be a solution for long-term retention of history; for that you should
handle versioning in the schema design at the application layer." The default
window is 4 hours. 25 hours is the largest value Cockroach Labs regularly
tests.

That matters for any claim about auditability. A regulator asking what a
system knew when it made a decision is not asking about yesterday afternoon.

This script asks one question two ways, at an instant far enough back that the
difference shows, and prints both answers.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("UNSAY_ALLOW_FAKE_EMBEDDINGS", "1")

from unsay import embeddings, memory  # noqa: E402
from unsay.db import close_pool, connection, query, run_in_txn  # noqa: E402

KEY = "expiry-demo:sartan-lot-88"
DAYS_BACK = 45


def seed() -> str:
    """Lay down a claim that changed 30 days ago, with honest backdating.

    asserted_at and retracted_at are written explicitly rather than defaulted,
    because the point of transaction time is that it records when the system
    believed something, and a demo of history needs history.
    """
    now = datetime.now(timezone.utc)
    v1_asserted = now - timedelta(days=60)
    v1_retracted = now - timedelta(days=30)
    vec = embeddings.to_sql(embeddings.embed("sartan lot 88 safety"))

    def work(conn):
        conn.execute("DELETE FROM fact WHERE fact_key = %s", (KEY,))
        conn.execute(
            """
            INSERT INTO fact (fact_key, version, subject_kind, subject_id, predicate,
                              claim, severity, valid_from, asserted_at, retracted_at,
                              source, source_ref, content_hash, embedding)
            VALUES (%s, 1, 'lot', 'sartan-demo', 'recall',
                    'No open recall affects sartan lot 88.', 'info',
                    %s, %s, %s, 'expiry-demo', 'v1', 'h1', %s::VECTOR)
            """,
            (KEY, v1_asserted, v1_asserted, v1_retracted, vec),
        )
        conn.execute(
            """
            INSERT INTO fact (fact_key, version, subject_kind, subject_id, predicate,
                              claim, severity, valid_from, asserted_at,
                              source, source_ref, content_hash, embedding)
            VALUES (%s, 2, 'lot', 'sartan-demo', 'recall',
                    'Class I recall: sartan lot 88 exceeds the NDMA limit.', 'class_i',
                    %s, %s, 'expiry-demo', 'v2', 'h2', %s::VECTOR)
            """,
            (KEY, v1_retracted, v1_retracted, vec),
        )

    run_in_txn(work, label="seed_expiry")
    # The HLC just after the seed commits. The control below has to compare the
    # two routes at an instant where the rows physically exist, otherwise it is
    # measuring "the write had not happened yet", not "the routes disagree".
    return str(query("SELECT cluster_logical_timestamp() AS t")[0]["t"])


def main() -> int:
    after_seed = seed()
    moment = datetime.now(timezone.utc) - timedelta(days=DAYS_BACK)
    print(f"question: what did this system believe about sartan lot 88, "
          f"{DAYS_BACK} days ago?\n")

    # ---------------------------------------------------------------- route A
    print("route A -- bitemporal reconstruction (application-level versioning)")
    rows = memory.facts_as_believed_at(moment, subject_id="sartan-demo")
    if rows:
        for r in rows:
            print(f"  ANSWERED: v{r['version']} [{r['severity']}] {r['claim']}")
    else:
        print("  no rows")
    route_a_ok = len(rows) == 1 and rows[0]["version"] == 1

    # ---------------------------------------------------------------- route B
    print("\nroute B -- MVCC time-travel (AS OF SYSTEM TIME at the same instant)")
    nanos = int(moment.timestamp() * 1_000_000_000)
    route_b_ok = False
    try:
        with connection() as conn:
            with conn.transaction():
                conn.execute(f"SET TRANSACTION AS OF SYSTEM TIME {nanos}")
                cur = conn.execute(
                    "SELECT version, severity, claim FROM fact WHERE fact_key = %s", (KEY,)
                )
                got = cur.fetchall()
        for r in got:
            print(f"  ANSWERED: v{r['version']} [{r['severity']}] {r['claim']}")
        route_b_ok = True
    except Exception as exc:
        msg = str(exc).strip().splitlines()[0]
        print(f"  FAILED: {msg[:190]}")

    # ------------------------------------------------------------ inside window
    print("\ncontrol -- both routes at an instant inside the GC window")
    a_now = [
        r["version"]
        for r in memory.facts_as_believed_at(
            datetime.now(timezone.utc), subject_id="sartan-demo"
        )
    ]
    with connection() as conn:
        with conn.transaction():
            conn.execute(f"SET TRANSACTION AS OF SYSTEM TIME {after_seed}")
            b_now = [
                r["version"]
                for r in conn.execute(
                    "SELECT version FROM fact WHERE fact_key = %s AND retracted_at IS NULL",
                    (KEY,),
                ).fetchall()
            ]
    agree = a_now == b_now
    print(f"  bitemporal: v{a_now}   MVCC: v{b_now}   agree: {agree}")

    ttl = query("SHOW ZONE CONFIGURATION FOR DATABASE unsay")
    window = next((line for row in ttl for line in str(row.get("raw_config_sql", "")).splitlines()
                   if "gc.ttlseconds" in line), "gc.ttlseconds = (default 14400)")

    print("\n" + "-" * 68)
    print(f"configured window: {window.strip()}")
    print(f"  bitemporal answered at {DAYS_BACK} days: {route_a_ok}")
    print(f"  MVCC answered at {DAYS_BACK} days:       {route_b_ok}")
    print(f"  the two agree inside the window:      {agree}")
    print("-" * 68)

    if route_a_ok and not route_b_ok and agree:
        print("\nPASSED: replay outlives the garbage-collection window.")
        print("Both routes agree while MVCC history exists. Past the horizon the")
        print("MVCC route is gone and the bitemporal route still answers exactly.")
        close_pool()
        return 0

    print("\nINCONCLUSIVE: see routes above.")
    close_pool()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
