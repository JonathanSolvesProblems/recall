"""Turning openFDA records into bitemporal safety claims.

The whole of Recall's behaviour rests on one rule enforced here: a claim is
never updated in place. When the world changes, the version we believed is
retracted and a new version is asserted beside it. Nothing that any past
decision read is ever mutated or deleted.

That is what makes a decision from three months ago still explainable today,
and it is the difference between memory and a cache.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import psycopg

from recall import embeddings, openfda
from recall.db import run_in_txn

log = logging.getLogger(__name__)

SEVERITY_BY_CLASS = {
    "Class I": "class_i",
    "Class II": "class_ii",
    "Class III": "class_iii",
}


@dataclass
class Claim:
    """One atomic, safety-relevant assertion about a drug."""

    fact_key: str
    subject_kind: str
    subject_id: str
    predicate: str
    claim: str
    severity: str
    valid_from: datetime
    valid_to: datetime | None
    source: str
    source_ref: str
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        """Identity of the claim's *content*, used to detect real change.

        Deliberately excludes ingestion timestamps: re-running the ingester
        over unchanged FDA data must not manufacture new versions, because a
        spurious version would trigger a spurious sweep and, at the far end,
        a spurious message to a patient.
        """
        material = "\x1f".join(
            [
                self.fact_key,
                self.predicate,
                self.claim,
                self.severity,
                self.valid_from.isoformat(),
                self.valid_to.isoformat() if self.valid_to else "",
            ]
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# openFDA record mapping
# ---------------------------------------------------------------------------

_LOT_RE = re.compile(r"\b(?:lot|batch)\s*(?:#|no\.?|number)?\s*:?\s*([A-Z0-9][A-Z0-9\-]{2,})", re.I)


def _fda_date(value: str | None) -> datetime | None:
    """openFDA dates are YYYYMMDD strings."""
    if not value or len(value) != 8 or not value.isdigit():
        return None
    return datetime.strptime(value, "%Y%m%d").replace(tzinfo=timezone.utc)


def normalize_subject(name: str) -> str:
    """Collapse a drug name to a stable join key."""
    return re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")[:120]


def lots_in(text: str) -> list[str]:
    """Pull lot numbers out of free-text recall descriptions.

    Lot-level resolution is the gap the 2024 study on automated recall
    notification identified as blocking: prescriptions could not be traced to
    the lot actually dispensed. Extracting them is imperfect, so a recall with
    no parsable lot falls back to drug-level scope rather than silently
    matching nobody.
    """
    return sorted({m.group(1).upper() for m in _LOT_RE.finditer(text or "")})


def recall_to_claims(rec: dict) -> list[Claim]:
    """Map one openFDA enforcement record to one claim per affected lot."""
    desc = rec.get("product_description", "") or ""
    reason = rec.get("reason_for_recall", "") or ""
    firm = rec.get("recalling_firm", "") or "unknown firm"
    classification = rec.get("classification", "") or ""
    severity = SEVERITY_BY_CLASS.get(classification, "info")

    generic = (rec.get("openfda", {}) or {}).get("generic_name") or []
    brand = (rec.get("openfda", {}) or {}).get("brand_name") or []
    name = (generic or brand or [desc[:60]])[0]
    subject_id = normalize_subject(name)
    if not subject_id:
        return []

    valid_from = (
        _fda_date(rec.get("recall_initiation_date"))
        or _fda_date(rec.get("report_date"))
        or datetime.now(timezone.utc)
    )
    valid_to = _fda_date(rec.get("termination_date"))

    recall_number = rec.get("recall_number", "") or rec.get("event_id", "") or "unknown"
    lots = lots_in(f"{desc} {rec.get('code_info', '')}")
    scopes: list[tuple[str, str]] = (
        [("lot", lot) for lot in lots] if lots else [("drug", subject_id)]
    )

    claims = []
    for kind, scope in scopes:
        suffix = scope if kind == "lot" else "all-lots"
        text = (
            f"{classification or 'Recall'}: {name} ({desc[:160]}) recalled by {firm}. "
            f"Reason: {reason[:240]}. "
            f"Scope: {'lot ' + scope if kind == 'lot' else 'all lots'}. "
            f"Recall number {recall_number}, status {rec.get('status', 'unknown')}."
        )
        claims.append(
            Claim(
                fact_key=f"recall:{subject_id}:{suffix}:{recall_number}",
                subject_kind=kind,
                subject_id=subject_id,
                predicate="recall",
                claim=text,
                severity=severity,
                valid_from=valid_from,
                valid_to=valid_to,
                source="openfda:enforcement",
                source_ref=recall_number,
                payload={
                    "classification": classification,
                    "status": rec.get("status"),
                    "recalling_firm": firm,
                    "lot": scope if kind == "lot" else None,
                    "distribution_pattern": rec.get("distribution_pattern"),
                },
            )
        )
    return claims


def label_to_claims(rec: dict) -> list[Claim]:
    """Map one SPL label revision to its boxed warning, if it has one."""
    ofda = rec.get("openfda", {}) or {}
    names = ofda.get("generic_name") or ofda.get("brand_name") or []
    if not names:
        return []
    subject_id = normalize_subject(names[0])
    if not subject_id:
        return []

    boxed = rec.get("boxed_warning") or []
    if not boxed:
        return []

    set_id = rec.get("set_id") or rec.get("id") or "unknown"
    effective = _fda_date(rec.get("effective_time")) or datetime.now(timezone.utc)
    text = " ".join(boxed)[:1800]

    return [
        Claim(
            fact_key=f"boxed:{subject_id}:{set_id}",
            subject_kind="drug",
            subject_id=subject_id,
            predicate="boxed_warning",
            claim=f"Boxed warning for {names[0]}: {text}",
            severity="boxed_warning",
            # A label revision is true from its effective date until superseded
            # by a later revision, which ingestion discovers rather than knows.
            valid_from=effective,
            valid_to=None,
            source="openfda:label",
            source_ref=set_id,
            payload={"spl_version": rec.get("version"), "effective_time": rec.get("effective_time")},
        )
    ]


# ---------------------------------------------------------------------------
# Bitemporal write path
# ---------------------------------------------------------------------------


def assert_claim(conn: psycopg.Connection, claim: Claim, vector: list[float]) -> tuple[int, bool]:
    """Assert a claim, versioning it if its content has changed.

    Returns ``(version, changed)``. When ``changed`` is False the incoming
    record was byte-identical to what is already believed and nothing was
    written, which keeps a weekly re-ingest of 17.8k unchanged recalls from
    producing 17.8k spurious versions.
    """
    current = conn.execute(
        """
        SELECT version, content_hash
          FROM fact
         WHERE fact_key = %s AND retracted_at IS NULL
         ORDER BY version DESC
         LIMIT 1
        """,
        (claim.fact_key,),
    ).fetchone()

    if current and current["content_hash"] == claim.content_hash:
        return current["version"], False

    # Retract whatever is currently believed, in the same transaction as
    # asserting its replacement. There is no instant at which the memory holds
    # both or neither, which is what stops a concurrent reader seeing a gap.
    #
    # Predicated on `retracted_at IS NULL` rather than on a version number read
    # a moment ago: the set of believed versions is decided by the database at
    # write time, not by a value this process cached.
    conn.execute(
        "UPDATE fact SET retracted_at = now() WHERE fact_key = %s AND retracted_at IS NULL",
        (claim.fact_key,),
    )

    # The version number is computed inside the INSERT rather than by a
    # separate SELECT. A read-modify-write across two statements lets two
    # concurrent ingesters of the same drug both decide they are version N+1;
    # under SERIALIZABLE that surfaces as a retryable conflict, but only if
    # nothing in between has already committed a duplicate primary key.
    # Computing it in SQL keeps the whole decision inside one statement.
    row = conn.execute(
        """
        INSERT INTO fact (fact_key, version, subject_kind, subject_id, predicate,
                          claim, severity, payload, valid_from, valid_to,
                          source, source_ref, content_hash, embedding)
        SELECT %s, coalesce(max(f.version), 0) + 1, %s, %s, %s, %s, %s, %s::JSONB,
               %s, %s, %s, %s, %s, %s::VECTOR
          FROM fact f
         WHERE f.fact_key = %s
        RETURNING version
        """,
        (
            claim.fact_key, claim.subject_kind, claim.subject_id,
            claim.predicate, claim.claim, claim.severity,
            json.dumps(claim.payload),
            claim.valid_from, claim.valid_to, claim.source, claim.source_ref,
            claim.content_hash, embeddings.to_sql(vector),
            claim.fact_key,
        ),
    ).fetchone()
    next_version = row["version"]

    conn.execute(
        """
        INSERT INTO memory_audit (actor, action, target, detail)
        VALUES ('ingest', %s, %s, %s::JSONB)
        """,
        (
            "supersede" if current else "assert",
            claim.fact_key,
            json.dumps(
                {"version": next_version, "replaced": current["version"] if current else None}
            ),
        ),
    )

    return next_version, True


def ingest_recalls(since: str = "2024-01-01", limit: int | None = None) -> dict[str, int]:
    """Pull recalls from openFDA and assert them as claims."""
    stats = {"seen": 0, "claims": 0, "changed": 0}

    for record in openfda.recalls(since=since, limit=limit):
        stats["seen"] += 1
        for claim in recall_to_claims(record):
            stats["claims"] += 1
            vector = embeddings.embed(claim.claim)
            _, changed = run_in_txn(
                lambda conn, c=claim, v=vector: assert_claim(conn, c, v),
                label=f"assert {claim.fact_key}",
            )
            if changed:
                stats["changed"] += 1

    log.info("recall ingest: %s", stats)
    return stats


def ingest_boxed_warnings(limit: int | None = None) -> dict[str, int]:
    """Pull boxed-warning labels from openFDA and assert them as claims."""
    stats = {"seen": 0, "claims": 0, "changed": 0}

    for record in openfda.boxed_warnings(limit=limit):
        stats["seen"] += 1
        for claim in label_to_claims(record):
            stats["claims"] += 1
            vector = embeddings.embed(claim.claim)
            _, changed = run_in_txn(
                lambda conn, c=claim, v=vector: assert_claim(conn, c, v),
                label=f"assert {claim.fact_key}",
            )
            if changed:
                stats["changed"] += 1

    log.info("boxed-warning ingest: %s", stats)
    return stats
