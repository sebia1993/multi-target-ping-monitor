from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.finalize_cyclonedx_sbom import deterministic_serial_number, validate_serial_number


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(
    zip_path: Path,
    checksum_path: Path,
    sbom_path: Path,
    manifest_path: Path,
    expected_version: str,
    expected_source_commit: str,
) -> dict[str, object]:
    if not all(path.is_file() for path in (zip_path, checksum_path, sbom_path, manifest_path)):
        raise RuntimeError("one or more release assets are missing")
    if len(expected_source_commit) != 40 or any(
        character not in "0123456789abcdef" for character in expected_source_commit.lower()
    ):
        raise RuntimeError("expected source commit must be a full 40-character SHA")

    checksum_parts = checksum_path.read_text(encoding="ascii").strip().split()
    if len(checksum_parts) != 2 or checksum_parts[1] != zip_path.name:
        raise RuntimeError("checksum sidecar does not identify the ZIP exactly")
    actual_hash = sha256(zip_path)
    if checksum_parts[0].lower() != actual_hash:
        raise RuntimeError("ZIP SHA-256 does not match the sidecar")

    with zipfile.ZipFile(zip_path) as archive:
        bad_member = archive.testzip()
        if bad_member is not None:
            raise RuntimeError("ZIP CRC validation failed")
        members = archive.namelist()
        required_suffixes = (
            "MultiPingCheck.exe",
            "README-실행안내.txt",
            "multipingcheck_build_info.json",
        )
        for suffix in required_suffixes:
            if not any(member.replace("\\", "/").endswith(suffix) for member in members):
                raise RuntimeError(f"ZIP is missing required content: {suffix}")
        build_info_name = next(
            member for member in members if member.replace("\\", "/").endswith("multipingcheck_build_info.json")
        )
        build_info = json.loads(archive.read(build_info_name).decode("utf-8"))
        if build_info.get("program_version") != expected_version:
            raise RuntimeError("packaged version does not match the expected release version")
        if build_info.get("git_commit") != expected_source_commit:
            raise RuntimeError("packaged source commit does not match the expected release commit")

    sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
    components = list(sbom.get("components") or [])
    component_names = {str(component.get("name", "")).lower() for component in components}
    if sbom.get("bomFormat") != "CycloneDX" or sbom.get("specVersion") != "1.6":
        raise RuntimeError("SBOM is not validated CycloneDX 1.6 JSON")
    serial_number = validate_serial_number(sbom.get("serialNumber"))
    expected_serial_number = deterministic_serial_number(
        sbom,
        f"v{expected_version}@{expected_source_commit}",
    )
    if serial_number != expected_serial_number:
        raise RuntimeError("SBOM serialNumber does not match the deterministic release identity")
    if not {"pyside6", "openpyxl"}.issubset(component_names):
        raise RuntimeError("SBOM is missing runtime components")

    sbom_hash = sha256(sbom_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if manifest.get("schema_version") != 1:
        raise RuntimeError("release manifest schema is missing or unsupported")
    if manifest.get("application_version") != expected_version or manifest.get("tag") != f"v{expected_version}":
        raise RuntimeError("release manifest version/tag does not match")
    if manifest.get("source_commit") != expected_source_commit:
        raise RuntimeError("release manifest source commit does not match")
    expected_zip_manifest = {
        "name": zip_path.name,
        "bytes": zip_path.stat().st_size,
        "sha256": actual_hash,
    }
    if manifest.get("zip") != expected_zip_manifest:
        raise RuntimeError("release manifest ZIP metadata does not match")
    manifest_sbom = manifest.get("sbom") or {}
    if (
        manifest_sbom.get("name") != sbom_path.name
        or manifest_sbom.get("bytes") != sbom_path.stat().st_size
        or manifest_sbom.get("sha256") != sbom_hash
        or manifest_sbom.get("format") != "CycloneDX 1.6 JSON"
    ):
        raise RuntimeError("release manifest SBOM metadata does not match")

    return {
        "zip": zip_path.name,
        "zip_bytes": zip_path.stat().st_size,
        "zip_sha256": actual_hash,
        "zip_members": len(members),
        "version": expected_version,
        "source_commit": expected_source_commit,
        "sbom_components": len(components),
        "sbom_sha256": sbom_hash,
        "manifest_sha256": sha256(manifest_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify MultiPingCheck release assets fail-closed.")
    parser.add_argument("--zip", type=Path, required=True)
    parser.add_argument("--checksum", type=Path, required=True)
    parser.add_argument("--sbom", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--expected-source-commit", required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            verify(
                args.zip,
                args.checksum,
                args.sbom,
                args.manifest,
                args.expected_version,
                args.expected_source_commit,
            ),
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
