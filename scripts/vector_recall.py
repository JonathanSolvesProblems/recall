"""Does the vector index return the same neighbours a full scan would?

An approximate index is allowed to miss. At 554 claims that is unlikely to
matter, but "unlikely" is not a measurement, and the README claims the
`believed` prefix costs no recall. This checks it the only way that settles it:
run the same top-K twice, once through the index and once with the index
disabled so the planner has to scan, and compare the ordered results.

Usage:
    python scripts/vector_recall.py            # against UNSAY_DSN
    python scripts/vector_recall.py --cloud    # against UNSAY_CLOUD_DSN
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dotenv  # noqa: E402
import psycopg  # noqa: E402

QUERIES = [
    "amlodipine benazepril lot GB01616 recall",
    "is my blood pressure medication recalled",
    "NDMA impurity above acceptable intake limit",
    "sterility failure injectable product",
    "labelling mix-up wrong strength tablets",
]
K = 8


def topk_d(cur, vec: str, use_index: bool) -> list[tuple]:
    """Same as topk, but carrying the distance so a miss can be sized."""
    src = "fact" if use_index else "fact@primary"
    cur.execute(
        f"SELECT fact_key, version, embedding <=> %s::VECTOR AS d FROM {src} "
        "WHERE believed ORDER BY 3 LIMIT %s",
        (vec, K),
    )
    return cur.fetchall()


def topk(cur, vec: str, use_index: bool) -> list[tuple]:
    # Forcing the primary index is what makes the second run a control rather
    # than a restatement of the first: the planner cannot reach fact_semantic,
    # so it scans every believed row and sorts the distances exactly. There is
    # no session flag for this in v26.2, hence the hint.
    src = "fact" if use_index else "fact@primary"
    cur.execute(
        f"SELECT fact_key, version FROM {src} WHERE believed "
        "ORDER BY embedding <=> %s::VECTOR LIMIT %s",
        (vec, K),
    )
    return cur.fetchall()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cloud", action="store_true")
    args = ap.parse_args()

    dotenv.load_dotenv(dotenv_path=".env")
    dsn = os.environ["UNSAY_CLOUD_DSN" if args.cloud else "UNSAY_DSN"]

    from unsay import embeddings

    exact, worst = 0, 0.0
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        for q in QUERIES:
            v = embeddings.embed(q)
            vec = "[" + ",".join(f"{x:.6f}" for x in v) + "]"
            idx, scan = topk_d(cur, vec, True), topk_d(cur, vec, False)
            same = [r[:2] for r in idx] == [r[:2] for r in scan]
            exact += same
            missed = {r[:2] for r in scan} - {r[:2] for r in idx}
            print(f"  {'exact ' if same else 'DIFFER'}  {K - len(missed)}/{K} shared  {q}")
            for key, ver in missed:
                # How much worse is what the index returned instead? On an
                # approximate index the answer that matters is not "did it
                # differ" but "by how far", and a tie at the tail costs nothing.
                d_true = next(r[2] for r in scan if r[:2] == (key, ver))
                d_got = idx[-1][2]
                worst = max(worst, d_got - d_true)
                print(f"      missed {key[:46]} at distance {d_true:.6f};"
                      f" index's last is {d_got:.6f}, a gap of {d_got - d_true:.6f}")

    print(f"\n{exact}/{len(QUERIES)} queries returned an identical ordered top-{K}.")
    print(f"Largest distance penalty on any substituted neighbour: {worst:.6f}.")
    if worst > 0.01:
        print("That is a real recall loss, not a tie. Do not describe it as one.")
        return 1
    print("PASSED: substitutions only ever happen between near-tied neighbours.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
