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

# Real patients do not ask in chorus. Four phrasings so the sweep produces four
# genuinely different re-decisions rather than one answer repeated twelve times,
# which reads as a mail-merge and undersells what actually happened. Still
# memoised per distinct question, so a sweep costs four model calls, not twelve.
QUESTIONS = [
    QUESTION,
    f"I take amlodipine/benazepril for blood pressure, lot {LOT}. Should I stop?",
    # Not "was it recalled?": that reads as a yes/no about the recall existing
    # and opens at STOP even when the recall has terminated, which is a
    # different scenario from the caution-to-stop reversal this demo shows.
    f"Should I be worried about my blood pressure tablets, lot {LOT}?",
    f"Is there any problem with lot {LOT} of amlodipine and benazepril?",
]

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

    Patients are spread across four phrasings of the same concern. Everyone
    asking a given phrasing about the same lot genuinely receives the same
    answer, so sharing it within a phrasing is faithful rather than a shortcut,
    and a reset costs four Bedrock round trips the first time and none
    afterwards. The sweep re-decides all of them against current memory.
    """
    # The opening answer is the same every time: same question, same drug, same
    # claims, temperature pinned at 0. Re-asking the model on every reset would
    # spend a Bedrock call each time the page auto-restores, which on a public
    # URL is an open tap. It is cached after the first reset and only recomputed
    # if the cache is missing.
    baselines = [_cached_baseline(q, i) for i, q in enumerate(QUESTIONS)]

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

    claims = memory.retrieve(QUESTION, subject_id=SUBJECT, k=8)

    for n, pid in enumerate(ids):
        b = baselines[n % len(baselines)]
        memory.record_decision(
            question=QUESTIONS[n % len(QUESTIONS)], answer=b["answer"],
            verdict=b["verdict"], confidence=b["confidence"], model_id="demo-reset",
            retrieved=claims, cited=set(b["cited"]) or {c.fact_key for c in claims},
            patient_id=pid,
        )

    baseline = baselines[0]
    log.info("demo reset: %d patients across %d phrasings, opening verdict %s",
             len(ids), len(QUESTIONS), baseline["verdict"])
    return {
        "patients": len(ids),
        "verdict": baseline["verdict"],
        "subject": SUBJECT,
        "lot": LOT,
        "model_calls": baseline["model_calls"],
    }


BASELINE_DDL = """
CREATE TABLE IF NOT EXISTS demo_baseline (
    id         INT2 PRIMARY KEY,
    verdict    STRING NOT NULL,
    answer     STRING NOT NULL,
    confidence FLOAT8 NOT NULL,
    cited      JSONB  NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def _cached_baseline(question: str, slot: int) -> dict:
    """The opening answer, computed once and reused by every later reset.

    Deterministic by construction: same question, same drug, same believed
    claims, temperature pinned at 0. Asking the model again on every reset
    would spend a Bedrock call each time the page auto-restores itself, and on
    an unauthenticated public URL that is an open tap for anyone reloading.
    """
    import json as _json

    from unsay.db import query

    run_in_txn(lambda c: c.execute(BASELINE_DDL), label="baseline_ddl")
    rows = query(
        "SELECT verdict, answer, confidence, cited FROM demo_baseline WHERE id = %s",
        (slot,),
    )
    if rows:
        r = rows[0]
        cited = r["cited"] if isinstance(r["cited"], list) else _json.loads(r["cited"])
        return {**r, "cited": cited, "model_calls": 0}

    a = agent.ask(question, subject_id=SUBJECT, persist=False)
    cited = sorted(a.cited)

    def store(conn):
        conn.execute(
            """
            INSERT INTO demo_baseline (id, verdict, answer, confidence, cited)
            VALUES (%s, %s, %s, %s, %s::JSONB)
            ON CONFLICT (id) DO UPDATE SET verdict = excluded.verdict,
                answer = excluded.answer, confidence = excluded.confidence,
                cited = excluded.cited
            """,
            (slot, a.verdict, a.answer, a.confidence, _json.dumps(cited)),
        )

    run_in_txn(store, label="baseline_store")
    return {"verdict": a.verdict, "answer": a.answer, "confidence": a.confidence,
            "cited": cited, "model_calls": 1}
