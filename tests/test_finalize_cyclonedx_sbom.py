from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from scripts.finalize_cyclonedx_sbom import finalize, validate_serial_number


def _write_sbom(path) -> None:
    path.write_text(
        json.dumps(
            {
                "bomFormat": "CycloneDX",
                "specVersion": "1.6",
                "version": 1,
                "components": [{"type": "library", "name": "PySide6", "version": "6.10.2"}],
            }
        ),
        encoding="utf-8",
    )


def test_finalize_adds_deterministic_action_compatible_serial_number(tmp_path) -> None:
    sbom_path = tmp_path / "runtime-sbom.cdx.json"
    _write_sbom(sbom_path)

    first = finalize(sbom_path, "a" * 40)
    first_bytes = sbom_path.read_bytes()
    second = finalize(sbom_path, "a" * 40)

    payload = json.loads(sbom_path.read_text(encoding="utf-8"))
    assert first["serialNumber"] == second["serialNumber"] == payload["serialNumber"]
    assert payload["serialNumber"] == "urn:uuid:4b2d6ac0-9a31-5d77-ac15-b7945c6c93cb"
    assert validate_serial_number(payload["serialNumber"]) == payload["serialNumber"]
    parsed = uuid.UUID(payload["serialNumber"].removeprefix("urn:uuid:"))
    assert parsed.version == 5
    assert parsed.variant == uuid.RFC_4122
    assert sbom_path.read_bytes() == first_bytes
    assert payload["bomFormat"] == "CycloneDX"
    assert payload["specVersion"] == "1.6"


def test_finalize_uses_source_identity_in_serial_number(tmp_path) -> None:
    sbom_path = tmp_path / "runtime-sbom.cdx.json"
    _write_sbom(sbom_path)
    first = finalize(sbom_path, "a" * 40)["serialNumber"]
    second = finalize(sbom_path, "b" * 40)["serialNumber"]

    assert first != second


def test_finalize_uses_sbom_content_in_serial_number(tmp_path) -> None:
    sbom_path = tmp_path / "runtime-sbom.cdx.json"
    _write_sbom(sbom_path)
    first = finalize(sbom_path, "a" * 40)["serialNumber"]
    payload = json.loads(sbom_path.read_text(encoding="utf-8"))
    payload["components"].append({"type": "library", "name": "openpyxl", "version": "3.1.5"})
    sbom_path.write_text(json.dumps(payload), encoding="utf-8")
    second = finalize(sbom_path, "a" * 40)["serialNumber"]

    assert first != second


def test_finalize_rejects_non_cyclonedx_payload(tmp_path) -> None:
    sbom_path = tmp_path / "runtime-sbom.cdx.json"
    sbom_path.write_text(json.dumps({"spdxVersion": "SPDX-2.3"}), encoding="utf-8")

    with pytest.raises(RuntimeError, match="CycloneDX 1.6"):
        finalize(sbom_path, "a" * 40)


def test_finalize_preserves_original_and_removes_temp_file_when_replace_fails(tmp_path, monkeypatch) -> None:
    sbom_path = tmp_path / "runtime-sbom.cdx.json"
    _write_sbom(sbom_path)
    original = sbom_path.read_bytes()

    def fail_replace(_source: Path, _target: Path) -> Path:
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="synthetic replace failure"):
        finalize(sbom_path, "a" * 40)

    assert sbom_path.read_bytes() == original
    assert list(tmp_path.glob(".runtime-sbom.cdx.json.*.tmp")) == []


@pytest.mark.parametrize(
    "serial_number",
    [
        None,
        "12345678-1234-4234-8234-123456789abc",
        "urn:uuid:NOT-A-UUID",
        "urn:uuid:12345678-1234-4234-8234-123456789ABC",
    ],
)
def test_validate_serial_number_rejects_noncanonical_values(serial_number) -> None:
    with pytest.raises(RuntimeError, match="serialNumber"):
        validate_serial_number(serial_number)
