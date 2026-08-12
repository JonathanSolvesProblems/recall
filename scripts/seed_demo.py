"""Seed the demo scenario: a cohort of people dispensed one recalled lot.

    python scripts/seed_demo.py [--cloud] [--patients 12]

The sweep's whole point is that it resolves a changed fact to *named people*.
With one patient that is a mechanism; with a cohort it is the thing the FDA's
own recall process cannot do, which is the argument the demo makes.

Patient data is synthetic and disclosed as such in the README. The FDA side is
entirely real: the drug, the lot, the recall and its termination date all come
from openFDA.

The agent is asked once and the answer replicated across the cohort. Everyone
asking the same question about the same lot genuinely does get the same answer,
so replicating it is faithful rather than a shortcut, and it keeps the seeding
cost to a single model call. The sweep afterwards re-decides every one of them
for real.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SUBJECT = "amlodipine-besylate-and-benazepril-hydrochloride"
LOT = "GB01616"
QUESTION = f"My amlodipine and benazepril is from lot {LOT}. Is it safe to keep taking?"

# Ordinary names, because the correction list is the emotional beat of the demo
# and "Patient 07" undoes that.
NAMES = [
    "Margaret Hale", "Daniel Okafor", "Priya Raman", "Tomas Alvarez",
    "Ruth Bernstein", "Wei Chen", "Aisha Farouk", "Colm Doherty",
    "Elena Popescu", "Samuel Adeyemi", "Nora Lindqvist", "Hiro Tanaka",
    "Fatima Zahra", "Peter Novak", "Grace Mwangi", "Lucas Moreau",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cloud", action="store_true", help="target the hosted cluster")
    ap.add_argument("--patients", type=int, default=12)
    args = ap.parse_args()

    if args.cloud:
        dsn = re.search(
            r"^UNSAY_CLOUD_DSN=(.+)$",
            pathlib.Path(".env").read_text(encoding="utf-8"), re.M,
        ).group(1).strip()
        os.environ["UNSAY_DSN"] = dsn

    from unsay import agent, memory  # noqa: E402
    from unsay.db import close_pool, query, run_in_txn  # noqa: E402

    names = NAMES[: args.patients]

    def clear(conn):
        conn.execute("DELETE FROM outbox")
        conn.execute("DELETE FROM correction")
        conn.execute("DELETE FROM decision_read")
        conn.execute("DELETE FROM decision")
        conn.execute("DELETE FROM dispense")
        conn.execute("DELETE FROM fact WHERE source_ref = 'DEMO-ESCALATION'")
        # Put the lot's claim back to its pre-escalation state so the demo can
        # be run repeatedly without rebuilding the corpus.
        conn.execute(
            "UPDATE fact SET retracted_at = NULL WHERE subject_id = %s AND version = 1",
            (SUBJECT,),
        )
        conn.execute("DELETE FROM patient")
    run_in_txn(clear, label="clear_demo")
    print("cleared previous demo state")

    ids = []
    for i, name in enumerate(names):
        handle = name.lower().replace(" ", ".")

        def add(conn, n=name, h=handle, idx=i):
            pid = conn.execute(
                "INSERT INTO patient (mrn, display_name, contact) VALUES (%s,%s,%s)"
                " RETURNING patient_id",
                (f"MRN-{2000+idx}", n, f"{h}@example.test"),
            ).fetchone()["patient_id"]
            conn.execute(
                """
                INSERT INTO dispense (patient_id, drug_name, subject_id, lot_number, quantity, dispensed_at)
                VALUES (%s, 'Amlodipine/Benazepril 10-20mg', %s, %s, 30, now() - INTERVAL '21 days')
                """,
                (pid, SUBJECT, LOT),
            )
            return pid
        ids.append(str(run_in_txn(add, label="add_patient")))
    print(f"seeded {len(ids)} patients, each dispensed lot {LOT}")

    # One real call to the model; the cohort shares its answer and read set.
    answer = agent.ask(QUESTION, subject_id=SUBJECT, persist=False)
    claims = memory.retrieve(QUESTION, subject_id=SUBJECT, k=8)
    print(f"agent verdict for the cohort: {answer.verdict.upper()}")
    if answer.verdict == "stop":
        print("  WARNING: already STOP before escalation. The demo needs a "
              "non-stop opening verdict; check the lot's claim was reset.")

    for pid in ids:
        memory.record_decision(
            question=QUESTION, answer=answer.answer, verdict=answer.verdict,
            confidence=answer.confidence, model_id=answer.decision_id or "seed",
            retrieved=claims, cited=answer.cited or {c.fact_key for c in claims},
            patient_id=pid,
        )

    standing = query("SELECT count(*) AS n FROM decision WHERE status='standing'")[0]["n"]
    print(f"{standing} standing answers now rest on lot {LOT}")
    print("\nnext: publish the Class I escalation, then run the sweep")
    close_pool()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
