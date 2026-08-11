"""Contention test: many agents writing the same memory at once.

    UNSAY_ALLOW_FAKE_EMBEDDINGS=1 python scripts/concurrency.py [writers] [keys]

The hackathon's premise is thousands of agents writing memory concurrently, and
Cockroach Labs' own framing is that agents "spawn autonomously, write
constantly". So the interesting question is not whether a single ingest works,
it is what the bitemporal invariants do when N writers race on the same claim.

Two invariants must hold no matter the interleaving:

  1. Version numbers are dense and unique per fact_key. No gaps, no duplicates.
  2. Exactly one version per fact_key is believed at any settled moment.

Both are properties of the schema plus the write path, not of luck. This
script tries to break them.
"""

from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("UNSAY_ALLOW_FAKE_EMBEDDINGS", "1")

import logging  # noqa: E402

from unsay import embeddings  # noqa: E402
from unsay.db import close_pool, pool, query, run_in_txn  # noqa: E402
from unsay.ingest import Claim, assert_claim  # noqa: E402

logging.getLogger("unsay.db").setLevel(logging.ERROR)  # retries are expected here


def writer(key: str, n: int) -> tuple[int, bool]:
    """Assert a distinct revision of one claim."""
    claim = Claim(
        fact_key=key,
        subject_kind="lot", subject_id="contention-drug", predicate="recall",
        claim=f"Revision {n} of the safety claim for {key}.",
        severity="class_ii",
        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc), valid_to=None,
        source="contention-test", source_ref=f"rev-{n}",
    )
    vec = embeddings.embed(claim.claim)
    return run_in_txn(lambda c: assert_claim(c, claim, vec), max_attempts=20, label="contend")


def main() -> int:
    writers = int(sys.argv[1]) if len(sys.argv) > 1 else 32
    keys = int(sys.argv[2]) if len(sys.argv) > 2 else 4

    fact_keys = [f"contention:key-{i}" for i in range(keys)]

    def cleanup(conn):
        conn.execute("DELETE FROM fact WHERE source = 'contention-test'")
    run_in_txn(cleanup, label="cleanup")

    pool().resize(min_size=8, max_size=max(16, writers))

    print(f"{writers} concurrent writers across {keys} fact keys "
          f"({writers // keys} racing on each)")

    started = time.time()
    ok = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=writers) as ex:
        futures = [
            ex.submit(writer, fact_keys[i % keys], i)
            for i in range(writers)
        ]
        for f in as_completed(futures):
            try:
                f.result()
                ok += 1
            except Exception as exc:
                failed += 1
                print(f"  FAILED: {type(exc).__name__}: {str(exc)[:120]}")
    elapsed = time.time() - started

    print(f"\n{ok} committed, {failed} failed, in {elapsed:.2f}s "
          f"({ok / elapsed:.1f} memory writes/sec)")

    # ------------------------------------------------------------ invariants
    print("\ninvariants:")
    bad = 0

    dupes = query(
        """
        SELECT fact_key, version, count(*) AS n
          FROM fact WHERE source = 'contention-test'
         GROUP BY fact_key, version HAVING count(*) > 1
        """
    )
    print(f"  duplicate (fact_key, version) pairs: {len(dupes)}")
    bad += len(dupes)

    density = query(
        """
        SELECT fact_key, count(*) AS versions, max(version) AS highest
          FROM fact WHERE source = 'contention-test'
         GROUP BY fact_key ORDER BY fact_key
        """
    )
    gaps = [r for r in density if r["versions"] != r["highest"]]
    print(f"  fact keys with version gaps: {len(gaps)}")
    for r in density:
        marker = "  <-- GAP" if r["versions"] != r["highest"] else ""
        print(f"    {r['fact_key']}: {r['versions']} versions, highest v{r['highest']}{marker}")
    bad += len(gaps)

    believed = query(
        """
        SELECT fact_key, count(*) AS n
          FROM fact WHERE source = 'contention-test' AND retracted_at IS NULL
         GROUP BY fact_key
        """
    )
    multi = [r for r in believed if r["n"] != 1]
    print(f"  fact keys with != 1 believed version: {len(multi)}")
    bad += len(multi)

    # No decision may ever reference a version that does not exist. The foreign
    # key enforces this, so this is a check that the FK is actually doing its
    # job rather than being silently unvalidated.
    orphans = query(
        """
        SELECT count(*) AS n
          FROM decision_read dr
          LEFT JOIN fact f ON f.fact_key = dr.fact_key AND f.version = dr.fact_version
         WHERE f.fact_key IS NULL
        """
    )[0]["n"]
    print(f"  provenance rows pointing at a missing version: {orphans}")
    bad += orphans

    print("\n" + ("CONTENTION TEST PASSED" if bad == 0 and failed == 0
                  else f"FAILED: {bad} invariant violations, {failed} write failures"))
    close_pool()
    return 0 if (bad == 0 and failed == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
