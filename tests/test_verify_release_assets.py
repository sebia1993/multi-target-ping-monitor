from __future__ import annotations

import hashlib
import json
import zipfile

import pytest

from scripts.finalize_cyclonedx_sbom import finalize
from scripts.verify_release_assets import verify

SOURCE_COMMIT = "a" * 40


def _assets(tmp_path, *, version: str = "0.2.0"):
    zip_path = tmp_path / "MultiPingCheck_v0.2.0.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("MultiPingCheck.exe", b"synthetic test binary")
        archive.writestr("README-실행안내.txt", "synthetic package")
        archive.writestr(
            "_internal/multipingcheck_build_info.json",
            json.dumps({"program_version": version, "git_commit": SOURCE_COMMIT}),
        )
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    checksum_path = tmp_path / f"{zip_path.name}.sha256"
    checksum_path.write_text(f"{digest}  {zip_path.name}\n", encoding="ascii")
    sbom_path = tmp_path / "MultiPingCheck_v0.2.0_sbom.cdx.json"
    sbom_path.write_text(
        json.dumps(
            {
                "bomFormat": "CycloneDX",
                "specVersion": "1.6",
                "components": [{"name": "PySide6"}, {"name": "openpyxl"}],
            }
        ),
        encoding="utf-8",
    )
    finalize(sbom_path, f"v{version}@{SOURCE_COMMIT}")
    sbom_digest = hashlib.sha256(sbom_path.read_bytes()).hexdigest()
    manifest_path = tmp_path / "MultiPingCheck_v0.2.0_release-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "application_version": "0.2.0",
                "tag": "v0.2.0",
                "source_commit": SOURCE_COMMIT,
                "zip": {
                    "name": zip_path.name,
                    "bytes": zip_path.stat().st_size,
                    "sha256": digest,
                },
                "sbom": {
                    "name": sbom_path.name,
                    "bytes": sbom_path.stat().st_size,
                    "sha256": sbom_digest,
                    "format": "CycloneDX 1.6 JSON",
                },
            }
        ),
        encoding="utf-8",
    )
    return zip_path, checksum_path, sbom_path, manifest_path


def test_release_assets_verify_digest_crc_contents_version_and_sbom(tmp_path) -> None:
    zip_path, checksum_path, sbom_path, manifest_path = _assets(tmp_path)

    summary = verify(zip_path, checksum_path, sbom_path, manifest_path, "0.2.0", SOURCE_COMMIT)

    assert summary["version"] == "0.2.0"
    assert summary["zip_members"] == 3
    assert summary["sbom_components"] == 2
    assert summary["source_commit"] == SOURCE_COMMIT


def test_release_assets_fail_closed_on_wrong_version(tmp_path) -> None:
    zip_path, checksum_path, sbom_path, manifest_path = _assets(tmp_path, version="0.1.0")

    with pytest.raises(RuntimeError, match="packaged version"):
        verify(zip_path, checksum_path, sbom_path, manifest_path, "0.2.0", SOURCE_COMMIT)


def test_release_assets_fail_closed_on_checksum_mismatch(tmp_path) -> None:
    zip_path, checksum_path, sbom_path, manifest_path = _assets(tmp_path)
    checksum_path.write_text(f"{'0' * 64}  {zip_path.name}\n", encoding="ascii")

    with pytest.raises(RuntimeError, match="SHA-256"):
        verify(zip_path, checksum_path, sbom_path, manifest_path, "0.2.0", SOURCE_COMMIT)


def test_release_assets_fail_closed_on_manifest_mismatch(tmp_path) -> None:
    zip_path, checksum_path, sbom_path, manifest_path = _assets(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_commit"] = "b" * 40
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimeError, match="manifest source commit"):
        verify(zip_path, checksum_path, sbom_path, manifest_path, "0.2.0", SOURCE_COMMIT)


def test_release_assets_fail_closed_when_sbom_serial_number_is_missing(tmp_path) -> None:
    zip_path, checksum_path, sbom_path, manifest_path = _assets(tmp_path)
    sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
    sbom.pop("serialNumber")
    sbom_path.write_text(json.dumps(sbom), encoding="utf-8")

    with pytest.raises(RuntimeError, match="serialNumber"):
        verify(zip_path, checksum_path, sbom_path, manifest_path, "0.2.0", SOURCE_COMMIT)


def test_release_assets_fail_closed_when_sbom_serial_number_has_other_identity(tmp_path) -> None:
    zip_path, checksum_path, sbom_path, manifest_path = _assets(tmp_path)
    finalize(sbom_path, f"v0.2.0@{'b' * 40}")

    with pytest.raises(RuntimeError, match="deterministic release identity"):
        verify(zip_path, checksum_path, sbom_path, manifest_path, "0.2.0", SOURCE_COMMIT)
