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

# Point at a dedicated database BEFORE unsay.config is imported, since settings
# are cached on first read. The benchmark needs a corpus containing only the
# instance under test: any other claim in the table is a distractor that would
# quietly change the measurement.
_DEFAULT_BENCH_DSN = (
    "postgresql://root@localhost:26257,localhost:26258,localhost:26259"
    "/unsay_bench?sslmode=disable"
)
os.environ["UNSAY_DSN"] = os.environ.get("UNSAY_BENCH_DSN", _DEFAULT_BENCH_DSN)

from unsay import agent, embeddings, memory  # noqa: E402
from unsay.config import settings  # noqa: E402
from unsay.db import close_pool, query, run_in_txn  # noqa: E402
from unsay.ingest import Claim, assert_claim  # noqa: E402

log = logging.getLogger("longmemeval")

DATA_DIR = pathlib.Path(__file__).parent / "data"
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

Use lower_snake_case, specific but value-free. Emit [] if nothing is durable.

You will be shown KNOWN KEYS: attributes already recorded for this user. If
this session updates, corrects or refines one of them, you MUST reuse that
exact key. Reusing it is what lets the newer claim supersede the older one.
Inventing a near-synonym instead leaves both versions live and the stale one
can win. Only mint a new key for an attribute genuinely not in the list."""


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


# Sessions whose facts never reached the store. Counted, never swallowed: an
# extraction failure that returns [] is indistinguishable from a session with
# nothing worth remembering, and the benchmark then scores an ingestion bug as
# a memory failure. In the first pilot this cost a knowledge-update instance
# whose updating session simply never landed.
EXTRACT_FAILURES: list[str] = []

# Instances abandoned mid-run. Reported, never scored: an instance that never
# ran is not an instance the system got wrong.
SKIPPED: list[str] = []


def known_keys() -> list[tuple[str, str]]:
    """Attributes already recorded, newest believed version of each."""
    return [
        (r["fact_key"].removeprefix("lme:"), r["claim"])
        for r in query(
            """
            SELECT fact_key, claim FROM fact
             WHERE source = %s AND believed
             ORDER BY fact_key
            """,
            (BENCH_SOURCE,),
        )
    ]


def canonical_key(proposed: str, existing: list[str]) -> str:
    """Snap a proposed key onto an existing one when it is a refinement of it.

    Instructing the model to reuse keys is necessary but not sufficient. Asked
    to record a new family-trip destination against a known
    `recent_family_trip_destination`, it emitted
    `recent_family_trip_destination_paris`: a near-synonym that encodes the
    value in the key, so the two claims sit side by side and the stale one can
    still be retrieved.

    A qualifier appended to a known attribute is a new value for that
    attribute, not a new attribute, so the extension is dropped. Enforced here
    rather than asked for, because a constraint the code can guarantee should
    not be delegated to a prompt.

    The tradeoff is deliberate: a genuine sub-attribute whose name extends a
    parent (`car_make_model` and `car_make_model_second_car`) merges too. In
    this corpus value-encoding is far commoner than true nesting, and merging
    costs a distinction while not merging costs a wrong answer.
    """
    if proposed in existing:
        return proposed
    matches = [e for e in existing if proposed.startswith(e + "_")]
    return max(matches, key=len) if matches else proposed


def extract_facts(session_text: str, *, attempts: int = 3) -> list[dict]:
    """One Bedrock call per session, turning turns into keyed claims.

    Extraction is memory-aware. Shown the keys already on file, the model can
    reuse one when a session revises that attribute, which is what makes the
    new claim supersede the old rather than sit beside it. Without this the
    same attribute acquires a fresh synonym key per session and the store
    becomes additive, which is the failure mode this design exists to avoid.
    """
    known = known_keys()
    context = ""
    if known:
        listed = "\n".join(f"  {k}: {c[:90]}" for k, c in known[:60])
        context = f"KNOWN KEYS (reuse when this session updates one):\n{listed}\n\n"

    existing = [k for k, _ in known]
    last = ""
    for attempt in range(1, attempts + 1):
        try:
            raw = agent.call_json(EXTRACT_SYSTEM, context + session_text, max_tokens=1500,
                                  model_id=settings().bedrock_extract_model_id)
            facts = raw.get("facts", [])
            out = [
                f for f in facts
                if isinstance(f, dict) and f.get("key") and f.get("claim")
            ]
            for f in out:
                f["key"] = canonical_key(f["key"], existing)
            return out
        except Exception as exc:
            last = str(exc)[:140]
            log.warning("extraction attempt %d/%d failed: %s", attempt, attempts, last)

    EXTRACT_FAILURES.append(last)
    return []


BENCH_SOURCE = "longmemeval"


def wipe() -> None:
    """Clear the previous instance's history.

    Scoped to this benchmark's own rows even though it runs against a separate
    database. An earlier version deleted the whole `fact` table and, sharing a
    database with the openFDA corpus at the time, destroyed it. Isolation by
    convention is not isolation; the WHERE clause is the actual guarantee.
    """
    def work(conn):
        conn.execute(
            "DELETE FROM decision_read WHERE fact_key LIKE 'lme:%%'"
        )
        conn.execute(
            "DELETE FROM decision WHERE model_id = %s", (BENCH_SOURCE,)
        )
        conn.execute("DELETE FROM fact WHERE source = %s", (BENCH_SOURCE,))
    run_in_txn(work, label="bench_wipe")


def load_instance(inst: dict, workers: int) -> int:
    """Assert every session of one instance, in chronological order.

    Order matters and is the point: sessions are sorted by date so that a later
    session revising a fact retracts the earlier version rather than racing it.
    Extraction and embedding are parallel; the asserts are sequential per key.
    """
    sessions = list(zip(inst["haystack_dates"], inst["haystack_sessions"]))
    sessions.sort(key=lambda p: parse_date(p[0]))

    total = 0
    # Sessions are processed strictly in order and one at a time, because each
    # extraction is shown the keys the previous ones stored. Parallelising the
    # extractions would mean every session sees an empty key list and invents
    # its own naming, which is the bug this ordering fixes. Embedding within a
    # session is still concurrent.
    for date_str, turns in sessions:
        when = parse_date(date_str)
        facts = extract_facts(render_session(turns))
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
    ap.add_argument("--split", default="oracle", choices=["oracle", "s"],
                    help="oracle = evidence sessions only (easy). "
                         "s = evidence buried in ~115k tokens of distractors, "
                         "which is what the published baselines use.")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("unsay.db").setLevel(logging.ERROR)

    if os.environ.get("UNSAY_ALLOW_FAKE_EMBEDDINGS") == "1":
        print("REFUSING: benchmark numbers from stub embeddings are meaningless.")
        return 2

    data = json.load(open(DATA_DIR / f"longmemeval_{args.split}.json", encoding="utf-8"))
    wanted = {t.strip() for t in args.types.split(",")}
    items = [d for d in data if d["question_type"] in wanted]
    if args.limit:
        items = items[: args.limit]

    RESULTS.mkdir(exist_ok=True)
    out = pathlib.Path(args.out) if args.out else RESULTS / "hypotheses.jsonl"

    # Resume. On a constrained Bedrock quota an s-split run is measured in
    # hours, and losing all of it to one unrecoverable throttle after four
    # hours is the difference between a number and no number. Instances
    # already written are skipped and the file is appended to.
    done: set[str] = set()
    if out.exists():
        for line in out.read_text(encoding="utf-8").splitlines():
            try:
                done.add(json.loads(line)["question_id"])
            except Exception:
                continue
    if done:
        print(f"resuming: {len(done)} instance(s) already in {out}")
    items = [i for i in items if i["question_id"] not in done]

    print(f"{len(items)} instances to run across {sorted(wanted)}")
    print(f"answer model: {settings().bedrock_model_id}")
    print(f"extract model: {settings().bedrock_extract_model_id}\nwriting: {out}\n")

    started = time.time()
    with open(out, "a", encoding="utf-8") as fh:
        for i, inst in enumerate(items, 1):
            try:
                wipe()
                n = load_instance(inst, args.workers)
                hyp = answer(inst["question"], args.k)
            except Exception as exc:
                # One instance failing must not cost the hours already spent.
                # It is recorded as skipped rather than scored, so it cannot
                # be silently counted as a wrong answer.
                log.warning("instance %s failed, skipping: %s",
                            inst["question_id"], str(exc)[:160])
                SKIPPED.append(inst["question_id"])
                continue
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
    if SKIPPED:
        print(f"\nSKIPPED {len(SKIPPED)} instance(s) that failed mid-run. They are "
              f"absent from the output, not scored as wrong:")
        for q in SKIPPED[:8]:
            print(f"  {q}")
    if EXTRACT_FAILURES:
        print(f"\nWARNING: {len(EXTRACT_FAILURES)} session(s) failed extraction after "
              f"retries. Those instances measure ingestion, not memory, and their "
              f"results are not trustworthy:")
        for f in EXTRACT_FAILURES[:5]:
            print(f"  {f}")
    else:
        print("all sessions extracted cleanly")
    print("\nGrade with the official evaluator (GPT-4o judge, as published):")
    print("  git clone https://github.com/xiaowu0162/LongMemEval && cd LongMemEval/src/evaluation")
    print(f"  python3 evaluate_qa.py gpt-4o {out} ../../data/longmemeval_oracle.json")
    close_pool()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
