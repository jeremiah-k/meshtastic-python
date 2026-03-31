"""Test ResponseHandler backward compatibility for tuple unpacking.

This file documents the backward compatibility implications of adding the
isAckNakHandler field to the ResponseHandler NamedTuple.

IMPORTANT: The addition of isAckNakHandler field BREAKS backward compatibility
for code doing tuple unpacking like:
    callback, ack_permitted = handler

Such code will now raise:
    ValueError: too many values to unpack (expected 2)

This test serves as documentation of the breaking change and demonstrates
the correct patterns for both old and new code.
"""

import pytest
from meshtastic import ResponseHandler


def test_response_handler_tuple_unpacking_breaking_change() -> None:
    """Document that ResponseHandler now has 3 fields, breaking 2-tuple unpacking.

    Historical code may have done:
        callback, ack_permitted = handler

    This will now FAIL with ValueError. Code must be updated to either:
        1. Use 3-element unpacking: callback, ack_permitted, is_ack_nak = handler
        2. Use named access: handler.callback, handler.ackPermitted
    """

    def dummy_callback(_response: dict) -> None:
        pass

    # Create handler with default isAckNakHandler=False
    handler = ResponseHandler(callback=dummy_callback, ackPermitted=True)

    # Demonstrate that 2-element unpacking FAILS (documenting breaking change)
    with pytest.raises(ValueError, match="too many values to unpack"):
        callback, ack_permitted = handler  # type: ignore # This is expected to fail

    # Show the correct 3-element unpacking pattern for new code
    callback, ack_permitted, is_ack_nak = handler
    assert callback is dummy_callback
    assert ack_permitted is True
    assert is_ack_nak is False


def test_response_handler_named_access() -> None:
    """Verify ResponseHandler fields can be accessed by name (recommended pattern)."""

    def dummy_callback(_response: dict) -> None:
        pass

    handler = ResponseHandler(
        callback=dummy_callback, ackPermitted=False, isAckNakHandler=True
    )

    # Named access is the safest pattern and works regardless of field additions
    assert handler.callback is dummy_callback
    assert handler.ackPermitted is False
    assert handler.isAckNakHandler is True


def test_response_handler_iteration_yields_three_values() -> None:
    """Verify that iterating over ResponseHandler yields all 3 field values."""

    def dummy_callback(_response: dict) -> None:
        pass

    handler = ResponseHandler(
        callback=dummy_callback, ackPermitted=True, isAckNakHandler=False
    )

    # Iterating yields all 3 values
    values = list(handler)
    assert len(values) == 3
    assert values[0] is dummy_callback
    assert values[1] is True
    assert values[2] is False


def test_response_handler_length_is_three() -> None:
    """Verify len() returns 3 (all fields including defaults)."""

    def dummy_callback(_response: dict) -> None:
        pass

    handler = ResponseHandler(callback=dummy_callback, ackPermitted=True)
    assert len(handler) == 3
