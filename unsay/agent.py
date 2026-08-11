"""The agent: reasoning over memory via Amazon Bedrock.

The model is the engine here, not a commentator on a rule engine. It reads the
retrieved claims, decides the verdict, and names which claims it actually
leaned on. That last part is what the sweep depends on: an answer can only be
invalidated later by evidence the model itself said it used.

What the model is *not* allowed to do is invent a safety claim. Every sentence
it produces has to trace to a retrieved fact version, and the verdict is
clamped against the severity of what was retrieved, so a hallucinated
reassurance cannot override a Class I recall sitting in the context.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.config import Config

from unsay import memory, sweep
from unsay.config import settings

log = logging.getLogger(__name__)

_client = None

SYSTEM = """You are a medication-safety assistant for a pharmacy.

You answer only from the SAFETY CLAIMS provided. Each claim carries a key and a
version. You must not introduce any drug-safety information that is not in
those claims. If the claims do not cover the question, say so.

Reply with a single JSON object and nothing else:

{
  "verdict": "safe" | "caution" | "stop" | "unknown",
  "answer": "two or three plain sentences addressed to the patient",
  "cited": ["<fact_key of every claim you actually relied on>"],
  "confidence": 0.0 to 1.0
}

Verdict meanings:
  stop     an active recall or contraindication applies to this exact product
  caution  a real risk applies but does not require stopping
  safe     the claims affirmatively cover this product and show no active risk
  unknown  the claims do not answer the question

"cited" must list only claims that changed your verdict. Do not pad it.
Write for a worried person, not a clinician. No hedging boilerplate."""


@dataclass
class Answer:
    verdict: str
    answer: str
    cited: set[str]
    confidence: float
    decision_id: str | None = None


def client():
    global _client
    if _client is None:
        cfg = settings()
        _client = boto3.client(
            "bedrock-runtime",
            region_name=cfg.aws_region,
            config=Config(retries={"max_attempts": 4, "mode": "adaptive"}),
        )
    return _client


def _render(claims: list[memory.Retrieved]) -> str:
    if not claims:
        return "SAFETY CLAIMS: (none found)"
    lines = ["SAFETY CLAIMS:"]
    for c in claims:
        window = c.valid_from.date().isoformat()
        window += f" to {c.valid_to.date().isoformat()}" if c.valid_to else " to present"
        lines.append(
            f"- key={c.fact_key} v{c.version} severity={c.severity} valid={window}\n"
            f"  {c.claim}"
        )
    return "\n".join(lines)


def call_text(
    system: str, prompt: str, *, max_tokens: int = 700, prefill: str | None = None,
    model_id: str | None = None,
) -> str:
    """One Bedrock Converse call, returning raw text.

    Temperature is pinned at 0. A safety verdict that changes between identical
    runs is not a verdict, and a benchmark that does the same is not a
    measurement.

    ``prefill`` seeds the assistant turn. Continuing a reply is a much stronger
    constraint than instructing one, which matters when the caller needs a
    machine-readable answer rather than a conversational one.
    """
    cfg = settings()
    messages: list[dict[str, Any]] = [{"role": "user", "content": [{"text": prompt}]}]
    if prefill:
        messages.append({"role": "assistant", "content": [{"text": prefill}]})

    resp = client().converse(
        modelId=model_id or cfg.bedrock_model_id,
        system=[{"text": system}],
        messages=messages,
        inferenceConfig={"maxTokens": max_tokens, "temperature": 0.0},
    )
    text = resp["output"]["message"]["content"][0]["text"]
    return ((prefill or "") + text).strip()


def call_json(
    system: str, prompt: str, *, max_tokens: int = 700, model_id: str | None = None
) -> dict[str, Any]:
    """As ``call_text``, parsing the reply as a JSON object.

    Prefilled with an opening brace so the model is continuing a JSON document
    rather than deciding whether to write one. Without this, a session that
    reads like an invitation to chat gets a conversational reply, the parse
    fails, and the caller sees an empty result that is indistinguishable from
    "nothing worth extracting".
    """
    text = call_text(system, prompt, max_tokens=max_tokens, prefill="{", model_id=model_id)
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError(f"model did not return JSON: {text[:200]}")
    return json.loads(match.group(0))


def _invoke(prompt: str) -> dict[str, Any]:
    return call_json(SYSTEM, prompt)


# Severity that, if present in the retrieved claims, the model is not permitted
# to answer away. A Class I recall is the FDA's "reasonable probability of
# serious adverse health consequences or death" tier.
BLOCKING = {"class_i", "class_ii"}


SEVERITY_WORDS = {"class_i": "Class I", "class_ii": "Class II"}


def _clamp(
    verdict: str, claims: list[memory.Retrieved]
) -> tuple[str, list[memory.Retrieved]]:
    """Refuse to let a reassuring verdict stand over an active serious recall.

    Returns the possibly-corrected verdict and the claims that forced it.
    This is a guardrail on the model, not a replacement for it: the model still
    writes the answer and picks the citations. It just cannot say "safe" while
    a Class I recall for the same product is sitting in front of it.
    """
    serious = [c for c in claims if c.severity in BLOCKING and c.valid_to is None]
    if serious and verdict in {"safe", "unknown"}:
        return "stop", serious
    return verdict, []


def _stop_lead(serious: list[memory.Retrieved]) -> str:
    """The sentence an overridden answer has to open with.

    When the clamp fires, the model has usually written something hedged, and
    hedged prose under a STOP verdict is the worst of both: the reader takes
    the tone, not the label. The directive goes first, and the model's own
    wording follows it as detail.
    """
    worst = min(serious, key=lambda c: 0 if c.severity == "class_i" else 1)
    label = SEVERITY_WORDS.get(worst.severity, "safety")
    return (
        f"Stop using this product and contact your pharmacy. An active {label} "
        f"recall applies to it."
    )


def ask(
    question: str,
    *,
    subject_id: str | None = None,
    patient_id: str | None = None,
    k: int = 8,
    persist: bool = True,
) -> Answer:
    """Answer a question from memory and record what it was answered from."""
    claims = memory.retrieve(question, subject_id=subject_id, k=k)
    raw = _invoke(f"{_render(claims)}\n\nPATIENT QUESTION: {question}")

    verdict = str(raw.get("verdict", "unknown")).lower()
    cited = {c for c in raw.get("cited", []) if isinstance(c, str)}
    # A citation the model invented is not provenance.
    known = {c.fact_key for c in claims}
    cited &= known

    answer_text = str(raw.get("answer", "")).strip()
    confidence = float(raw.get("confidence", 0.0))

    verdict, forced = _clamp(verdict, claims)
    if forced:
        keys = ", ".join(c.fact_key for c in forced[:3])
        log.warning("verdict raised to stop: active %s claim (%s)", forced[0].severity, keys)
        # The claims that forced the override are load-bearing by definition:
        # they are the reason the verdict is what it is, so a later change to
        # any of them must put this answer back in the sweep.
        cited |= {c.fact_key for c in forced}
        answer_text = f"{_stop_lead(forced)} {answer_text}".strip()
        # The model's own confidence described a verdict it no longer holds.
        confidence = max(confidence, 0.9)

    result = Answer(
        verdict=verdict,
        answer=answer_text,
        cited=cited,
        confidence=confidence,
    )

    if persist:
        rec = memory.record_decision(
            question=question, answer=result.answer, verdict=result.verdict,
            confidence=result.confidence, model_id=settings().bedrock_model_id,
            retrieved=claims, cited=result.cited, patient_id=patient_id,
        )
        result.decision_id = str(rec["decision_id"])

    return result


def reevaluate(candidate: sweep.Candidate) -> tuple[str, str]:
    """Re-decide a past answer against current memory. Passed to ``run_sweep``.

    Deliberately re-runs full retrieval rather than patching the old answer.
    The original question is asked again as if for the first time, so the
    correction reflects everything known now, not just the one claim that
    happened to change.
    """
    subject = None
    if candidate.stale:
        key = candidate.stale[0].get("fact_key", "")
        parts = key.split(":")
        if len(parts) > 1:
            subject = parts[1]

    fresh = ask(candidate.question, subject_id=subject, persist=False)
    return fresh.verdict, fresh.answer
