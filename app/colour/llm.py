"""Tier 3 — ask OpenAI which colour owns which code.

WHAT THE MODEL IS ACTUALLY FOR, because it changes the whole design:

This is not "generate a code for Yellow". The codes are already known — they
were extracted from the payload's own combination SKUs. Both sides of the
problem are closed sets of the same size. The model's job is to produce the
bijection between them:

    colours: ["Lime", "Orange"]     codes: ["LE", "OE"]     -> which is which

That framing buys three things. It is a small prompt (never more than ~10 items
a side). It is checkable — a proposal either forms a complete bijection or it
does not. And it is not a test of colour knowledge, so a small model is
appropriate; gpt-4o-mini is the default and there is no evidence yet that
anything larger would do better.

A PROPOSAL IS NOT A DECISION. Whatever comes back here goes through the same
verification every other tier's answer does (combination_skus.combination_map:
every payload combination consumed exactly once, counts equal colours x sizes).
A model that hallucinates a code produces a failed row — the same outcome as
today's unresolvable colour — and never a wrong SKU in the catalogue.

Absent OPENAI_API_KEY this module is inert and resolution behaves exactly as it
did before it existed.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

from app.config import settings

log = logging.getLogger("colour.llm")

TIMEOUT_SECONDS = 10.0
TRANSPORT_RETRIES = 2

SYSTEM_PROMPT = """\
You match product colour names to their short SKU codes.

You are given a list of colour names and a list of short codes taken from the \
same product's SKUs. Both lists are complete and the same length. Every colour \
has exactly one code and every code belongs to exactly one colour.

Codes are usually built from the colour name — often the first letter of each \
word plus the last letter of the final word (Yellow->YW, Grey->GY, \
Hi-Vis Orange->HOE), but this is a tendency, not a rule: qualifiers are \
sometimes dropped entirely (Hi-Viz Lime->LE). Use the worked examples for the \
same product family as your strongest signal.

Return every colour exactly once. Never invent a code that is not in the \
supplied list. If a pairing is genuinely uncertain, still give your best \
assignment and lower its confidence."""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "colour": {"type": "string"},
                    "code": {"type": "string"},
                    "confidence": {"type": "number"},
                    "reasoning": {"type": "string"},
                },
                "required": ["colour", "code", "confidence", "reasoning"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["assignments"],
    "additionalProperties": False,
}


@dataclass
class LLMResult:
    assignments: dict[str, str] = field(default_factory=dict)
    confidence: dict[str, float] = field(default_factory=dict)
    reasoning: dict[str, str] = field(default_factory=dict)
    model: str = ""
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.assignments)

    @property
    def cost_usd(self) -> float:
        """gpt-4o-mini list pricing. Approximate and only used for a running
        total in the UI — not for billing."""
        return round(self.prompt_tokens / 1e6 * 0.15
                     + self.completion_tokens / 1e6 * 0.60, 6)


def available() -> bool:
    return settings().llm_enabled


def infer(colours: list[str], codes: list[str], *, scope: str = "",
          examples: list[tuple[str, str]] | None = None) -> LLMResult:
    """Propose a colour->code assignment. Never raises.

    `colours` and `codes` are the UNRESOLVED remainder — anything the map or
    the heuristic already settled is excluded by the caller, along with the
    codes it consumed. That shrinks the problem, and on a row where one colour
    is unknown it collapses to a single possible answer.

    `examples` are confirmed (colour, code) pairs from the same scope. They are
    the highest-value part of the prompt: they teach the family's convention
    without anyone having to write it down.
    """
    if not available():
        return LLMResult(error="OPENAI_API_KEY is not set")
    if not colours or not codes:
        return LLMResult(error="nothing to infer")

    config = settings()
    payload = {
        "product_family": scope or "unknown",
        "colours": colours,
        "available_codes": codes,
        "worked_examples": [{"colour": c, "code": k} for c, k in (examples or [])],
    }

    started = time.monotonic()
    last_error = "unknown"

    for attempt in range(1, TRANSPORT_RETRIES + 2):
        try:
            from openai import OpenAI

            client = OpenAI(api_key=config.openai_api_key, timeout=TIMEOUT_SECONDS)
            response = client.chat.completions.create(
                model=config.openai_model,
                temperature=0,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(payload, indent=2)},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "colour_assignment",
                                    "schema": RESPONSE_SCHEMA, "strict": True},
                },
            )
            body = json.loads(response.choices[0].message.content)
            usage = response.usage

            result = LLMResult(
                model=config.openai_model,
                latency_ms=int((time.monotonic() - started) * 1000),
                prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            )
            for item in body.get("assignments", []):
                colour, code = item.get("colour"), item.get("code")
                # Drop anything outside the supplied sets rather than passing a
                # hallucinated code downstream to fail a confusing check later.
                if colour in colours and code in codes:
                    result.assignments[colour] = code
                    result.confidence[colour] = float(item.get("confidence", 0))
                    result.reasoning[colour] = str(item.get("reasoning", ""))[:500]
                else:
                    log.warning("model returned an out-of-set pair %r->%r", colour, code)

            log.info("tier 3 resolved %d/%d colour(s) in %dms",
                     len(result.assignments), len(colours), result.latency_ms)
            return result

        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            log.warning("openai attempt %d failed: %s", attempt, last_error)
            if attempt <= TRANSPORT_RETRIES:
                time.sleep(0.5 * attempt)

    return LLMResult(error=last_error,
                     latency_ms=int((time.monotonic() - started) * 1000))
