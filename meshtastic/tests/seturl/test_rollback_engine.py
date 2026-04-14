"""Tests for _SetUrlRollbackEngine (RETIRED).

The rollback engine has been retired in favor of fail-fast semantics.
See coordinator.py and the corresponding test_coordinator.py for the
current fail-fast behavior.
"""

import pytest

# The rollback module is intentionally empty.  These tests are retired
# alongside the rollback engine.  A single passing test preserves the
# file in the test discovery list without importing the retired module.


def test_rollback_retired() -> None:
    """Rollback engine has been retired; this test is a placeholder."""
    assert True
