"""The error taxonomy — what kind of failure this is, and what to do about it.

Every failure in the system is classified into one ErrorClass. That single fact
is what makes three other things possible:

    retry policy     transient classes are retried; deterministic ones never are
    triage           the UI groups 40 failures by cause, not by row
    metrics          "failure rate" is meaningless; "failure rate by class" acts

Before this existed a failure was a string in a list, so all three were manual.

THE RULE THAT MATTERS: never retry a deterministic failure. Retrying an
unresolvable colour five times spends five minutes arriving at the same answer.
`retryable` below is the whole policy, and it is deliberately data, not code
scattered across call sites.

Classification is best-effort and ordered most-specific-first: the patterns are
matched against the exception's text, and anything unrecognised becomes
UNKNOWN. A rising UNKNOWN count is itself the signal that this table is stale —
it is a metric, not a shrug.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class ErrorClass(str, Enum):
    # --- transient: the world, not the data. Retry these. ----------------
    TRANSIENT_DOM = "transient_dom"      # Kendo rebound the grid under us
    TRANSIENT_NET = "transient_net"      # timeout, 502, connection reset
    AUTH = "auth"                        # session expired mid-run

    # --- deterministic: retrying changes nothing. -------------------------
    SELECTOR_MISS = "selector_miss"      # element never appeared — nop changed
    DATA_COLOUR = "data_colour"          # colour unresolvable at every tier
    DATA_INVALID = "data_invalid"        # the row itself is wrong
    NOP_REJECTED = "nop_rejected"        # the admin refused the value
    CONFLICT = "conflict"                # another run owns this SKU

    UNKNOWN = "unknown"


#: Which classes are worth another attempt, and how many.
RETRY_POLICY: dict[ErrorClass, int] = {
    ErrorClass.TRANSIENT_DOM: 5,   # cheap, local, and genuinely common
    ErrorClass.TRANSIENT_NET: 3,
    ErrorClass.AUTH: 1,            # one re-login, then give up
}


@dataclass(frozen=True)
class Classified:
    error_class: ErrorClass
    message: str

    @property
    def retryable(self) -> bool:
        return self.error_class in RETRY_POLICY

    @property
    def max_attempts(self) -> int:
        return RETRY_POLICY.get(self.error_class, 1)

    @property
    def blame(self) -> str:
        """Who has to act. Drives which bucket the UI files this under."""
        return {
            ErrorClass.TRANSIENT_DOM: "nobody",
            ErrorClass.TRANSIENT_NET: "infrastructure",
            ErrorClass.AUTH: "nobody",
            ErrorClass.SELECTOR_MISS: "us",
            ErrorClass.DATA_COLOUR: "client",
            ErrorClass.DATA_INVALID: "client",
            ErrorClass.NOP_REJECTED: "client",
            ErrorClass.CONFLICT: "expected",
            ErrorClass.UNKNOWN: "us",
        }[self.error_class]


# Ordered: first match wins, so the specific patterns must precede the general.
_PATTERNS: list[tuple[ErrorClass, re.Pattern]] = [
    (ErrorClass.TRANSIENT_DOM, re.compile(
        r"element is not attached|detached from|element handle|stale|"
        r"rebound|not stable|element is outside of the viewport", re.I)),
    (ErrorClass.AUTH, re.compile(
        r"login|sign in|unauthor|session (has )?expired|403|401", re.I)),
    (ErrorClass.TRANSIENT_NET, re.compile(
        r"timeout|timed out|econnreset|net::err|502|503|504|"
        r"connection (refused|reset|closed)", re.I)),
    (ErrorClass.DATA_COLOUR, re.compile(
        r"combination|colour|color code|no code among|ambiguous", re.I)),
    (ErrorClass.NOP_REJECTED, re.compile(
        r"validation (summary|error)|already exists|is required|"
        r"the admin refused", re.I)),
    (ErrorClass.SELECTOR_MISS, re.compile(
        r"no candidate|never appeared|waiting for (selector|locator)|"
        r"resolved to 0 elements|strict mode violation", re.I)),
    (ErrorClass.CONFLICT, re.compile(r"claimed by|conflict|duplicate key", re.I)),
    (ErrorClass.DATA_INVALID, re.compile(
        r"missing an 'sku'|is not a list|has no 'rows'|invalid row", re.I)),
]


def classify(exc: BaseException | str) -> Classified:
    """Best-effort classification of a failure.

    Exception *type* is checked before text where the type is unambiguous —
    CombinationError means a colour problem regardless of its wording, and
    trusting the type is more durable than matching a message that will be
    reworded eventually.
    """
    message = str(exc).strip() or exc.__class__.__name__

    if not isinstance(exc, str):
        name = type(exc).__name__
        if name == "CombinationError":
            return Classified(ErrorClass.DATA_COLOUR, message)
        if name == "ConfigError":
            return Classified(ErrorClass.DATA_INVALID, message)
        if name in {"TimeoutError", "PlaywrightTimeoutError"}:
            return Classified(ErrorClass.TRANSIENT_NET, message)

    # library.StepError renders as "step: message"; classify on the whole
    # string so the step name contributes (e.g. "combinations: ...").
    for error_class, pattern in _PATTERNS:
        if pattern.search(message):
            return Classified(error_class, message)
    return Classified(ErrorClass.UNKNOWN, message)
