"""End-to-end exercise of the flagship path against a live cluster.

    RECALL_ALLOW_FAKE_EMBEDDINGS=1 python scripts/smoke.py

Walks the full lifecycle: assert a claim, answer a question against it with
provenance, let the world change, sweep, and verify the correction plus
exactly-once notification. Uses stub embeddings so it runs without AWS.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RECALL_ALLOW_FAKE_EMBEDDINGS", "1")

from recall import embeddings, memory, sweep  # noqa: E402
from recall.db import close_pool, query, run_in_txn  # noqa: E402
from recall.ingest import Claim, assert_claim  # noqa: E402

OK = "  [ok]"


def reset() -> None:
    def work(conn):
        for table in ("outbox", "correction", "sweep", "memory_audit",
                      "decision_read", "decision", "dispense", "fact", "patient"):
            conn.execute(f"DELETE FROM {table}")
    run_in_txn(work, label="reset")
    print("reset: tables cleared")


def main() -> int:
    reset()

    # ---------------------------------------------------------------- setup
    def seed_patient(conn):
        return conn.execute(
            """
            INSERT INTO patient (mrn, display_name, contact)
            VALUES ('MRN-77', 'A. Patient', 'patient@example.test')
            RETURNING patient_id
            """
        ).fetchone()["patient_id"]

    patient_id = str(run_in_txn(seed_patient, label="seed_patient"))
    print(f"patient {patient_id}")

    # ------------------------------------------- 1. what we believed on day 1
    clean = Claim(
        fact_key="recall:valsartan:LOT-2291:none",
        subject_kind="lot", subject_id="valsartan", predicate="recall",
        claim="No open recall affects valsartan lot LOT-2291.",
        severity="info",
        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc), valid_to=None,
        source="openfda:enforcement", source_ref="seed",
    )
    v, changed = run_in_txn(
        lambda c: assert_claim(c, clean, embeddings.embed(clean.claim)), label="assert"
    )
    assert (v, changed) == (1, True), (v, changed)
    print(f"{OK} asserted v1")

    # Re-asserting identical content must NOT create a version.
    v2, changed2 = run_in_txn(
        lambda c: assert_claim(c, clean, embeddings.embed(clean.claim)), label="reassert"
    )
    assert (v2, changed2) == (1, False), (v2, changed2)
    print(f"{OK} identical re-ingest created no new version")

    # ------------------------------------------------ 2. the agent answers
    hits = memory.retrieve("Is my valsartan safe?", subject_id="valsartan", k=5)
    assert len(hits) == 1 and hits[0].version == 1, hits
    print(f"{OK} retrieved {len(hits)} believed claim(s)")

    rec = memory.record_decision(
        question="Is my valsartan safe to keep taking?",
        answer="Yes. Lot LOT-2291 has no open recall.",
        verdict="safe", confidence=0.94, model_id="smoke",
        retrieved=hits, cited={hits[0].fact_key}, patient_id=patient_id,
    )
    decision_id = str(rec["decision_id"])
    print(f"{OK} decision {decision_id} recorded with provenance")

    reads = query("SELECT * FROM decision_read WHERE decision_id = %s", (decision_id,))
    assert len(reads) == 1 and reads[0]["load_bearing"], reads
    print(f"{OK} read set persisted in the same transaction")

    # ------------------------------------------------- 3. the world changes
    recalled = Claim(
        fact_key="recall:valsartan:LOT-2291:none",
        subject_kind="lot", subject_id="valsartan", predicate="recall",
        claim=("Class I recall: valsartan lot LOT-2291 contains NDMA above the "
               "acceptable intake limit. Stop use and return to pharmacy."),
        severity="class_i",
        valid_from=datetime(2026, 8, 4, tzinfo=timezone.utc), valid_to=None,
        source="openfda:enforcement", source_ref="D-1234-2026",
    )
    v3, changed3 = run_in_txn(
        lambda c: assert_claim(c, recalled, embeddings.embed(recalled.claim)), label="supersede"
    )
    assert (v3, changed3) == (2, True), (v3, changed3)
    print(f"{OK} superseded to v2, v1 retracted")

    live = memory.retrieve("valsartan safety", subject_id="valsartan", k=5)
    assert len(live) == 1 and live[0].version == 2, live
    print(f"{OK} retrieval now returns only v2")

    # -------------------------------------- 4. point-in-time reconstruction
    replay = memory.replay_decision(decision_id)
    assert len(replay["stale_reads"]) == 1, replay["stale_reads"]
    assert replay["reads"][0]["current_version"] == 2
    print(f"{OK} replay shows the answer stood on v1, now superseded by v2")

    mvcc = memory.replay_via_mvcc(decision_id)
    assert len(mvcc) == 1 and mvcc[0]["version"] == 1, mvcc
    assert mvcc[0]["claim"] == clean.claim
    print(f"{OK} AS OF SYSTEM TIME replay agrees with bitemporal reconstruction")

    # ------------------------------------------------------- 5. THE SWEEP
    found = sweep.find_candidates()
    assert len(found) == 1 and found[0].decision_id == decision_id, found
    print(f"{OK} sweep found {len(found)} standing answer built on changed evidence")

    def reevaluate(c: sweep.Candidate) -> tuple[str, str]:
        return "stop", ("Correction: lot LOT-2291 is now under a Class I recall for "
                        "NDMA contamination. Stop taking it and contact your pharmacy.")

    summary = sweep.run_sweep(
        reevaluate=reevaluate, trigger_kind="test", trigger_ref="D-1234-2026"
    )
    assert summary["candidates"] == 1 and summary["reversed"] == 1, summary
    assert summary["notified"] == 1, summary
    print(f"{OK} sweep reversed 1 answer in {summary['seconds']:.3f}s and queued 1 notice")

    # -------------------------------- 6. exactly-once across a retried sweep
    again = sweep.run_sweep(
        reevaluate=reevaluate, trigger_kind="test-retry", trigger_ref="D-1234-2026"
    )
    outbox = query("SELECT count(*) AS n FROM outbox")[0]["n"]
    assert outbox == 1, f"expected exactly one notification, got {outbox}"
    print(f"{OK} sweep replayed; outbox still holds exactly 1 notice (dedupe held)")

    # The decision is no longer standing, so it is not a candidate twice.
    assert again["candidates"] == 0, again
    print(f"{OK} repaired answers are not re-swept")

    print("\nSMOKE PASSED")
    close_pool()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
