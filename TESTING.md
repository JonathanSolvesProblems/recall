# How to verify Unsay

Ordered by effort. Section 1 needs nothing installed and takes about two
minutes; section 5 rebuilds everything from scratch.

If you would rather watch first, the
[2:34 demo](https://www.youtube.com/watch?v=UiWwvPHfN3A) walks the same path
this document tells you how to reproduce.

Every claim in the README maps to something runnable here. Where a check can
only be done one way, that is said rather than implied.

---

## 1. Zero setup: the hosted demo (~2 minutes)

**<https://dzcqeaznqrb2dwvy3h4btiavrm0etzef.lambda-url.us-east-1.on.aws/>**

Lambda in front of a CockroachDB Cloud cluster holding 554 live openFDA claims.
The first request after a quiet spell pays a cold start of a few seconds; that
is Lambda waking, not the database.

Walk the four numbered steps on the page, in order:

| Step | Do | Expect |
|---|---|---|
| 1 | Pick a patient from the dropdown, click **Ask** | **CAUTION**. Beneath it, the FDA claim it read, with the fact key and version. The bold left rule marks a load-bearing read |
| 2 | Click **Publish Class I recall** | The same fact key moves `v1 → v2`, prior version retracted |
| 3 | Click **Find affected answers** | **12** standing answers, each listing the fact version it stood on |
| 3 | Click **Run sweep** | ~4s. Then **12 examined, 9 reversed, 9 notified**. The other three had already said stop, so they are reaffirmed rather than corrected, and are deliberately not notified |
| 4 | Scroll down | Nine correction notices, each naming a person, each `CAUTION → STOP`, each with a once-only key. **Three** distinct texts across the nine: the twelve ask in four phrasings, three of which open at CAUTION and get reversed, while the fourth ("should I be worried about my blood pressure tablets") already opens at STOP and is reaffirmed instead. That fourth phrasing is the three patients who are deliberately not messaged |

Then click **Run sweep** a second time. The outbox still holds **9** notices,
not 18: the dedupe key is derived from the correction itself, so a replayed
sweep is a no-op. This is the exactly-once claim, and it is the one worth
checking yourself because it is the easiest to assert and hardest to believe.

**The demo restores itself.** The sweep is destructive by design: it moves
every standing answer to `reversed`, so a second visitor would otherwise find
the buttons do nothing. Loading the page checks `/api/demo/state` and, if the
previous visitor spent the scenario, restores it before you touch anything.
There is also a **Reset demo** button in step 4 if you want to run it twice
yourself.

### What section 1 does *not* show

Region failure. The hosted cluster is CockroachDB Cloud **Basic**, which is
single-region, so its survival goal is `zone` and there is no region to remove.
Multi-region is an Advanced-tier feature. The real thing runs locally on a
9-node cluster, in section 3.

---

## 2. Local setup (~10 minutes, mostly downloads)

Needs Docker, Python 3.11+, and roughly 6 GB of RAM for the cluster.

```bash
git clone https://github.com/JonathanSolvesProblems/unsay
cd unsay

docker compose up -d                        # 9 nodes, 3 simulated AWS regions
docker compose --profile init run --rm init # ~30s for gossip to settle

C=cockroachdb-crdb-use1-1-1
docker exec -i $C cockroach sql --insecure < sql/001_bootstrap.sql
docker exec -i $C cockroach sql --insecure < sql/002_schema.sql
docker exec -i $C cockroach sql --insecure < sql/003_vector.sql

python -m venv .venv
.venv/Scripts/pip install -e .              # .venv/bin/pip on macOS/Linux
```

Confirm the topology:

```bash
docker exec -i $C cockroach sql --insecure -d unsay \
  -e "SHOW SURVIVAL GOAL FROM DATABASE unsay; SHOW REGIONS FROM DATABASE unsay;"
```

Expect `survival_goal = region` and three regions with three zones each.

`001_bootstrap.sql` sets multi-region topology and needs an Enterprise Free
licence (free under $10M revenue, from the CockroachDB Cloud console). Without
one the cluster runs unlicensed for 7 days and then throttles to 5 concurrent
transactions. Skip that file to run single-region; everything except section 3
still works.

---

## 3. The evidence scripts

All three run **without AWS credentials** via `UNSAY_ALLOW_FAKE_EMBEDDINGS=1`.
That switch is gated behind an environment variable and is never a silent
fallback, because retrieval quality under stub vectors is meaningless. It is
set here because these three test *structure*, not retrieval.

```bash
UNSAY_ALLOW_FAKE_EMBEDDINGS=1 .venv/Scripts/python scripts/smoke.py
UNSAY_ALLOW_FAKE_EMBEDDINGS=1 .venv/Scripts/python scripts/expiry.py
UNSAY_ALLOW_FAKE_EMBEDDINGS=1 .venv/Scripts/python scripts/concurrency.py 64 4
```

### `smoke.py`: the full lifecycle

Asserts a claim, answers with provenance, supersedes, sweeps, corrects,
notifies, then replays the sweep. Ends `SMOKE PASSED`. Checks in order:

- an identical re-ingest creates **no** new version (weekly openFDA refreshes
  must not fire spurious corrections)
- retrieval returns only the believed version after supersession
- both replay routes agree inside the GC window
- the sweep finds exactly the affected answer
- a replayed sweep leaves the outbox at exactly 1

### `expiry.py`: the claim that most needs checking

Asks one question two ways at 45 days back. Ends
`PASSED: replay outlives the garbage-collection window.`

```
route A -- bitemporal reconstruction
  ANSWERED: v1 [info] No open recall affects sartan lot 88.
route B -- MVCC time-travel (AS OF SYSTEM TIME at the same instant)
  FAILED: batch timestamp ... must be after replica GC threshold ...
control -- both routes at an instant inside the GC window
  bitemporal: v[2]   MVCC: v[2]   agree: True
```

The control is the part that matters. Inside the window the two routes agree
exactly, so the bitemporal model is reconstructing real history rather than a
convenient one. Past the horizon only one still answers.

### `concurrency.py`: invariants under contention

`64 4` runs 64 writers with 16 racing on each of 4 claims. Ends
`CONTENTION TEST PASSED`, having checked that version chains are dense
(`v1..v16`, no gaps, no duplicates), exactly one version per claim is believed,
and no provenance row points at a missing version.

`128 128` instead measures uncontended throughput (~177 writes/sec on a laptop
9-node cluster). The `64 4` numbers are worst-case contention, not throughput,
and should not be read as one.

### Region failure (the part the hosted demo cannot show)

With the local cluster up:

```bash
docker compose stop crdb-euw1-1 crdb-euw1-2 crdb-euw1-3
.venv/Scripts/python -m unsay.cli status
```

`eu-west-1` reports **DOWN**, the survival goal still reads `region`, and
queries keep answering. `SURVIVE REGION FAILURE` places 5 replicas so no single
region holds a majority. Bring it back with `docker compose start crdb-euw1-1
crdb-euw1-2 crdb-euw1-3`.

The two lines to check are `survival goal` and `regions up`. The memory counts
underneath depend entirely on what you have ingested locally, and `smoke.py`
clears the tables as its first step, so run this before an ingest and expect
single digits, or after one and expect hundreds. The demo video was recorded
against a locally ingested corpus; your numbers will be your own.

---

## 4. With AWS credentials: the real path

Needs Bedrock access to `claude-sonnet-4-5`, `claude-haiku-4-5` and
`titan-embed-text-v2` in `us-east-1`, plus `.env` from `.env.example`.

```bash
.venv/Scripts/python -m unsay.cli ingest-recalls --since 2025-01-01 --limit 400
.venv/Scripts/python -m unsay.cli status
.venv/Scripts/python -m unsay.cli ask "Is my cefazolin injection safe?" --subject cefazolin
.venv/Scripts/python -m unsay.cli sweep --dry-run
```

Ingest takes ~9 minutes for 554 claims: Titan has no batch endpoint, so it is
554 sequential calls. Expect ~82% of claims to resolve to a specific lot rather
than a whole drug.

Reset the hosted demo to its opening state:

```bash
.venv/Scripts/python scripts/seed_demo.py --cloud --patients 12
```

That clears prior decisions, restores the lot's claim to v1, seeds 12 synthetic
patients each dispensed lot `GB01616`, and records a standing answer for each.
Patient data is synthetic and disclosed as such; the drug, lot, recall and
termination date are all real openFDA records.

### Verifying the MCP server

```bash
.venv/Scripts/python -m unsay.cli schema fact
```

Prints the endpoint and scope, then the live `CREATE TABLE` for `fact` as the
Managed MCP Server reports it right now, then the tool count. Expect 12 tools,
the `believed BOOL ... AS (retracted_at IS NULL) STORED` computed column, and
`VECTOR INDEX fact_semantic (believed, embedding vector_cosine_ops)`. This is
the same call the answer path makes before the model reasons, which is the
point: the schema is read at request time rather than carried in the prompt.
Needs `CRDB_MCP_API_KEY` (service account, `mcp:read`) and `CRDB_CLUSTER_ID`.

### Verifying the ccloud preflight

```bash
.venv/Scripts/python -m unsay.cli cluster-health
```

The same check runs automatically ahead of `ingest-recalls` and
`ingest-warnings`, and aborts them if the control plane says the cluster is not
fit to write to. `--skip-preflight` bypasses it. If ccloud is not installed or
not authenticated the ingest warns and proceeds, because `ccloud auth login` is
browser OAuth only and a fresh clone will not have a session.

---

## 5. The benchmark

```bash
.venv/Scripts/python bench/longmemeval.py --split s --types temporal-reasoning \
  --limit 40 --workers 1 --out bench/results/s_run.jsonl
```

Then grade with the **official** evaluator, not with anything in this repo:

```bash
git clone https://github.com/xiaowu0162/LongMemEval
cd LongMemEval/src/evaluation
OPENAI_API_KEY=... python evaluate_qa.py gpt-4o <hyp.jsonl> <longmemeval_s.json>
```

Two things worth knowing before reading any number:

**Split matters more than the score.** `--split oracle` contains only the
evidence sessions and is much easier. `--split s` buries them in ~115k tokens
of distractors and is what Zep's 63.8% and Mem0's 49.0% are measured on. An
oracle number must not be compared to those.

**Throughput is quota-bound.** A new AWS account sustains roughly 6 Bedrock
calls per minute, and one `s` instance needs ~47 sequential extraction calls,
so 40 instances is about 5 hours. The run appends and resumes: re-running the
same command skips instances already in the output file.

The harness refuses to run under `UNSAY_ALLOW_FAKE_EMBEDDINGS`, and reports
any instance it skipped or any session whose extraction failed rather than
letting either be silently scored as a wrong answer.

---

## Known limitations

- Lot extraction is regex over openFDA free text; recalls with no parsable lot
  fall back to drug-level scope, which over-notifies rather than under.
- Dispensing data is synthetic. Real pharmacy records are PHI.
- Not a medical device. openFDA's own terms state the data is unvalidated and
  must not be relied on for medical care. Unsay drafts a correction for a
  pharmacist; it does not contact patients.
- The hosted demo is writable by anyone with the URL, by design. It has no
  destructive endpoint, and section 4 shows how to reset it.
