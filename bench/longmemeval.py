"""LongMemEval harness (ICLR 2025), targeting the two categories this design is for.

    python bench/longmemeval.py --types knowledge-update,temporal-reasoning --limit 20

Why these two categories. LongMemEval defines six question types. Two of them
measure whether a memory system can handle facts that *change*:

  knowledge-update    a later session revises an earlier fact. The published
                      failure mode for additive stores is that the old fact is
                      preserved alongside the new one and surfaces next to it.
  temporal-reasoning  resolving "last known" state from explicit and implicit
                      time cues.

Unsay's whole thesis is that both are storage problems, not retrieval problems.
Each session is asserted at its own timestamp, and a session that revises a
fact retracts the prior version rather than sitting beside it. If the thesis is
right, these two categories should be where it shows.

Published baselines to compare against (LongMemEval, GPT-4o judge):
    Zep / Graphiti  63.8%
    Mem0            49.0%

Isolation: every instance is an independent user history, so the fact table is
cleared between instances. Instances therefore run sequentially; the work
inside one instance is parallelised instead.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unsay import agent, embeddings, memory  # noqa: E402
from unsay.config import settings  # noqa: E402
from unsay.db import close_pool, run_in_txn  # noqa: E402
from unsay.ingest import Claim, assert_claim  # noqa: E402

log = logging.getLogger("longmemeval")

DATA = pathlib.Path(__file__).parent / "data" / "longmemeval_oracle.json"
RESULTS = pathlib.Path(__file__).parent / "results"

# LongMemEval dates look like '2023/05/25 (Thu) 20:21'.
DATE_RE = re.compile(r"(\d{4})/(\d{2})/(\d{2})[^\d]*(\d{2}):(\d{2})")

EXTRACT_SYSTEM = """You convert a chat session into durable memory facts.

Emit only facts about the USER that are worth remembering later: preferences,
possessions, plans, relationships, measurements, states, commitments. Ignore
assistant chatter, pleasantries, and anything transient.

Reply with a single JSON object:

{"facts": [{"key": "...", "claim": "..."}, ...]}

The "key" is the critical field. It names the ATTRIBUTE, not the value, so that
a later session revising the same attribute produces the SAME key:

  good: "personal_best_5k_time"    claim: "Personal best in the charity 5K is 27:10."
  good: "car_make_model"           claim: "Drives a 2019 Subaru Outback."
  bad:  "ran_5k_in_27_minutes"     (encodes the value, so an update looks like a new fact)
  bad:  "fitness"                  (too broad, unrelated facts collide)

Use lower_snake_case, specific but value-free. If the session revises something
stated earlier, reuse the key it would have had. Emit [] if nothing is durable."""


def parse_date(s: str) -> datetime:
    m = DATE_RE.search(s or "")
    if not m:
        return datetime.now(timezone.utc)
    y, mo, d, h, mi = (int(x) for x in m.groups())
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def render_session(turns: list[dict]) -> str:
    return "\n".join(
        f"{t.get('role', '?').upper()}: {t.get('content', '')}" for t in turns
    )[:14000]


def extract_facts(session_text: str) -> list[dict]:
    """One Bedrock call per session, turning turns into keyed claims."""
    try:
        raw = agent.call_json(EXTRACT_SYSTEM, session_text, max_tokens=1200)
    except Exception as exc:
        log.warning("extraction failed: %s", str(exc)[:120])
        return []
    facts = raw.get("facts", [])
    return [
        f for f in facts
        if isinstance(f, dict) and f.get("key") and f.get("claim")
    ]


def wipe() -> None:
    def work(conn):
        conn.execute("DELETE FROM decision_read")
        conn.execute("DELETE FROM decision")
        conn.execute("DELETE FROM fact")
    run_in_txn(work, label="bench_wipe")


def load_instance(inst: dict, workers: int) -> int:
    """Assert every session of one instance, in chronological order.

    Order matters and is the point: sessions are sorted by date so that a later
    session revising a fact retracts the earlier version rather than racing it.
    Extraction and embedding are parallel; the asserts are sequential per key.
    """
    sessions = list(zip(inst["haystack_dates"], inst["haystack_sessions"]))
    sessions.sort(key=lambda p: parse_date(p[0]))

    with ThreadPoolExecutor(max_workers=workers) as ex:
        extracted = list(ex.map(lambda p: extract_facts(render_session(p[1])), sessions))

    total = 0
    for (date_str, _), facts in zip(sessions, extracted):
        when = parse_date(date_str)
        if not facts:
            continue
        with ThreadPoolExecutor(max_workers=workers) as ex:
            vectors = list(ex.map(lambda f: embeddings.embed(f["claim"]), facts))

        for f, vec in zip(facts, vectors):
            claim = Claim(
                fact_key=f"lme:{f['key']}",
                subject_kind="drug",           # schema CHECK constraint vocabulary
                subject_id="user",
                predicate="dosage",            # ditto; unused by this benchmark
                claim=f["claim"],
                severity="info",
                valid_from=when, valid_to=None,
                source="longmemeval", source_ref=inst["question_id"],
            )
            # asserted_at must be the SESSION date, not now(), or every fact
            # looks simultaneous and supersession loses its ordering.
            run_in_txn(
                lambda c, cl=claim, v=vec, w=when: _assert_at(c, cl, v, w),
                label="lme_assert",
            )
            total += 1
    return total


def _assert_at(conn, claim: Claim, vector, when: datetime):
    version, changed = assert_claim(conn, claim, vector)
    if changed:
        conn.execute(
            "UPDATE fact SET asserted_at = %s WHERE fact_key = %s AND version = %s",
            (when, claim.fact_key, version),
        )
        conn.execute(
            """
            UPDATE fact SET retracted_at = %s
             WHERE fact_key = %s AND version < %s AND retracted_at IS NOT NULL
               AND retracted_at > %s
            """,
            (when, claim.fact_key, version, when),
        )
    return version, changed


ANSWER_SYSTEM = """You answer a question using only the MEMORY FACTS provided.

The facts are what the system currently believes, already filtered to remove
anything superseded. Answer directly and briefly, in the fewest words that
fully answer it. No hedging, no restating the question, no explanation of
where the fact came from. If the facts genuinely do not contain the answer,
reply exactly: I don't know."""


def answer(question: str, k: int) -> str:
    facts = memory.retrieve(question, k=k)
    rendered = "MEMORY FACTS:\n" + "\n".join(
        f"- ({f.valid_from:%Y-%m-%d}) {f.claim}" for f in facts
    ) if facts else "MEMORY FACTS: (none)"
    try:
        return agent.call_text(ANSWER_SYSTEM, f"{rendered}\n\nQUESTION: {question}", max_tokens=200)
    except Exception as exc:
        log.warning("answer failed: %s", str(exc)[:120])
        return "I don't know."


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--types", default="knowledge-update,temporal-reasoning")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("unsay.db").setLevel(logging.ERROR)

    if os.environ.get("UNSAY_ALLOW_FAKE_EMBEDDINGS") == "1":
        print("REFUSING: benchmark numbers from stub embeddings are meaningless.")
        return 2

    data = json.load(open(DATA, encoding="utf-8"))
    wanted = {t.strip() for t in args.types.split(",")}
    items = [d for d in data if d["question_type"] in wanted]
    if args.limit:
        items = items[: args.limit]

    RESULTS.mkdir(exist_ok=True)
    out = pathlib.Path(args.out) if args.out else RESULTS / "hypotheses.jsonl"
    print(f"{len(items)} instances across {sorted(wanted)}")
    print(f"model: {settings().bedrock_model_id}\nwriting: {out}\n")

    started = time.time()
    with open(out, "w", encoding="utf-8") as fh:
        for i, inst in enumerate(items, 1):
            wipe()
            n = load_instance(inst, args.workers)
            hyp = answer(inst["question"], args.k)
            fh.write(json.dumps({
                "question_id": inst["question_id"],
                "question_type": inst["question_type"],
                "hypothesis": hyp,
                "question": inst["question"],
                "gold": inst["answer"],
                "facts_loaded": n,
            }) + "\n")
            fh.flush()
            print(f"[{i}/{len(items)}] {inst['question_type']:19s} facts={n:3d}  "
                  f"gold={str(inst['answer'])[:34]:36s} got={hyp[:42]}")

    print(f"\n{len(items)} answered in {time.time() - started:.0f}s -> {out}")
    print("\nGrade with the official evaluator (GPT-4o judge, as published):")
    print("  git clone https://github.com/xiaowu0162/LongMemEval && cd LongMemEval/src/evaluation")
    print(f"  python3 evaluate_qa.py gpt-4o {out} ../../data/longmemeval_oracle.json")
    close_pool()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
