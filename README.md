# Recall

**An AI medication-safety agent that goes back and un-says what it told you.**

Ask it whether your prescription is safe and it answers from live FDA data. The
part that matters comes later: when the FDA recalls that drug next Tuesday,
Recall finds every person it already reassured, works out which of those
answers are now wrong, and corrects them. Each one, by name, in seconds.

That second half is impossible for every agent memory product on the market
today, and it is impossible for a specific, structural reason. This README is
mostly about that reason.

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
through their EHR portal. It ran into two walls. Most recalls are Class II, so
notification "stops with wholesalers and pharmacies rather than reaching
patients." And critically: *"it was not possible to trace a medication
prescription from the EHR to specific lot numbers dispensed to that patient by
a community pharmacy."*

A recall is scoped to a lot. If you cannot resolve a lot to a person, you
either tell everybody or you tell nobody.

---

## The one thing Recall does that nothing else can

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

**A vector database cannot express this query.** Not slowly, not approximately.
It has no notion that a memory has versions, and no record of which version a
given answer consumed. The most it can return is today's nearest neighbours,
which tells you exactly nothing about what you said in March.

Mem0, Zep, Letta, and Pinecone all store what is true now. Recall stores what
was true when, and what each answer stood on. That is a database problem, and
it is why the memory layer here is CockroachDB rather than an index.

---

## Why CockroachDB specifically

Three properties, each load-bearing.

**1. Bitemporal claims.** Every safety claim carries two independent time
axes. `valid_from`/`valid_to` is when it was true in the world. `asserted_at`/
`retracted_at` is when this system believed it. Keeping them apart is what
separates "the drug became dangerous on March 3rd" from "we found out on July
2nd", which is the difference between an unlucky answer and a negligent one.

A claim is never updated in place. The version we believed is retracted and a
new version is asserted beside it, in one transaction. Nothing a past decision
read is ever mutated or deleted.

**2. Provenance written atomically with the answer.** The decision row and its
read set commit together or not at all. There is no code path that stores an
answer without recording what produced it, because an answer whose provenance
was lost can never be repaired, and a memory system that drops provenance under
load effectively has none.

**3. Exactly-once correction across a region failure.** The correction, the
status change, the outbox entry, and the audit line are one transaction. The
outbox key is a deterministic hash of (decision, new verdict, triggering fact
version), so a sweep killed halfway and restarted recomputes the identical key
and the unique constraint turns the replay into a no-op.

"Zero duplicate patient notifications across a region failure" is therefore a
property of the schema, not a hope about how the process exits.

The cluster runs 9 nodes across 3 simulated AWS regions under
`SURVIVE REGION FAILURE`, which places 5 replicas so no region holds a
majority. Losing a whole region costs latency, never availability, and never a
committed write.

### An honest note on `AS OF SYSTEM TIME`

MVCC time-travel is the obvious way to build this, and it is the wrong
mechanism. CockroachDB's own docs are explicit that `gc.ttlseconds` "is not
meant to be a solution for long-term retention of history; for that you should
handle versioning in the schema design at the application layer." The default
window is 4 hours; 25 is the largest value Cockroach Labs regularly tests.

So the durable mechanism is the bitemporal schema, which is exact and
unbounded. `AS OF SYSTEM TIME` is a complementary fast path for same-day
forensics, and the test suite asserts the two agree inside the window.

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

Data residency is enforced by the storage layer, not by application code:
`patient` is `REGIONAL BY ROW`, so an EU patient's memory lives in `eu-west-1`
because CockroachDB puts it there.

---

## CockroachDB tools used

The hackathon requires two. Recall uses all four.

| Tool | How it is used | Status |
|---|---|---|
| **Distributed Vector Indexing** | `CREATE VECTOR INDEX fact_semantic ON fact (believed, embedding vector_cosine_ops)`. The `believed` prefix column is the point: without it a top-K search spends part of its budget on retracted claims that get filtered afterwards, so a query asking for 8 results quietly returns 3. Prefixing means all K neighbours come from claims currently held true. | live |
| **Managed MCP Server** | The agent introspects live schema through `https://cockroachlabs.cloud/mcp` in read-only mode before generating SQL, which is Cockroach Labs' own stated fix for schema hallucination. | **in progress** |
| **ccloud CLI** | Provisions the Cloud cluster, configures networking, and pulls audit logs. JSON output on every command is what makes it drivable by an agent rather than a person. | **in progress** |
| **Agent Skills** | [cockroachlabs/cockroachdb-skills](https://github.com/cockroachlabs/cockroachdb-skills) vendored into `.claude/skills/`, driving schema-design and performance review of this repo. | **in progress** |

Status is stated per row on purpose. Anything marked "in progress" is not
wired up yet, and this table is the single place to check what the artifact
actually does versus what it is heading towards.

## AWS services used

| Service | How it is used |
|---|---|
| **Amazon Bedrock** | Claude does the reasoning and picks its own citations. Titan Text Embeddings V2 produces the 1024-dimension vectors that `VECTOR(1024)` is sized for. |
| **AWS Lambda** | Scheduled openFDA ingestion and change detection. A new fact version is what triggers a sweep. |
| **Amazon S3** | Raw openFDA snapshots and signed audit exports. |
| **Amazon EC2** | Hosts the 9-node cluster and the API. |

---

## The AI is the engine, not a commentator

The model reads the retrieved claims, decides the verdict, writes the answer,
and names which claims it actually leaned on. That last part is load-bearing:
an answer can only be invalidated later by evidence the model itself said it
used. Reads it was merely shown are recorded but marked non-load-bearing, so a
change to background context does not spuriously invalidate an answer that
never depended on it. That is what keeps a sweep from becoming spam.

There is exactly one guardrail around it: the model may not return `safe` while
an active Class I or Class II recall for the same product sits in its context.
The verdict is raised to `stop` and the override is logged. The model still
writes the answer and picks the citations; it just cannot answer away a recall.

---

## What is measured

Verified on a live 9-node, 3-region cluster:

| Property | Result |
|---|---|
| Real openFDA ingestion | 250 live enforcement records to 331 lot-scoped claims in 40.7s |
| Lot extraction | 270 of 331 claims (81.6%) resolved to a specific lot rather than a whole drug |
| Idempotency | Re-ingesting the identical 331 claims produced **0** new versions |
| Sweep correctness | Standing answers built on superseded evidence found and reversed |
| Exactly-once | Sweep replayed after completion; outbox still held exactly 1 notice |
| Replay agreement | Bitemporal reconstruction and `AS OF SYSTEM TIME` returned identical read sets |

Idempotency matters more than it looks. openFDA refreshes weekly and most
records are unchanged. An ingester that versioned on every pass would fire 331
spurious sweeps and, at the far end, 331 spurious messages to patients.

**Still to land before submission:** accuracy on the `knowledge-update` and
`temporal-reasoning` subsets of [LongMemEval](https://github.com/xiaowu0162/LongMemEval)
(ICLR 2025), against the published baselines of Zep at 63.8% and Mem0 at 49.0%.
Those are the two categories that measure exactly what this design targets, and
the benchmark is external rather than one I wrote, which is the point.

---

## Running it

```bash
docker compose up -d
docker compose --profile init run --rm init

docker exec -i <use1-1-container> cockroach sql --insecure < sql/001_bootstrap.sql
docker exec -i <use1-1-container> cockroach sql --insecure < sql/002_schema.sql
docker exec -i <use1-1-container> cockroach sql --insecure < sql/003_vector.sql

python -m venv .venv && .venv/Scripts/pip install -e .
cp .env.example .env      # then fill in AWS + CockroachDB Cloud values

recall status
recall ingest-recalls --since 2024-01-01
recall ask "Is my valsartan safe to keep taking?" --subject valsartan
recall sweep --dry-run
```

Kill a region mid-demo and watch answers keep flowing:

```bash
docker compose stop crdb-euw1-1 crdb-euw1-2 crdb-euw1-3
recall status          # eu-west-1 DOWN, survival goal holds
recall sweep           # completes; outbox count does not double
```

`scripts/smoke.py` walks the full lifecycle end to end and runs without AWS
credentials via `RECALL_ALLOW_FAKE_EMBEDDINGS=1`. That switch is gated behind
an environment variable and never a silent fallback, because retrieval quality
under stub embeddings is meaningless.

**Which of the numbers above used stub embeddings.** All of them, so far. The
FDA records, the lot extraction, the versioning, the sweep, the exactly-once
behaviour and both replay paths are real and were measured on the live
9-node cluster. The *vectors* were not: every run to date set
`RECALL_ALLOW_FAKE_EMBEDDINGS=1`. That means the structural results hold
exactly as stated and no claim about **retrieval quality** has been earned
yet. Re-ingestion against Bedrock Titan V2, and the LongMemEval run that
depends on it, are the next things to land.

---

## Limitations

Named plainly, because a safety tool that oversells itself is worse than none.

- **Lot extraction is regex over free text.** openFDA writes lot numbers into
  prose, not a structured field. Recalls with no parsable lot fall back to
  drug-level scope, which over-notifies rather than under-notifies. A
  production build would reconcile against pharmacy dispensing records.
- **Dispensing data is synthetic.** Real pharmacy dispensing records are PHI
  and are not obtainable for a hackathon. The FDA side is entirely real and
  live; the patient side is generated.
- **Not a medical device.** openFDA's own terms state the data is unvalidated
  and must not be relied on for decisions regarding medical care. Recall drafts
  a correction for a pharmacist to review. It does not contact patients
  autonomously.
- **`crdb_internal` is avoided.** v26.2 restricts it with a hint that it is
  unsupported in production. Cluster status is read through supported SQL and
  connection probes instead of setting `allow_unsafe_internals`.
- **Region-failure survival needs a licence.** Free for companies under $10M
  revenue via the CockroachDB Cloud console. Without it the schema still works
  single-region.

## Licence

MIT. See [LICENSE](LICENSE).
