# Unsay

**An AI medication-safety agent that goes back and un-says what it told you.**

**[Live demo](https://dzcqeaznqrb2dwvy3h4btiavrm0etzef.lambda-url.us-east-1.on.aws/)**
· [health](https://dzcqeaznqrb2dwvy3h4btiavrm0etzef.lambda-url.us-east-1.on.aws/api/health)
· AWS Lambda in front of a CockroachDB Cloud cluster holding 554 live openFDA
claims. First request after a quiet spell pays a cold start of a few seconds.

Ask it whether your prescription is safe and it answers from live FDA data.
The part that matters comes later: when the FDA recalls that drug next
Tuesday, Unsay finds every person it already reassured, works out which of
those answers are now wrong, and corrects them. Each one, by name, in seconds.

Two claims, both demonstrated below rather than asserted:

> **Other agent memories can tell you what they knew. Unsay goes back and
> fixes what it said.**
>
> **And its replay still works in six months, when MVCC time-travel expired
> after twenty-five hours.**

---

## The problem this exists for

The named Day-2 failure of agent memory in 2026 is **stale context**:
similarity to a stored memory does not prove that the memory is still true. An
agent retrieves a fact that was correct when it was written, and answers as if
it were correct now.

For most agents that is embarrassing. In a pharmacy it is a Class I recall,
which the FDA defines as a reasonable probability of serious adverse health
consequences or death.

The gap is documented. A 2024 study at an academic medical center,
[*Automating Individualized Notification of Drug Recalls to Patients*](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC12798837/),
built a system to scan FDA recalls nightly and message affected patients
through their EHR portal. It hit two walls. Most recalls are Class II, so
notification "stops with wholesalers and pharmacies rather than reaching
patients." And critically: *"it was not possible to trace a medication
prescription from the EHR to specific lot numbers dispensed to that patient by
a community pharmacy."*

A recall is scoped to a lot. If you cannot resolve a lot to a person, you
either tell everybody or you tell nobody.

---

## Claim 1: repair, not just recall

When the FDA publishes a recall, some number of answers already given are now
wrong. They were correct when given. Nothing about them changed; the world
moved underneath them.

Finding them is this query:

```sql
SELECT d.decision_id, d.answer, d.verdict
  FROM decision d
  JOIN decision_read dr ON dr.decision_id = d.decision_id
  JOIN fact stale       ON stale.fact_key = dr.fact_key
                       AND stale.version  = dr.fact_version
 WHERE d.status = 'standing'
   AND dr.load_bearing
   AND stale.retracted_at IS NOT NULL;
```

Read it as English: *every answer still standing that leaned on a version of a
claim we no longer believe.*

**A vector database cannot express this query.** Not slowly, not
approximately. It has no notion that a memory has versions, and no record of
which version a given answer consumed. The most it can return is today's
nearest neighbours, which tells you nothing about what you said in March.

Reconstructing history is an insight layer. Unsay closes the loop: each
affected answer is re-decided against current memory, and where the verdict
moves, a correction is drafted and a notification queued for a named person.

---

## Claim 2: the replay does not expire

The obvious way to build "what did the agent know when it decided" on
CockroachDB is to store the read timestamp and replay it with
`AS OF SYSTEM TIME`. It is elegant, needs no extra tables, and is correct right
up until the garbage collector moves past the timestamp you saved.

CockroachDB's own documentation says so plainly: `gc.ttlseconds` *"is not meant
to be a solution for long-term retention of history; for that you should
handle versioning in the schema design at the application layer."* The default
window is **4 hours**. **25 hours** is the largest value Cockroach Labs
regularly tests.

So Unsay's durable mechanism is a **bitemporal schema**, which is what those
docs prescribe. Every claim carries two independent time axes:

- `valid_from` / `valid_to` — when the claim is true **in the world**
- `asserted_at` / `retracted_at` — when **this system** believed it

Keeping them apart is what separates "the drug became dangerous on March 3rd"
from "we found out on July 2nd", which is the difference between an unlucky
answer and a negligent one.

`AS OF SYSTEM TIME` remains as a fast path inside the window. `scripts/expiry.py`
asks one question both ways and prints both answers:

```
question: what did this system believe about sartan lot 88, 45 days ago?

route A -- bitemporal reconstruction
  ANSWERED: v1 [info] No open recall affects sartan lot 88.

route B -- MVCC time-travel (AS OF SYSTEM TIME at the same instant)
  FAILED: batch timestamp ... must be after replica GC threshold ...

control -- both routes at an instant inside the GC window
  bitemporal: v[2]   MVCC: v[2]   agree: True
```

The control matters as much as the failure: inside the window the two agree
exactly, so the bitemporal model is reconstructing real history rather than
inventing a convenient one. Past the horizon, one route is gone and the other
still answers.

---

## Architecture

```
   openFDA                AWS Lambda            Amazon Bedrock
 enforcement +   ──────▶  ingest +     ──────▶  Titan V2 embeddings
   SPL labels             change detect         Claude (the agent)
                               │                        │
                               ▼                        ▼
              ┌─────────────────────────────────────────────────┐
              │            CockroachDB  (memory)                │
              │                                                 │
              │  fact          bitemporal claims + VECTOR(1024) │
              │  decision      what the agent said              │
              │  decision_read which claim VERSION it read      │
              │  correction    what changed and why             │
              │  outbox        exactly-once patient notices     │
              │                                                 │
              │  us-east-1    us-west-2    eu-west-1            │
              │  SURVIVE REGION FAILURE   REGIONAL BY ROW       │
              └─────────────────────────────────────────────────┘
                               │
                               ▼
                     Amazon S3: raw snapshots,
                     signed audit exports
```

Three properties carry the design.

**Provenance is written atomically with the answer.** The decision row and its
read set commit together or not at all. There is no code path that stores an
answer without recording what produced it, because an answer whose provenance
was lost can never be repaired, and a memory system that drops provenance
under load effectively has none.

**Corrections are exactly-once across a region failure.** The correction, the
status change, the outbox entry and the audit line are one transaction. The
outbox key is a deterministic hash of (decision, new verdict, triggering fact
version), so a sweep killed halfway and restarted recomputes the identical key
and the unique constraint turns the replay into a no-op. "Zero duplicate
patient notifications" is a property of the schema, not a hope about how the
process exits.

**Residency is enforced by storage, not by code.** `patient` is
`REGIONAL BY ROW`, so an EU patient's memory lives in `eu-west-1` because
CockroachDB puts it there.

The cluster runs 9 nodes across 3 simulated AWS regions under
`SURVIVE REGION FAILURE`, which places 5 replicas so no region holds a
majority.

---

## CockroachDB tools used

The hackathon requires two.

| Tool | How it is used | Status |
|---|---|---|
| **Distributed Vector Indexing** | `CREATE VECTOR INDEX fact_semantic ON fact (believed, embedding vector_cosine_ops)`. The `believed` prefix column is the point: without it a top-K search spends part of its budget on retracted claims that get filtered afterwards, so a query asking for 8 results quietly returns 3. Prefixing means all K neighbours come from claims currently held true. | live |
| **Agent Skills** | All 34 skills from [cockroachlabs/cockroachdb-skills](https://github.com/cockroachlabs/cockroachdb-skills) installed via `npx skills add`. `designing-application-transactions` was run against this codebase and found three real defects, listed below. | live |
| **Managed MCP Server** | `unsay/mcp.py` speaks JSON-RPC to `https://cockroachlabs.cloud/mcp` with a service-account key scoped to `mcp:read`, and `get_table_schema` returns the live definition of `fact` from the running cluster. Introspecting beats remembering: Cockroach Labs' stated reason for the server is that an agent working from a stale schema emits "brittle queries, schema mismatches, or unnecessary load". | live |
| **ccloud CLI** | Installed and authenticated; used for control-plane inspection of the hosted cluster. See the feedback note below on why the application does *not* drive it. | partial |

Status is per row on purpose, and this table is the single place to check what
the artifact does rather than what it intends to.

**Feedback on ccloud, offered because the hackathon asks for it.** ccloud is
presented as agent-ready, and its noun-verb shape and JSON output genuinely
are. But `ccloud auth login` is browser OAuth only: version 0.8.23 accepts no
API key, and `CCLOUD_API_KEY`, `COCKROACH_CLOUD_API_KEY`, `CC_API_KEY` and
`CCLOUD_API_TOKEN` are all ignored. An agent therefore cannot authenticate it
unattended, which is a real gap for the exact use the tool is being pitched
for. Everything this project needed from the control plane (read cluster
details, create a SQL user, provision a database) was done instead against the
Cloud REST API with a service-account key, which does support headless auth.
A `ccloud auth login --api-key` would close the gap.

### What the Agent Skills actually caught

Running `designing-application-transactions` against this repo found three
defects that were really there, not three suggestions:

1. **Read-modify-write on version numbers.** `assert_claim` did
   `SELECT max(version)` then `INSERT version+1` as two statements. Two
   ingesters racing on the same drug can both decide they are version N+1.
   Fixed by computing the version inside `INSERT ... SELECT`.
2. **`SELECT *` in `replay_decision`**, pulling a 1024-dimension embedding over
   the wire for no reason.
3. **`LIMIT` without keyset pagination in the sweep.** Offset-style paging
   re-scans and discards everything already processed on each page. Now paged
   by `decision_id`, so the sweep starts correcting before it has finished
   enumerating.

The first was a genuine correctness bug under concurrency. The contention test
below exists because the skill prompted it.

## AWS services used

| Service | How it is used | Status |
|---|---|---|
| **Amazon Bedrock** | Titan Text Embeddings V2 produces the 1024-dimension vectors `VECTOR(1024)` is sized for; all 554 claims are embedded with it. Claude Sonnet 4.5 reasons over retrieved claims and names its own citations. Haiku 4.5 does bulk fact extraction in the benchmark, where call volume is ~40x the answer path and the work is mechanical. | live |
| **AWS Lambda** | Hosts the demo behind a public function URL. Chosen because it charges nothing while idle, and judging is four weeks of mostly idle. | live |
| **Amazon S3** | Raw openFDA snapshots and signed audit exports. | planned |

---

## The AI is the engine, not a commentator

The model reads the retrieved claims, decides the verdict, writes the answer,
and names which claims it leaned on. That last part is load-bearing: an answer
can only be invalidated later by evidence the model itself said it used. Reads
it was merely shown are recorded but marked non-load-bearing, so a change to
background context does not spuriously invalidate an answer that never
depended on it. That is what keeps a sweep from becoming spam.

One guardrail sits around it: the model may not return `safe` while an active
Class I or Class II recall for the same product is in its context. The verdict
is raised to `stop` and the override logged. The model still writes the answer
and picks the citations; it just cannot answer away a recall.

---

## What is measured

Verified on a live 9-node, 3-region cluster:

| Property | Result |
|---|---|
| Real openFDA ingestion | 400 live enforcement records to **554 claims** across **315 drugs**, embedded with Titan V2 |
| Severity mix | 21 active Class I, 494 Class II, 39 Class III |
| Lot extraction | **455 of 554 (82.1%)** resolved to a specific lot rather than a whole drug |
| Idempotency | Re-ingesting identical claims produced **0** new versions |
| Sweep correctness | Standing answers built on superseded evidence found and reversed |
| Exactly-once | Sweep replayed after completion; outbox still held exactly 1 notice |
| Replay agreement | Bitemporal reconstruction and `AS OF SYSTEM TIME` returned identical read sets inside the window |
| **Replay durability** | At 45 days: bitemporal **answered exactly**, MVCC **failed** on the GC threshold |
| Concurrent writes | 128 writers across 128 claims: **176.9 memory writes/sec**, 0 failures |
| Worst-case contention | 64 writers, 16 racing on each of 4 claims: 0 failures, version chains dense at exactly v1..v16, exactly 1 believed version per claim, 0 orphaned provenance rows |

Idempotency matters more than it looks. openFDA refreshes weekly and most
records are unchanged. An ingester that versioned on every pass would fire 331
spurious sweeps and, at the far end, 331 spurious messages to patients.

**Which numbers used stub embeddings.** Only the contention test, where the
vectors are payload and the thing being measured is the database. Everything
about the corpus, retrieval and answers now runs on real Titan V2 vectors.

### LongMemEval, and what it does not yet show

[LongMemEval](https://github.com/xiaowu0162/LongMemEval) (ICLR 2025) graded by
its **own evaluator with the published GPT-4o judge**, not by me:

| Run | Result |
|---|---|
| temporal-reasoning, **oracle** split, n=20 | **15/20 = 75%** |

**This is not yet comparable to Zep's 63.8% or Mem0's 49.0%, and should not be
presented as beating them.** Those figures are the temporal-reasoning sub-task
measured on `longmemeval_s`, where the evidence sessions are buried in roughly
115k tokens of distractors. The oracle split contains only the evidence, so it
is a substantially easier task. A run against `s` is in progress; until it
finishes, 75% says the pipeline works, not that it wins.

Two further caveats worth stating rather than burying. At n=20 the 95%
confidence interval is roughly ±19 points, so this is a signal and not a
measurement. And Zep's Graphiti already stores `valid_at`/`invalid_at` on
every node and edge, so **bitemporal storage is not itself a novelty against
Zep**; what is different here is repairing past answers rather than only
reconstructing them, and a replay that outlives the GC window.

---

## Running it

```bash
docker compose up -d
docker compose --profile init run --rm init

docker exec -i cockroachdb-crdb-use1-1-1 cockroach sql --insecure < sql/001_bootstrap.sql
docker exec -i cockroachdb-crdb-use1-1-1 cockroach sql --insecure < sql/002_schema.sql
docker exec -i cockroachdb-crdb-use1-1-1 cockroach sql --insecure < sql/003_vector.sql

python -m venv .venv && .venv/Scripts/pip install -e .
cp .env.example .env      # then fill in AWS + CockroachDB Cloud values

unsay status
unsay ingest-recalls --since 2024-01-01
unsay ask "Is my valsartan safe to keep taking?" --subject valsartan
unsay sweep --dry-run
```

Kill a region mid-demo and watch answers keep flowing:

```bash
docker compose stop crdb-euw1-1 crdb-euw1-2 crdb-euw1-3
unsay status          # eu-west-1 DOWN, survival goal holds
unsay sweep           # completes; outbox count does not double
```

Evidence scripts, all runnable without AWS via `UNSAY_ALLOW_FAKE_EMBEDDINGS=1`:

| Script | Proves |
|---|---|
| `scripts/smoke.py` | The full lifecycle: assert, answer with provenance, supersede, sweep, correct, exactly-once |
| `scripts/expiry.py` | Bitemporal replay outlives the GC window; MVCC replay does not |
| `scripts/concurrency.py` | Version chains stay dense and singly-believed under N-way races |

That stub-embedding switch is gated behind an environment variable and never a
silent fallback, because retrieval quality under stub embeddings is
meaningless.

---

## Limitations

Named plainly, because a safety tool that oversells itself is worse than none.

- **Lot extraction is regex over free text.** openFDA writes lot numbers into
  prose, not a structured field. Recalls with no parsable lot fall back to
  drug-level scope, which over-notifies rather than under-notifies.
- **Dispensing data is synthetic.** Real pharmacy dispensing records are PHI
  and are not obtainable for a hackathon. The FDA side is entirely real and
  live; the patient side is generated.
- **Not a medical device.** openFDA's own terms state the data is unvalidated
  and must not be relied on for decisions regarding medical care. Unsay drafts
  a correction for a pharmacist to review. It does not contact patients
  autonomously.
- **`crdb_internal` is avoided.** v26.2 restricts it with a hint that it is
  unsupported in production. Cluster status is read through supported SQL and
  connection probes instead of setting `allow_unsafe_internals`.
- **Region-failure survival needs a licence.** Enterprise Free, at no cost for
  companies under $10M revenue, from the CockroachDB Cloud console. Without it
  the schema still works single-region.

## Licence

MIT. See [LICENSE](LICENSE).
