"""Demo scenario state: seeding it, and putting it back.

A hosted demo is not a script that runs once. Judging runs for four weeks and
the sweep is destructive by design: it moves every standing answer to
`reversed`, so the second visitor to click "Run sweep" sees nothing happen and
concludes the flagship is broken.

So the scenario is restorable, and the UI restores it automatically when it
finds the demo spent. Nobody should have to know that the person before them
already used it.

The FDA side is real throughout: real drug, real lot, real recall, real
termination date. Only the patients are synthetic, which the README says.
"""

from __future__ import annotations

import logging

import psycopg

from unsay import agent, memory
from unsay.db import query, run_in_txn

log = logging.getLogger(__name__)

SUBJECT = "amlodipine-besylate-and-benazepril-hydrochloride"
LOT = "GB01616"
QUESTION = f"My amlodipine and benazepril is from lot {LOT}. Is it safe to keep taking?"

NAMES = [
    "Margaret Hale", "Daniel Okafor", "Priya Raman", "Tomas Alvarez",
    "Ruth Bernstein", "Wei Chen", "Aisha Farouk", "Colm Doherty",
    "Elena Popescu", "Samuel Adeyemi", "Nora Lindqvist", "Hiro Tanaka",
    "Fatima Zahra", "Peter Novak", "Grace Mwangi", "Lucas Moreau",
]


def is_spent() -> bool:
    """True when the scenario has been used and would do nothing if run again."""
    row = query(
        """
        SELECT (SELECT count(*) FROM decision WHERE status = 'standing') AS standing,
               (SELECT count(*) FROM correction)                         AS corrections
        """
    )[0]
    return row["standing"] == 0 or row["corrections"] > 0


def _clear(conn: psycopg.Connection) -> None:
    conn.execute("DELETE FROM outbox")
    conn.execute("DELETE FROM correction")
    conn.execute("DELETE FROM decision_read")
    conn.execute("DELETE FROM decision")
    conn.execute("DELETE FROM dispense")
    conn.execute("DELETE FROM patient")
    # Drop the escalation and un-retract the claim it superseded, so the
    # scenario starts from the same believed version every time.
    conn.execute("DELETE FROM fact WHERE source_ref = 'DEMO-ESCALATION'")
    conn.execute(
        "UPDATE fact SET retracted_at = NULL WHERE subject_id = %s AND version = 1",
        (SUBJECT,),
    )


def reset(patients: int = 12) -> dict:
    """Restore the scenario to its opening state.

    One model call, replicated across the cohort. Everyone asking the same
    question about the same lot genuinely receives the same answer, so sharing
    it is faithful rather than a shortcut, and it keeps a reset to a single
    Bedrock round trip instead of twelve. The sweep afterwards re-decides every
    one of them for real.
    """
    run_in_txn(_clear, label="demo_clear")

    names = NAMES[:patients]
    ids: list[str] = []
    for idx, name in enumerate(names):
        handle = name.lower().replace(" ", ".")

        def add(conn, n=name, h=handle, i=idx):
            pid = conn.execute(
                "INSERT INTO patient (mrn, display_name, contact) VALUES (%s,%s,%s)"
                " RETURNING patient_id",
                (f"MRN-{2000+i}", n, f"{h}@example.test"),
            ).fetchone()["patient_id"]
            conn.execute(
                """
                INSERT INTO dispense (patient_id, drug_name, subject_id, lot_number,
                                      quantity, dispensed_at)
                VALUES (%s, 'Amlodipine/Benazepril 10-20mg', %s, %s, 30,
                        now() - INTERVAL '21 days')
                """,
                (pid, SUBJECT, LOT),
            )
            return pid

        ids.append(str(run_in_txn(add, label="demo_patient")))

    answer = agent.ask(QUESTION, subject_id=SUBJECT, persist=False)
    claims = memory.retrieve(QUESTION, subject_id=SUBJECT, k=8)
    cited = answer.cited or {c.fact_key for c in claims}

    for pid in ids:
        memory.record_decision(
            question=QUESTION, answer=answer.answer, verdict=answer.verdict,
            confidence=answer.confidence, model_id="demo-reset",
            retrieved=claims, cited=cited, patient_id=pid,
        )

    log.info("demo reset: %d patients, opening verdict %s", len(ids), answer.verdict)
    return {
        "patients": len(ids),
        "verdict": answer.verdict,
        "subject": SUBJECT,
        "lot": LOT,
    }
