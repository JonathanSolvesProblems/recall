"""Retroactive invalidation: going back and repairing answers that have gone wrong.

This is the part of Recall that no vector store can do, and the reason the
memory layer is a database rather than an index.

When the FDA publishes a recall, some number of answers already given are now
wrong. They were correct when given. Nothing about them has changed; the world
moved underneath them. Finding them requires knowing which *version* of which
claim each past answer stood on, which is a join across three tables:

    decision  ->  decision_read  ->  fact

A vector database cannot express this query. It has no notion that a memory
has versions, and no record of which version a given answer consumed. The most
it can do is return today's nearest neighbours, which tells you nothing about
what you said in March.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

import psycopg

from recall.db import query, run_in_txn

log = logging.getLogger(__name__)


@dataclass
class Candidate:
    """A standing answer that rests on at least one superseded claim."""

    decision_id: str
    question: str
    answer: str
    verdict: str
    patient_id: str | None
    stale: list[dict]


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

CANDIDATE_SQL = """
    SELECT d.decision_id,
           d.question,
           d.answer,
           d.verdict,
           d.patient_id,
           json_agg(json_build_object(
               'fact_key',    dr.fact_key,
               'read_version', dr.fact_version,
               'claim_as_read', stale_f.claim,
               'severity_as_read', stale_f.severity
           )) AS stale
      FROM decision d
      JOIN decision_read dr   ON dr.decision_id = d.decision_id
      JOIN fact stale_f       ON stale_f.fact_key = dr.fact_key
                             AND stale_f.version  = dr.fact_version
     WHERE d.status = 'standing'
       AND dr.load_bearing
       AND stale_f.retracted_at IS NOT NULL
       AND (%s::UUID IS NULL OR d.decision_id > %s::UUID)
       {extra}
     GROUP BY d.decision_id, d.question, d.answer, d.verdict, d.patient_id
     ORDER BY d.decision_id
     LIMIT %s
"""

# Keyset pagination rather than OFFSET. A recall on a widely dispensed drug can
# implicate a very large number of standing answers, and OFFSET makes the
# database re-scan and discard everything already processed on every page, so
# the sweep gets quadratically slower exactly when it matters most.
BATCH = 500


def find_candidates(
    *,
    subject_id: str | None = None,
    after: str | None = None,
    batch: int = BATCH,
) -> list[Candidate]:
    """One page of standing answers that leaned on a claim which has since changed.

    Restricted to load-bearing reads. A decision that merely had a claim in its
    context window, without relying on it, is not invalidated when that claim
    moves, and treating it as invalidated is how a sweep turns into spam.

    Ordered by decision_id so the caller can page through with ``after``.
    """
    extra = ""
    params: list[Any] = [after, after]
    if subject_id:
        extra = "AND stale_f.subject_id = %s"
        params.append(subject_id)
    params.append(batch)

    sql = CANDIDATE_SQL.format(extra=extra)

    return [
        Candidate(
            decision_id=str(r["decision_id"]), question=r["question"], answer=r["answer"],
            verdict=r["verdict"], patient_id=str(r["patient_id"]) if r["patient_id"] else None,
            stale=r["stale"],
        )
        for r in query(sql, tuple(params))
    ]


# ---------------------------------------------------------------------------
# Repair
# ---------------------------------------------------------------------------


def dedupe_key(decision_id: str, new_verdict: str, stale: list[dict]) -> str:
    """Deterministic identity for "this correction, for this reason".

    Recomputed identically by a retry after a node or region loss, so the
    unique constraint on outbox.dedupe_key turns the replay into a no-op. This
    is what makes exactly-once delivery a property of the schema rather than an
    assumption about how the process exits.
    """
    material = "\x1f".join(
        [decision_id, new_verdict]
        + sorted(f"{s['fact_key']}@{s['read_version']}" for s in stale)
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def open_sweep(trigger_kind: str, trigger_ref: str) -> str:
    def work(conn: psycopg.Connection) -> str:
        row = conn.execute(
            "INSERT INTO sweep (trigger_kind, trigger_ref) VALUES (%s, %s) RETURNING sweep_id",
            (trigger_kind, trigger_ref),
        ).fetchone()
        return str(row["sweep_id"])

    return run_in_txn(work, label="open_sweep")


def apply_correction(
    *,
    sweep_id: str,
    candidate: Candidate,
    new_verdict: str,
    new_answer: str,
    notify: bool = True,
) -> bool:
    """Record a re-evaluation and, if the verdict moved, queue one notification.

    Everything here happens in one transaction: the correction row, the status
    change on the original decision, the outbox entry, and the audit line. A
    region can be lost at any point during this and the result is either all of
    it or none of it. There is no state in which a patient is told their drug
    was recalled while the system has no record of having said so.

    Returns True when a notification was newly queued.
    """
    changed = new_verdict != candidate.verdict
    key = dedupe_key(candidate.decision_id, new_verdict, candidate.stale)

    def work(conn: psycopg.Connection) -> bool:
        conn.execute(
            """
            INSERT INTO correction (sweep_id, decision_id, prior_verdict, new_verdict,
                                    prior_answer, new_answer, changed)
            VALUES (%s, %s, %s, %s, %s, %s, %s::JSONB)
            ON CONFLICT (sweep_id, decision_id) DO NOTHING
            """,
            (sweep_id, candidate.decision_id, candidate.verdict, new_verdict,
             candidate.answer, new_answer, json.dumps(candidate.stale)),
        )

        conn.execute(
            "UPDATE decision SET status = %s WHERE decision_id = %s",
            ("reversed" if changed else "reaffirmed", candidate.decision_id),
        )

        queued = False
        if changed and notify and candidate.patient_id:
            contact = conn.execute(
                "SELECT contact, display_name FROM patient WHERE patient_id = %s",
                (candidate.patient_id,),
            ).fetchone()

            if contact:
                cur = conn.execute(
                    """
                    INSERT INTO outbox (dedupe_key, channel, recipient, payload)
                    VALUES (%s, 'email', %s, %s::JSONB)
                    ON CONFLICT (dedupe_key) DO NOTHING
                    RETURNING outbox_id
                    """,
                    (key, contact["contact"], json.dumps({
                        "decision_id": candidate.decision_id,
                        "name": contact["display_name"],
                        "prior_verdict": candidate.verdict,
                        "new_verdict": new_verdict,
                        "message": new_answer,
                        "because": candidate.stale,
                    })),
                )
                queued = cur.fetchone() is not None

        conn.execute(
            """
            INSERT INTO memory_audit (actor, action, target, detail)
            VALUES ('sweep', %s, %s, %s::JSONB)
            """,
            ("reverse" if changed else "reaffirm", candidate.decision_id,
             json.dumps({"sweep_id": sweep_id, "notified": queued})),
        )
        return queued

    return run_in_txn(work, label="apply_correction")


def close_sweep(sweep_id: str, *, candidates: int, reevaluated: int, reversed_: int) -> dict:
    def work(conn: psycopg.Connection) -> dict:
        row = conn.execute(
            """
            UPDATE sweep
               SET finished_at = now(), state = 'done',
                   candidates = %s, reevaluated = %s, reversed = %s
             WHERE sweep_id = %s
            RETURNING sweep_id, started_at, finished_at, candidates, reevaluated, reversed,
                      extract(epoch FROM (finished_at - started_at)) AS seconds
            """,
            (candidates, reevaluated, reversed_, sweep_id),
        ).fetchone()
        return dict(row)

    return run_in_txn(work, label="close_sweep")


def run_sweep(
    *,
    reevaluate: Callable[[Candidate], tuple[str, str]],
    trigger_kind: str = "manual",
    trigger_ref: str = "",
    subject_id: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Find every affected answer, re-decide it, and queue the corrections.

    ``reevaluate`` takes a candidate and returns ``(verdict, answer)`` judged
    against current memory. It is injected so the sweep can be exercised
    deterministically in tests and driven by Bedrock in production.
    """
    sweep_id = open_sweep(trigger_kind, trigger_ref)

    seen = 0
    reevaluated = 0
    reversed_ = 0
    notified = 0
    after: str | None = None

    # Paged rather than loaded whole. The candidate set for a recall on a
    # widely dispensed drug does not necessarily fit in memory, and holding it
    # all before doing any work delays the first correction by the time it
    # takes to enumerate the last one.
    while True:
        page = find_candidates(subject_id=subject_id, after=after)
        if not page:
            break

        for candidate in page:
            if limit is not None and seen >= limit:
                break
            seen += 1
            verdict, answer = reevaluate(candidate)
            reevaluated += 1
            if verdict != candidate.verdict:
                reversed_ += 1
            if apply_correction(
                sweep_id=sweep_id, candidate=candidate,
                new_verdict=verdict, new_answer=answer,
            ):
                notified += 1

        after = page[-1].decision_id
        if limit is not None and seen >= limit:
            break

    log.info("sweep %s: %d candidate decisions", sweep_id, seen)

    summary = close_sweep(
        sweep_id, candidates=seen, reevaluated=reevaluated, reversed_=reversed_
    )
    summary["notified"] = notified
    log.info("sweep %s complete: %s", sweep_id, summary)
    return summary
