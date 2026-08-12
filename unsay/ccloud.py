"""Control-plane checks through the ccloud CLI.

The data path talks SQL. This is the other half: asking the *control plane*
whether the cluster is in a state worth writing to, before starting work that
takes minutes and thousands of model calls.

Why shell out to ccloud rather than query SQL. A SQL connection tells you the
cluster answered one query. It cannot tell you the cluster is mid-upgrade, has
been suspended for exceeding its spend limit, or is in a state the control
plane considers unhealthy. A bulk ingest that discovers any of those halfway
through has wasted real money on embeddings, so the preflight asks the
authority that actually knows.

Every command is read-only. Nothing here creates, scales, or deletes anything:
the destructive verbs exist in ccloud and are deliberately not wired up, because
an agent that can delete a cluster is a worse trade than one that cannot.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import shutil
import subprocess

from unsay.config import settings

log = logging.getLogger(__name__)


class CcloudUnavailable(RuntimeError):
    """ccloud is not installed, or not authenticated."""


def binary() -> str:
    """Locate ccloud, including the Windows install path it does not add to PATH."""
    found = shutil.which("ccloud")
    if found:
        return found
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidate = pathlib.Path(appdata) / "ccloud" / "ccloud.exe"
        if candidate.exists():
            return str(candidate)
    raise CcloudUnavailable(
        "ccloud not found. Install it from "
        "https://www.cockroachlabs.com/docs/cockroachcloud/ccloud-get-started"
    )


def _run(args: list[str], timeout: float = 45.0) -> str:
    try:
        # ccloud writes progress spinners with characters the Windows default
        # codepage cannot decode, so the encoding is pinned and undecodable
        # bytes are replaced rather than raising. The JSON payload is ASCII;
        # only the decoration is at risk.
        proc = subprocess.run(
            [binary(), *args, "--output", "json"],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        raise CcloudUnavailable(f"ccloud timed out after {timeout}s") from exc

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout).strip().splitlines()
        first = err[0] if err else "unknown error"
        if "not logged in" in first.lower():
            raise CcloudUnavailable(
                "ccloud is not authenticated. Run `ccloud auth login`. "
                "Note it has no API-key path: auth is browser OAuth only."
            )
        raise CcloudUnavailable(f"ccloud failed: {first}")
    return proc.stdout


def clusters() -> list[dict]:
    return json.loads(_run(["cluster", "list"]) or "[]")


def preflight() -> dict:
    """Refuse to start a bulk write against a cluster that is not ready.

    Returns a verdict rather than raising on an unhealthy cluster, so the
    caller decides. Raising is reserved for "I could not find out", which is a
    different situation from "I found out and the answer is no".
    """
    cfg = settings()
    target = cfg.crdb_cluster_id

    found = [c for c in clusters() if c.get("id") == target] if target else []
    if not found:
        return {
            "ok": False,
            "reason": f"cluster {target or '(unset)'} not visible to this ccloud session",
            "cluster": None,
        }

    c = found[0]
    state = (c.get("state") or "").upper()
    healthy = state in {"CREATED", "AVAILABLE"}
    return {
        "ok": healthy,
        "reason": "ready" if healthy else f"cluster state is {state or 'unknown'}",
        "cluster": {
            "name": c.get("name"),
            "state": state,
            "plan": c.get("plan"),
            "version": c.get("cockroach_version"),
            "regions": [r.get("name") for r in (c.get("regions") or [])],
        },
    }
