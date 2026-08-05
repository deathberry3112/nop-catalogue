#!/usr/bin/env python3
"""The error taxonomy.

These tests are about one property above all: a deterministic failure must
never be marked retryable. Retrying an unresolvable colour five times spends
five minutes reaching the identical answer, and on a large batch that is the
difference between a run that finishes and one that times out.
"""

from __future__ import annotations

import pytest

import combination_skus
import library
import main
from app.errors import ErrorClass, RETRY_POLICY, classify


@pytest.mark.parametrize("message, expected", [
    ("Element is not attached to the DOM", ErrorClass.TRANSIENT_DOM),
    ("element handle is stale", ErrorClass.TRANSIENT_DOM),
    ("Timeout 15000ms exceeded", ErrorClass.TRANSIENT_NET),
    ("net::ERR_CONNECTION_RESET", ErrorClass.TRANSIENT_NET),
    ("502 Bad Gateway", ErrorClass.TRANSIENT_NET),
    ("session has expired", ErrorClass.AUTH),
    ("waiting for selector '#Sku'", ErrorClass.SELECTOR_MISS),
    ("no candidate became visible", ErrorClass.SELECTOR_MISS),
    ("validation summary: Name is required", ErrorClass.NOP_REJECTED),
    ("something nobody has seen before", ErrorClass.UNKNOWN),
])
def test_messages_are_classified(message, expected):
    assert classify(message).error_class is expected


def test_exception_type_beats_wording():
    """A CombinationError is a colour problem whatever it says — trusting the
    type is more durable than matching a message that will be reworded."""
    exc = combination_skus.CombinationError("wibble wobble")
    assert classify(exc).error_class is ErrorClass.DATA_COLOUR


def test_config_error_is_data_invalid():
    assert classify(main.ConfigError("bad rows")).error_class is ErrorClass.DATA_INVALID


def test_a_step_error_keeps_its_step_name_in_the_message():
    exc = library.StepError("the grid detached", step="combinations")
    classified = classify(exc)
    assert "combinations" in classified.message


# --- the property that actually matters -------------------------------------

DETERMINISTIC = [
    ErrorClass.SELECTOR_MISS, ErrorClass.DATA_COLOUR, ErrorClass.DATA_INVALID,
    ErrorClass.NOP_REJECTED, ErrorClass.CONFLICT, ErrorClass.UNKNOWN,
]


@pytest.mark.parametrize("error_class", DETERMINISTIC)
def test_deterministic_failures_are_never_retried(error_class):
    assert error_class not in RETRY_POLICY


@pytest.mark.parametrize("error_class", [
    ErrorClass.TRANSIENT_DOM, ErrorClass.TRANSIENT_NET, ErrorClass.AUTH])
def test_transient_failures_are_retried(error_class):
    assert RETRY_POLICY[error_class] >= 1


def test_an_unresolvable_colour_is_not_retryable():
    exc = combination_skus.CombinationError("no code among ['ZZ'] looks like 'Ocean'")
    assert classify(exc).retryable is False


def test_a_detached_grid_is_retryable_several_times():
    classified = classify("Element is not attached to the DOM")
    assert classified.retryable
    assert classified.max_attempts >= 3


def test_every_class_has_a_blame():
    for error_class in ErrorClass:
        from app.errors import Classified
        assert Classified(error_class, "x").blame
