"""Memory retrieval, provenance capture, and point-in-time reconstruction.

The contract this module enforces: an answer and the record of what produced
it are written in a single transaction. There is no code path that stores a
decision without its read set, because a decision whose provenance was lost
can never be repaired later, and a memory system that can lose provenance
under load has effectively no provenance at all.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg

from unsay import embeddings
from unsay.db import query, query_as_of, run_in_txn

log = logging.getLogger(__name__)


@dataclass
class Retrieved:
    """A claim pulled from memory, carrying the version that was read."""

    fact_key: str
    version: int
    subject_id: str
    predicate: str
    claim: str
    severity: str
    valid_from: datetime
    valid_to: datetime | None
    similarity: float
    rank: int


SEVERITY_RANK = {
    "class_i": 0, "boxed_warning": 1, "class_ii": 2,
    "class_iii": 3, "warning": 4, "info": 5,
}

# At most this many claims about the same drug may occupy the top-K.
#
# Each recalled lot is its own claim, so a drug with a long recall history can
# take every slot: an untuned query for "blood pressure medication" came back
# with five chlorpromazine lots out of eight. The model then reasons over one
# drug's history instead of the several candidates the question implied.
PER_SUBJECT_CAP = 2

# How much wider to search before capping, so diversification has spare
# candidates to promote rather than simply returning fewer rows.
OVERFETCH = 4


def _diversify(rows: list[dict], k: int) -> list[dict]:
    """Trim to k, allowing at most PER_SUBJECT_CAP claims per drug.

    Rows arrive sorted by similarity. Anything over the cap is set aside rather
    than discarded, and used to backfill if capping leaves fewer than k, so an
    open question against a corpus dominated by one drug still returns a full
    context window instead of a short one.
    """
    kept: list[dict] = []
    overflow: list[dict] = []
    seen: dict[str, int] = {}

    for r in rows:
        subject = r["subject_id"]
        if seen.get(subject, 0) < PER_SUBJECT_CAP:
            seen[subject] = seen.get(subject, 0) + 1
            kept.append(r)
        else:
            overflow.append(r)
        if len(kept) == k:
            return kept

    return (kept + overflow)[:k]


def retrieve(question: str, *, subject_id: str | None = None, k: int = 8) -> list[Retrieved]:
    """Semantic search across currently-believed claims.

    The `believed` prefix on the vector index means retracted versions are not
    candidates, so all k slots go to claims the system currently holds true.
    """
    vector = embeddings.to_sql(embeddings.embed(question))

    # A named drug is already the tightest possible filter, so diversification
    # would only throw away lots of the very drug being asked about. Overfetch
    # and capping apply to open questions only.
    fetch = k if subject_id else k * OVERFETCH

    if subject_id:
        # A named drug is a hard filter, not a hint. Semantic similarity will
        # happily return warnings about a different drug that reads similarly,
        # and in this domain that is the worst possible failure.
        rows = query(
            """
            SELECT fact_key, version, subject_id, predicate, claim, severity,
                   valid_from, valid_to,
                   1 - (embedding <=> %s::VECTOR) AS similarity
              FROM fact
             WHERE believed AND subject_id = %s
             ORDER BY embedding <=> %s::VECTOR
             LIMIT %s
            """,
            (vector, subject_id, vector, fetch),
        )
    else:
        rows = query(
            """
            SELECT fact_key, version, subject_id, predicate, claim, severity,
                   valid_from, valid_to,
                   1 - (embedding <=> %s::VECTOR) AS similarity
              FROM fact
             WHERE believed
             ORDER BY embedding <=> %s::VECTOR
             LIMIT %s
            """,
            (vector, vector, fetch),
        )
        rows = _diversify(rows, k)

    out = [
        Retrieved(
            fact_key=r["fact_key"], version=r["version"], subject_id=r["subject_id"],
            predicate=r["predicate"], claim=r["claim"], severity=r["severity"],
            valid_from=r["valid_from"], valid_to=r["valid_to"],
            similarity=float(r["similarity"]), rank=0,
        )
        for r in rows
    ]
    # Surface the most dangerous claims first regardless of cosine distance.
    # A Class I recall that ranks 6th on similarity still belongs at the top of
    # a safety answer.
    out.sort(key=lambda r: (SEVERITY_RANK.get(r.severity, 9), -r.similarity))
    for i, r in enumerate(out, start=1):
        r.rank = i
    return out


def record_decision(
    *,
    question: str,
    answer: str,
    verdict: str,
    confidence: float,
    model_id: str,
    retrieved: list[Retrieved],
    cited: set[str],
    patient_id: str | None = None,
) -> dict[str, Any]:
    """Persist an answer together with its exact read set, atomically.

    ``cited`` holds the fact keys the model actually leaned on. Reads that were
    merely shown to the model are still recorded, but marked non-load-bearing,
    so a later change to background context does not spuriously invalidate an
    answer that never depended on it.
    """
    vector = embeddings.to_sql(embeddings.embed(f"{question}\n{answer}"))

    def work(conn: psycopg.Connection) -> dict[str, Any]:
        row = conn.execute(
            """
            INSERT INTO decision (patient_id, question, answer, verdict, confidence,
                                  model_id, embedding)
            VALUES (%s, %s, %s, %s, %s, %s, %s::VECTOR)
            RETURNING decision_id, decided_at, read_hlc
            """,
            (patient_id, question, answer, verdict, confidence, model_id, vector),
        ).fetchone()

        for r in retrieved:
            conn.execute(
                """
                INSERT INTO decision_read (decision_id, fact_key, fact_version,
                                           rank, similarity, load_bearing)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (row["decision_id"], r.fact_key, r.version, r.rank,
                 r.similarity, r.fact_key in cited),
            )

        conn.execute(
            """
            INSERT INTO memory_audit (actor, action, target, detail)
            VALUES ('agent', 'decide', %s, %s::JSONB)
            """,
            (str(row["decision_id"]),
             json.dumps({"verdict": verdict, "reads": len(retrieved), "cited": len(cited)})),
        )
        return dict(row)

    return run_in_txn(work, label="record_decision")


# ---------------------------------------------------------------------------
# Point-in-time reconstruction
# ---------------------------------------------------------------------------


def facts_as_believed_at(moment: datetime, *, subject_id: str | None = None) -> list[dict]:
    """Reconstruct what the system believed at an arbitrary past instant.

    This is the exact, unbounded answer, derived from the bitemporal columns
    rather than from MVCC. It works for a decision made a year ago, long after
    the garbage-collection window has closed over the underlying versions.

    A version was believed at time T if it had been asserted by then and had
    not yet been retracted by then.
    """
    sql = """
        SELECT fact_key, version, subject_id, predicate, claim, severity,
               valid_from, valid_to, asserted_at, retracted_at
          FROM fact
         WHERE asserted_at <= %s
           AND (retracted_at IS NULL OR retracted_at > %s)
    """
    params: list[Any] = [moment, moment]
    if subject_id:
        sql += " AND subject_id = %s"
        params.append(subject_id)
    sql += " ORDER BY severity, fact_key"
    return query(sql, tuple(params))


def replay_decision(decision_id: str) -> dict[str, Any]:
    """Reconstruct a past answer and show how its evidence has moved since.

    Returns the decision, the exact versions it read, and for each one whether
    that version is still believed or has been superseded. This is the
    "what did the agent know, and when did it know it" view.
    """
    decision = query(
        """
        SELECT decision_id, patient_id, question, answer, verdict, confidence,
               model_id, decided_at, read_hlc, status
          FROM decision
         WHERE decision_id = %s
        """,
        (decision_id,),
    )
    if not decision:
        raise LookupError(f"no such decision: {decision_id}")
    d = decision[0]

    reads = query(
        """
        SELECT dr.fact_key, dr.fact_version, dr.rank, dr.similarity, dr.load_bearing,
               was.claim      AS claim_as_read,
               was.severity   AS severity_as_read,
               was.retracted_at,
               now_f.version  AS current_version,
               now_f.claim    AS claim_now,
               now_f.severity AS severity_now
          FROM decision_read dr
          JOIN fact was
            ON was.fact_key = dr.fact_key AND was.version = dr.fact_version
          LEFT JOIN LATERAL (
                SELECT version, claim, severity
                  FROM fact f2
                 WHERE f2.fact_key = dr.fact_key AND f2.retracted_at IS NULL
                 ORDER BY f2.version DESC
                 LIMIT 1
          ) now_f ON true
         WHERE dr.decision_id = %s
         ORDER BY dr.rank
        """,
        (decision_id,),
    )

    return {
        "decision": d,
        "reads": reads,
        "stale_reads": [r for r in reads if r["retracted_at"] is not None],
    }


def replay_via_mvcc(decision_id: str) -> list[dict]:
    """The same reconstruction taken straight from MVCC, via AS OF SYSTEM TIME.

    Exact and requiring no bitemporal bookkeeping at all, but only valid inside
    the garbage-collection window (25 hours here). Unsay uses this as a fast
    path for same-day forensics and relies on ``facts_as_believed_at`` beyond
    it. Both should agree inside the window, and the test suite asserts that.
    """
    rows = query("SELECT read_hlc FROM decision WHERE decision_id = %s", (decision_id,))
    if not rows:
        raise LookupError(f"no such decision: {decision_id}")
    hlc = str(rows[0]["read_hlc"])

    return query_as_of(
        """
        SELECT f.fact_key, f.version, f.claim, f.severity
          FROM decision_read dr
          JOIN fact f ON f.fact_key = dr.fact_key AND f.version = dr.fact_version
         WHERE dr.decision_id = %s
         ORDER BY dr.rank
        """,
        hlc,
        (decision_id,),
    )
