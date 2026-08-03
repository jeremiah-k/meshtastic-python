"""Tests for maintainable schema-driven node-table field discovery."""

import pytest

from meshtastic.mesh_interface_runtime import node_data


@pytest.mark.unit
def test_known_field_paths_include_node_and_telemetry_schema() -> None:
    fields = set(node_data.getKnownFieldPaths())

    assert "user.id" in fields
    assert "position.latitude" in fields
    assert "deviceMetrics.batteryLevel" in fields
    assert "environmentMetrics.temperature" in fields
    assert "healthMetrics.heartBpm" in fields


@pytest.mark.unit
def test_known_field_paths_include_observed_extension_fields() -> None:
    fields = set(
        node_data.getKnownFieldPaths(
            [{"num": 1, "vendorExtension": {"sampleValue": 7}}]
        )
    )

    assert "vendorExtension" in fields
    assert "vendorExtension.sampleValue" in fields
