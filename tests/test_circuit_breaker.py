"""Tests for circuit_breaker.py — CLOSED/HALF_OPEN/OPEN state machine."""

import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from src.core.circuit_breaker import CircuitBreaker, hash_error


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def cb(tmp_dir):
    return CircuitBreaker(tmp_dir, config={
        "no_progress_threshold": 3,
        "same_error_threshold": 3,
        "output_decline_pct": 0.7,
        "cooldown_seconds": 5,  # short for testing
        "max_open_count": 2,
    })


class TestInitialState:
    def test_starts_closed(self, cb):
        assert cb.state == CircuitBreaker.CLOSED

    def test_not_permanently_halted(self, cb):
        assert not cb.is_permanently_halted

    def test_zero_opens(self, cb):
        assert cb.total_opens == 0

    def test_empty_history(self, cb):
        assert cb.get_history() == []


class TestClosedToHalfOpen:
    def test_two_no_progress_triggers_half_open(self, cb):
        cb.record_iteration(progress=False, iteration=1)
        assert cb.state == CircuitBreaker.CLOSED
        cb.record_iteration(progress=False, iteration=2)
        assert cb.state == CircuitBreaker.HALF_OPEN

    def test_progress_resets_counter(self, cb):
        cb.record_iteration(progress=False, iteration=1)
        cb.record_iteration(progress=True, iteration=2)
        cb.record_iteration(progress=False, iteration=3)
        # Only 1 consecutive no-progress, not 2
        assert cb.state == CircuitBreaker.CLOSED


class TestClosedToOpen:
    def test_three_no_progress_triggers_open(self, cb):
        for i in range(3):
            cb.record_iteration(progress=False, iteration=i + 1)
        assert cb.state == CircuitBreaker.OPEN
        assert cb.total_opens == 1

    def test_same_error_triggers_open(self, cb):
        err = hash_error("TypeError: cannot read property of undefined")
        for i in range(3):
            cb.record_iteration(progress=False, error_hash=err, iteration=i + 1)
        assert cb.state == CircuitBreaker.OPEN

    def test_different_errors_dont_trigger(self, cb):
        cb.record_iteration(progress=False, error_hash="aaa", iteration=1)
        cb.record_iteration(progress=False, error_hash="bbb", iteration=2)
        # Only 1 consecutive same error for "bbb", resets on switch
        assert cb.state == CircuitBreaker.HALF_OPEN  # 2 no-progress
        cb.record_iteration(progress=False, error_hash="ccc", iteration=3)
        # 3 no-progress but only 1 same error — opens on no_progress
        assert cb.state == CircuitBreaker.OPEN


class TestHalfOpenRecovery:
    def test_progress_in_half_open_returns_to_closed(self, cb):
        # Get to HALF_OPEN
        cb.record_iteration(progress=False, iteration=1)
        cb.record_iteration(progress=False, iteration=2)
        assert cb.state == CircuitBreaker.HALF_OPEN
        # Progress recovers
        cb.record_iteration(progress=True, iteration=3)
        assert cb.state == CircuitBreaker.CLOSED

    def test_no_progress_in_half_open_opens(self, cb):
        # Get to HALF_OPEN
        cb.record_iteration(progress=False, iteration=1)
        cb.record_iteration(progress=False, iteration=2)
        assert cb.state == CircuitBreaker.HALF_OPEN
        # No progress in HALF_OPEN triggers OPEN
        cb.record_iteration(progress=False, iteration=3)
        assert cb.state == CircuitBreaker.OPEN


class TestOpenState:
    def test_stays_open_on_further_no_progress(self, cb):
        for i in range(3):
            cb.record_iteration(progress=False, iteration=i + 1)
        assert cb.state == CircuitBreaker.OPEN
        cb.record_iteration(progress=False, iteration=4)
        assert cb.state == CircuitBreaker.OPEN

    def test_cooldown_transitions_to_half_open(self, cb):
        for i in range(3):
            cb.record_iteration(progress=False, iteration=i + 1)
        assert cb.state == CircuitBreaker.OPEN

        # Simulate cooldown elapsed
        with patch("time.time", return_value=time.time() + 10):
            result = cb.check_cooldown()
        assert result is True
        assert cb.state == CircuitBreaker.HALF_OPEN

    def test_cooldown_not_elapsed(self, cb):
        for i in range(3):
            cb.record_iteration(progress=False, iteration=i + 1)
        assert cb.state == CircuitBreaker.OPEN
        result = cb.check_cooldown()
        assert result is False
        assert cb.state == CircuitBreaker.OPEN


class TestPermanentHalt:
    def test_two_opens_is_permanent(self, cb):
        # First OPEN
        for i in range(3):
            cb.record_iteration(progress=False, iteration=i + 1)
        assert cb.state == CircuitBreaker.OPEN
        assert cb.total_opens == 1

        # Cooldown → HALF_OPEN → recover
        with patch("time.time", return_value=time.time() + 10):
            cb.check_cooldown()
        cb.record_iteration(progress=True, iteration=4)
        assert cb.state == CircuitBreaker.CLOSED

        # Second OPEN
        for i in range(3):
            cb.record_iteration(progress=False, iteration=5 + i)
        assert cb.state == CircuitBreaker.OPEN
        assert cb.total_opens == 2
        assert cb.is_permanently_halted

    def test_no_cooldown_when_permanently_halted(self, cb):
        # Get to permanent halt
        for i in range(3):
            cb.record_iteration(progress=False, iteration=i + 1)
        with patch("time.time", return_value=time.time() + 10):
            cb.check_cooldown()
        cb.record_iteration(progress=True, iteration=4)
        for i in range(3):
            cb.record_iteration(progress=False, iteration=5 + i)
        assert cb.is_permanently_halted

        # Cooldown should not transition
        with patch("time.time", return_value=time.time() + 100):
            result = cb.check_cooldown()
        assert result is False


class TestReset:
    def test_manual_reset(self, cb):
        for i in range(3):
            cb.record_iteration(progress=False, iteration=i + 1)
        assert cb.state == CircuitBreaker.OPEN
        cb.reset()
        assert cb.state == CircuitBreaker.CLOSED


class TestHistory:
    def test_records_transitions(self, cb):
        cb.record_iteration(progress=False, iteration=1)
        cb.record_iteration(progress=False, iteration=2)
        # CLOSED → HALF_OPEN
        cb.record_iteration(progress=True, iteration=3)
        # HALF_OPEN → CLOSED
        history = cb.get_history()
        assert len(history) == 2
        assert history[0]["from"] == "CLOSED"
        assert history[0]["to"] == "HALF_OPEN"
        assert history[1]["from"] == "HALF_OPEN"
        assert history[1]["to"] == "CLOSED"

    def test_history_capped_at_20(self, cb):
        # Force many transitions
        for cycle in range(15):
            cb.record_iteration(progress=False, iteration=cycle * 3 + 1)
            cb.record_iteration(progress=False, iteration=cycle * 3 + 2)
            # HALF_OPEN
            cb.record_iteration(progress=True, iteration=cycle * 3 + 3)
            # Back to CLOSED
        assert len(cb.get_history()) <= 20


class TestHashError:
    def test_same_error_same_hash(self):
        h1 = hash_error("TypeError: cannot read property 'x' of undefined")
        h2 = hash_error("TypeError: cannot read property 'x' of undefined")
        assert h1 == h2

    def test_different_errors_different_hash(self):
        h1 = hash_error("TypeError: foo")
        h2 = hash_error("ReferenceError: bar")
        assert h1 != h2

    def test_normalizes_line_numbers(self):
        h1 = hash_error("Error at line 42")
        h2 = hash_error("Error at line 99")
        assert h1 == h2

    def test_normalizes_timestamps(self):
        h1 = hash_error("Error at 2026-03-27 14:30:00")
        h2 = hash_error("Error at 2026-03-28 09:15:00")
        assert h1 == h2
