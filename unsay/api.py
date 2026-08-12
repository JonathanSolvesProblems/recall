"""HTTP surface for the demo.

Deliberately shaped around the one story the demo tells, in order:

    ask  ->  the world changes  ->  sweep  ->  who was told what, and corrected

Everything else (status, replay, expiry proof) is a receipt the viewer can
check afterwards, not a step they have to walk through first.
"""

from __future__ import annotations

import json
import logging
import pathlib
from datetime import datetime, timezone
from typing import Any

import psycopg
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from unsay import agent, budget, demo, embeddings, memory, sweep
from unsay.db import close_pool, query, run_in_txn
from unsay.ingest import Claim, assert_claim

log = logging.getLogger(__name__)

WEB = pathlib.Path(__file__).parent.parent / "web"

app = FastAPI(title="Unsay", docs_url="/api/docs")

# One directly-reachable node per region, used only so the UI can show a region
# as gone during the failure demo. The application itself never targets a
# specific region; psycopg fails over across the whole list.
REGION_PROBES = {
    "us-east-1": "postgresql://root@localhost:26257/unsay?sslmode=disable&connect_timeout=2",
    "us-west-2": "postgresql://root@localhost:26258/unsay?sslmode=disable&connect_timeout=2",
    "eu-west-1": "postgresql://root@localhost:26259/unsay?sslmode=disable&connect_timeout=2",
}


class AskRequest(BaseModel):
    question: str
    subject: str | None = None
    patient_id: str | None = None


class SupersedeRequest(BaseModel):
    subject: str
    lot: str | None = None
    reason: str = "NDMA above the acceptable intake limit"


@app.get("/api/status")
def status() -> dict[str, Any]:
    # The probe map only covers the local 9-node cluster, where each region has
    # a directly reachable port so the demo can show one going away. A hosted
    # single-region cluster has no such port, and treating "no probe configured"
    # as "down" made the deployed demo report its own healthy region as failed.
    #
    # Absence of a probe is absence of evidence: the region is reported as up,
    # because this query reached the cluster in order to ask the question at
    # all, and marked unprobed so the UI does not overclaim.
    regions = []
    for r in query("SHOW REGIONS FROM DATABASE unsay"):
        name = r["region"]
        dsn = REGION_PROBES.get(name)
        if dsn is None:
            up, probed = True, False
        else:
            probed = True
            try:
                with psycopg.connect(dsn) as c:
                    c.execute("SELECT 1")
                up = True
            except Exception:
                up = False
        regions.append(
            {"region": name, "primary": bool(r.get("primary")), "up": up, "probed": probed}
        )

    counts = query(
        """
        SELECT (SELECT count(*) FROM fact)                               AS fact_versions,
               (SELECT count(*) FROM fact WHERE believed)                AS believed,
               (SELECT count(*) FROM decision)                           AS decisions,
               (SELECT count(*) FROM decision WHERE status = 'standing') AS standing,
               (SELECT count(*) FROM correction)                         AS corrections,
               (SELECT count(*) FROM outbox)                             AS notices
        """
    )[0]

    goal = query("SHOW SURVIVAL GOAL FROM DATABASE unsay")[0]["survival_goal"]
    return {"regions": regions, "survival_goal": goal, "counts": counts,
            "budget": budget.spent_today()}


@app.post("/api/ask")
def ask(req: AskRequest) -> dict[str, Any]:
    if not req.question.strip():
        raise HTTPException(400, "question is required")
    try:
        budget.charge(caller="ask", n=1)
    except budget.BudgetExceeded as exc:
        raise HTTPException(429, str(exc)) from exc
    a = agent.ask(
        req.question, subject_id=req.subject or None, patient_id=req.patient_id or None
    )
    reads = query(
        """
        SELECT dr.fact_key, dr.fact_version, dr.load_bearing, f.claim, f.severity
          FROM decision_read dr
          JOIN fact f ON f.fact_key = dr.fact_key AND f.version = dr.fact_version
         WHERE dr.decision_id = %s
         ORDER BY dr.rank
        """,
        (a.decision_id,),
    )
    return {
        "decision_id": a.decision_id,
        "verdict": a.verdict,
        "answer": a.answer,
        "confidence": a.confidence,
        "evidence": reads,
    }


@app.get("/api/patients")
def patients() -> list[dict]:
    return query(
        """
        SELECT p.patient_id, p.display_name, p.contact,
               count(d.decision_id) FILTER (WHERE d.status = 'standing') AS standing
          FROM patient p
          LEFT JOIN decision d ON d.patient_id = p.patient_id
         GROUP BY p.patient_id, p.display_name, p.contact
         ORDER BY p.display_name
        """
    )


@app.post("/api/demo/supersede")
def supersede(req: SupersedeRequest) -> dict[str, Any]:
    """Publish a Class I recall that supersedes what is currently believed.

    This is the "world changes" beat. It writes a genuine new claim version
    through the same ingest path openFDA uses, so the sweep that follows is
    reacting to a real supersession rather than a staged flag.
    """
    now = datetime.now(timezone.utc)
    scope = req.lot or "all-lots"

    # Supersede the claim that is actually on file for this drug rather than
    # minting a new key. This is what openFDA does when a recall is escalated:
    # same recall number, new revision. Writing a fresh key instead would leave
    # the original claim believed, so no past answer would be invalidated and
    # the sweep would correctly find nothing. The fact_key is the join column
    # that makes retroactive repair possible; inventing one bypasses it.
    existing = query(
        """
        SELECT fact_key, subject_kind FROM fact
         WHERE believed AND subject_id = %s AND predicate = 'recall'
         ORDER BY (severity = 'class_i') DESC, version DESC
         LIMIT 1
        """,
        (req.subject,),
    )
    if not existing:
        raise HTTPException(
            404,
            f"no believed recall claim for {req.subject!r} to supersede. "
            f"Ask about a drug that appears in the corpus first.",
        )

    fact_key = existing[0]["fact_key"]
    text = (
        f"Class I recall: {req.subject} lot {scope} recalled. Reason: {req.reason}. "
        f"Stop use and return to pharmacy."
    )
    claim = Claim(
        fact_key=fact_key,
        subject_kind=existing[0]["subject_kind"],
        subject_id=req.subject,
        predicate="recall",
        claim=text,
        severity="class_i",
        valid_from=now,
        valid_to=None,
        source="openfda:enforcement",
        source_ref="DEMO-ESCALATION",
    )
    vector = embeddings.embed(text)
    version, changed = run_in_txn(
        lambda c: assert_claim(c, claim, vector), label="demo_supersede"
    )
    return {"fact_key": claim.fact_key, "version": version, "changed": changed, "claim": text}


@app.post("/api/demo/reset")
def demo_reset(patients: int = 12) -> dict[str, Any]:
    """Restore the scenario so the next visitor sees it unspent.

    The sweep is destructive by design, so without this the demo works exactly
    once and every judge after the first sees a button that does nothing.
    """
    # Reset is free after the first one: the opening answer is cached, so this
    # spends nothing on the model and can safely run on every page load.
    return demo.reset(patients=max(1, min(patients, 16)))


@app.get("/api/demo/state")
def demo_state() -> dict[str, Any]:
    return {"spent": demo.is_spent(), "subject": demo.SUBJECT, "lot": demo.LOT}


@app.get("/api/sweep/candidates")
def candidates() -> list[dict]:
    return [
        {
            "decision_id": c.decision_id,
            "question": c.question,
            "answer": c.answer,
            "verdict": c.verdict,
            "patient_id": c.patient_id,
            "stale": c.stale,
        }
        for c in sweep.find_candidates()
    ]


@app.post("/api/sweep/run")
def run_sweep() -> dict[str, Any]:
    # One call per distinct question after memoisation, but charge for a few
    # so a sweep cannot be looped cheaply.
    try:
        budget.charge(caller="sweep", n=3)
    except budget.BudgetExceeded as exc:
        raise HTTPException(429, str(exc)) from exc
    summary = sweep.run_sweep(
        reevaluate=agent.reevaluate, trigger_kind="api", trigger_ref="demo"
    )
    summary["corrections"] = query(
        """
        SELECT c.decision_id, c.prior_verdict, c.new_verdict, c.new_answer,
               p.display_name, p.contact
          FROM correction c
          LEFT JOIN decision d ON d.decision_id = c.decision_id
          LEFT JOIN patient  p ON p.patient_id  = d.patient_id
         WHERE c.sweep_id = %s
         ORDER BY c.created_at
        """,
        (summary["sweep_id"],),
    )
    return summary


@app.get("/api/outbox")
def outbox() -> list[dict]:
    rows = query(
        """
        SELECT dedupe_key, recipient, state, payload, created_at
          FROM outbox ORDER BY created_at DESC LIMIT 50
        """
    )
    for r in rows:
        if isinstance(r["payload"], str):
            r["payload"] = json.loads(r["payload"])
    return rows


@app.get("/api/replay/{decision_id}")
def replay(decision_id: str) -> dict[str, Any]:
    try:
        return memory.replay_decision(decision_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/health")
def health() -> JSONResponse:
    try:
        query("SELECT 1")
        return JSONResponse({"ok": True})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)[:200]}, status_code=503)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB / "index.html")


if WEB.exists():
    app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.on_event("shutdown")
def _shutdown() -> None:
    close_pool()
