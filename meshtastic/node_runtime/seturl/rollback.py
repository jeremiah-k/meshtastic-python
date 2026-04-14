"""Rollback engine (RETIRED).

The rollback mechanism has been replaced with fail-fast semantics:
on first write failure, execution stops immediately, local caches are
invalidated conservatively, and the caller is instructed to reconnect
and reload device state.

Rollback was removed because it attempted compensating writes over a
degraded transport, making field recovery worse rather than better.
See coordinator.py for the current fail-fast implementation.
"""

# This module is intentionally empty.  All rollback logic has been retired.
# The _SetUrlRollbackEngine symbol is no longer exported from __init__.py.
