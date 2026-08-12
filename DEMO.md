# Demo script

Target 90 seconds. Hard ceiling 3 minutes, but the ceiling is not the goal.

The rule this script is built around: **open on a person getting something
they want, close on the receipt.** The proof of correctness is a 15-second
closing beat, never the climax. If the emotional peak of this video is a table
of zeros turning green, the wrong thing got built.

One feature is on show: **the agent goes back and un-says what it told you.**
Everything else is supporting and gets cut before that gets cut.

---

## Shot list

### 0:00 - 0:10 | The ordinary thing
**Screen:** the Unsay web UI. A patient question typed in plain language.

> "Margaret asks the pharmacy whether her blood-pressure tablets are still
> safe to take. The agent checks. There was a recall on her lot, but it closed
> in July, so it tells her to keep taking them and why."

Answer appears: **CAUTION**, with the FDA claim it relied on shown underneath,
lot GB01616 visible.

*Nothing remarkable has happened yet. That is the point.*

### 0:10 - 0:22 | The world moves
**Screen:** the real openFDA enforcement record arriving, dated.

> "Six days later the FDA publishes a Class I recall on the exact lot she was
> dispensed. Class I means a reasonable probability of serious harm or death."

**Screen:** the claim versioning in real time. v1 retracted, v2 asserted.

> "The answer Margaret got was correct when she got it. It is wrong now, and
> nothing about the answer changed. The world moved underneath it."

### 0:22 - 0:52 | The one feature
**Screen:** the sweep firing. Counter climbing.

> "This is the part no other agent memory can do."

**Screen:** the three-table join, on screen for three seconds, no narration
over it.

> "Every answer still standing that leaned on a version of a claim we no
> longer believe. A vector store cannot ask this question. It has no idea
> which version of a memory an answer was built on."

**Screen:** the corrections list populating with names.

> "Twelve people were told to keep taking it. Here they are, by name. Each
> one re-decided against what the FDA knows today, each one a correction
> drafted for a pharmacist to sign off."

*This is the peak. Hold on the named list, not on the counter. The real run
takes about 45 seconds for twelve; cut or speed-ramp the middle, but let the
names land one by one at the start and end.*

### 0:52 - 1:12 | It does not stop
**Screen:** terminal, `docker compose stop crdb-euw1-*`, mid-sweep.

> "Halfway through, I take out an entire AWS region."

**Screen:** `unsay status`, eu-west-1 DOWN, survival goal holds. Sweep
continues in the other pane.

> "The sweep finishes. No memory lost, no answer half-corrected."

**Screen:** outbox count before and after the restart, identical.

> "And Margaret is not told twice, because the notification key is derived
> from the correction itself. A replayed sweep computes the same key and the
> database refuses the duplicate."

### 1:12 - 1:25 | The receipt
**Screen:** `unsay replay <decision_id>`.

> "For any answer it ever gave, it can show exactly what it knew at the moment
> it decided, and what has moved since."

Evidence table: v1 as read, v2 now, SUPERSEDED in red.

**Screen:** `scripts/expiry.py` output, both routes side by side.

> "Plenty of projects can replay a decision. They do it with time-travel
> queries, which expire after twenty-five hours. Ask this one what it believed
> forty-five days ago and it still answers."

*Hold on the two lines: bitemporal ANSWERED, MVCC FAILED. Five seconds, no
narration over it.*

### 1:25 - 1:32 | Close
> "Unsay. Agent memory that can take back what it said. Built on CockroachDB
> because remembering is easy, and knowing when you were wrong is a database
> problem."

---

## Recording notes

- **Record the sweep beat first.** It is the only shot that must be perfect.
  Everything else can be re-shot cheaply.
- **Real data only, and the verified scenario is already picked.** Use
  `amlodipine-besylate-and-benazepril-hydrochloride`, lot `GB01616`. Its only
  recall in the corpus genuinely terminated on 2026-07-30 (a mislabelled
  expiry date), so the honest opening verdict is CAUTION rather than SAFE, and
  escalating that same fact_key to Class I gives a real CAUTION -> STOP
  reversal. Verified end to end against the live demo: examined 1, reversed 1,
  notified 1, in 3.14s.

  Resist the urge to stage a SAFE opening. Every claim in the corpus is a
  recall, so "safe" would require inventing a clean baseline, and the caution
  reversal reads just as strongly without the invention.
- **Do not narrate the architecture.** The two-plane schema, the bitemporal
  columns, and the vector index prefix belong in the README and the writeup.
  Naming them here costs seconds and buys nothing, because the judge cannot
  verify them from a video anyway.
- **Show the region kill actually being typed.** A cut to an already-dead
  region reads as staged.
- **No music under the sweep.** Let the counter and the names carry it.

## What is deliberately not in this video

- The schema diagram
- The contention test and the throughput number
- LongMemEval results
- The residency / `REGIONAL BY ROW` story
- The MCP and ccloud integrations

All of these are real and all belong in the README and the Devpost writeup.
None survive contact with a 90-second budget, and a demo that lists six things
is a demo that shows none of them.
