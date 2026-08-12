"""Command line for driving Unsay, including the live demo."""

from __future__ import annotations

import json
import logging
import time

import typer
from rich.console import Console
from rich.table import Table

from unsay import ingest, memory, sweep
from unsay.db import close_pool, query

app = typer.Typer(add_completion=False, help="Agent memory that can take back what it said.")
console = Console()

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


# One directly-reachable SQL port per region, used to probe which regions are
# actually up. The application itself never targets a specific region; this is
# purely so the demo can show a region gone while answers keep flowing.
REGION_PROBES = {
    "us-east-1": "postgresql://root@localhost:26257/unsay?sslmode=disable&connect_timeout=2",
    "us-west-2": "postgresql://root@localhost:26258/unsay?sslmode=disable&connect_timeout=2",
    "eu-west-1": "postgresql://root@localhost:26259/unsay?sslmode=disable&connect_timeout=2",
}


@app.command()
def status() -> None:
    """Cluster topology, per-region reachability, and memory counts.

    Deliberately avoids crdb_internal, which v26.2 restricts by default with
    the hint that it is unsupported in production. Everything here comes from
    supported SQL plus a direct connection probe per region.
    """
    import psycopg

    regions = query("SHOW REGIONS FROM DATABASE unsay")
    t = Table(title="topology")
    for col in ("region", "zones", "primary", "reachable"):
        t.add_column(col)

    live = 0
    for r in regions:
        name = r["region"]
        reachable = False
        dsn = REGION_PROBES.get(name)
        if dsn:
            try:
                with psycopg.connect(dsn) as c:
                    c.execute("SELECT 1")
                reachable = True
                live += 1
            except Exception:
                reachable = False
        t.add_row(
            name,
            ",".join(r["zones"]),
            "yes" if r.get("primary") else "",
            "[green]up[/green]" if reachable else "[red]DOWN[/red]",
        )
    console.print(t)

    goal = query("SHOW SURVIVAL GOAL FROM DATABASE unsay")[0]["survival_goal"]
    console.print(f"survival goal: [bold]{goal}[/bold]    regions up: {live}/{len(regions)}")

    counts = query(
        """
        SELECT (SELECT count(*) FROM fact)                               AS fact_versions,
               (SELECT count(*) FROM fact WHERE believed)                AS believed,
               (SELECT count(*) FROM decision)                           AS decisions,
               (SELECT count(*) FROM decision WHERE status = 'standing') AS standing,
               (SELECT count(*) FROM decision_read)                      AS provenance_edges,
               (SELECT count(*) FROM correction)                         AS corrections,
               (SELECT count(*) FROM outbox)                             AS notices
        """
    )[0]
    t2 = Table(title="memory")
    t2.add_column("metric")
    t2.add_column("count", justify="right")
    for key, val in counts.items():
        t2.add_row(key.replace("_", " "), f"{val:,}")
    console.print(t2)
    close_pool()


@app.command("ingest-recalls")
def ingest_recalls(
    since: str = typer.Option("2024-01-01", help="Earliest openFDA report_date."),
    limit: int | None = typer.Option(None, help="Stop after N source records."),
) -> None:
    """Pull drug recalls from openFDA into bitemporal memory."""
    started = time.time()
    stats = ingest.ingest_recalls(since=since, limit=limit)
    console.print(
        f"[green]{stats['seen']:,} records -> {stats['claims']:,} claims, "
        f"{stats['changed']:,} new versions in {time.time() - started:.1f}s[/green]"
    )
    close_pool()


@app.command("ingest-warnings")
def ingest_warnings(limit: int | None = typer.Option(None)) -> None:
    """Pull boxed-warning labels from openFDA into bitemporal memory."""
    started = time.time()
    stats = ingest.ingest_boxed_warnings(limit=limit)
    console.print(
        f"[green]{stats['seen']:,} labels -> {stats['claims']:,} claims, "
        f"{stats['changed']:,} new versions in {time.time() - started:.1f}s[/green]"
    )
    close_pool()


@app.command()
def ask(
    question: str,
    subject: str | None = typer.Option(None, help="Normalized drug name, e.g. valsartan."),
    patient: str | None = typer.Option(None, help="Patient UUID."),
) -> None:
    """Ask the agent a question. Requires AWS credentials for Bedrock."""
    from unsay import agent

    result = agent.ask(question, subject_id=subject, patient_id=patient)
    console.print(f"[bold]{result.verdict.upper()}[/bold]  ({result.confidence:.2f})")
    console.print(result.answer)
    console.print(f"[dim]cited {len(result.cited)} claim(s) | decision {result.decision_id}[/dim]")
    close_pool()


@app.command()
def replay(decision_id: str) -> None:
    """Show what the agent knew when it answered, and what has moved since."""
    view = memory.replay_decision(decision_id)
    d = view["decision"]
    console.print(f"[bold]{d['question']}[/bold]")
    console.print(f"answered {d['decided_at']}: [bold]{d['verdict']}[/bold] -- {d['answer']}\n")

    t = Table(title="evidence as read, versus now")
    for col in ("rank", "fact", "read", "now", "state"):
        t.add_column(col)
    for r in view["reads"]:
        stale = r["retracted_at"] is not None
        t.add_row(
            str(r["rank"]), r["fact_key"][:46],
            f"v{r['fact_version']} {r['severity_as_read']}",
            f"v{r['current_version']} {r['severity_now']}" if r["current_version"] else "-",
            "[red]SUPERSEDED[/red]" if stale else "[green]current[/green]",
        )
    console.print(t)
    console.print(f"{len(view['stale_reads'])} of {len(view['reads'])} reads have been superseded")
    close_pool()


@app.command("sweep")
def run_sweep(
    subject: str | None = typer.Option(None, help="Limit to one drug."),
    dry_run: bool = typer.Option(False, help="List affected answers without repairing."),
) -> None:
    """Find and repair every standing answer built on evidence that has changed."""
    if dry_run:
        found = sweep.find_candidates(subject_id=subject)
        console.print(f"{len(found)} standing answer(s) rest on superseded evidence")
        for c in found[:20]:
            console.print(f"  [{c.verdict}] {c.question[:70]}")
            for s in c.stale:
                console.print(f"      [dim]{s['fact_key']} v{s['read_version']}[/dim]")
        close_pool()
        return

    from unsay import agent

    started = time.time()
    summary = sweep.run_sweep(
        reevaluate=agent.reevaluate, trigger_kind="cli", trigger_ref=subject or "all",
        subject_id=subject,
    )
    console.print(
        f"[green]swept {summary['candidates']} answers in {time.time() - started:.2f}s: "
        f"{summary['reversed']} reversed, {summary['notified']} patients notified[/green]"
    )
    close_pool()


@app.command("cluster-health")
def cluster_health() -> None:
    """Ask the control plane whether the cluster is fit to write to.

    Uses the ccloud CLI rather than SQL: a SQL connection proves one query
    answered, not that the cluster is out of maintenance or inside its spend
    limit. Read-only; no ccloud verb here can change anything.
    """
    from unsay import ccloud

    try:
        v = ccloud.preflight()
    except ccloud.CcloudUnavailable as exc:
        console.print(f"[red]ccloud unavailable:[/red] {exc}")
        raise typer.Exit(2) from exc

    c = v["cluster"]
    if c:
        t = Table(title="control plane")
        for col in ("name", "state", "plan", "version", "regions"):
            t.add_column(col)
        t.add_row(c["name"], c["state"], str(c["plan"]), str(c["version"]),
                  ",".join(c["regions"] or []))
        console.print(t)
    console.print(
        f"[green]ready[/green]: {v['reason']}" if v["ok"]
        else f"[red]not ready[/red]: {v['reason']}"
    )
    close_pool()
    if not v["ok"]:
        raise typer.Exit(1)


@app.command()
def audit(limit: int = typer.Option(25, help="most recent entries")) -> None:
    """Every mutation to memory, in order. The observability surface.

    memory_audit is appended to inside the same transaction as the change it
    describes, so an entry cannot exist for a change that rolled back, and a
    change cannot commit without its entry.
    """
    rows = query(
        """
        SELECT at, actor, action, target, detail
          FROM memory_audit ORDER BY at DESC LIMIT %s
        """,
        (limit,),
    )
    t = Table(title=f"memory audit (last {len(rows)})")
    for col in ("when", "actor", "action", "target", "detail"):
        t.add_column(col)
    for r in rows:
        d = r["detail"] if isinstance(r["detail"], dict) else json.loads(r["detail"] or "{}")
        t.add_row(str(r["at"])[:19], r["actor"], r["action"],
                  str(r["target"])[:44], json.dumps(d)[:52])
    console.print(t)
    close_pool()


@app.command()
def outbox(pending_only: bool = typer.Option(False, "--pending")) -> None:
    """Show queued patient notifications."""
    sql = "SELECT dedupe_key, recipient, state, payload, created_at FROM outbox"
    if pending_only:
        sql += " WHERE state = 'pending'"
    sql += " ORDER BY created_at DESC LIMIT 50"

    rows = query(sql)
    console.print(f"{len(rows)} notification(s)")
    for r in rows:
        p = r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"])
        console.print(
            f"  [{r['state']}] {r['recipient']}: "
            f"{p.get('prior_verdict')} -> [bold]{p.get('new_verdict')}[/bold]"
        )
        console.print(f"      [dim]{p.get('message', '')[:110]}[/dim]")
        console.print(f"      [dim]dedupe {r['dedupe_key'][:16]}...[/dim]")
    close_pool()


if __name__ == "__main__":
    app()
