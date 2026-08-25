from __future__ import annotations

import argparse
import hashlib
import json
import re
import tempfile
import uuid
from pathlib import Path

CYCLONEDX_SERIAL_PATTERN = re.compile(r"^urn:uuid:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def validate_serial_number(value: object) -> str:
    serial_number = str(value or "")
    if not CYCLONEDX_SERIAL_PATTERN.fullmatch(serial_number):
        raise RuntimeError("CycloneDX serialNumber must be a lowercase RFC 4122 urn:uuid")
    try:
        parsed = uuid.UUID(serial_number.removeprefix("urn:uuid:"))
    except ValueError as exc:
        raise RuntimeError("CycloneDX serialNumber is not a valid UUID") from exc
    if f"urn:uuid:{parsed}" != serial_number:
        raise RuntimeError("CycloneDX serialNumber is not canonical")
    return serial_number


def deterministic_serial_number(payload: dict[str, object], identity: str) -> str:
    normalized_identity = identity.strip()
    if not normalized_identity or "\n" in normalized_identity or "\r" in normalized_identity:
        raise RuntimeError("SBOM identity must be a non-empty single-line value")
    payload_without_serial = dict(payload)
    payload_without_serial.pop("serialNumber", None)
    canonical_payload = json.dumps(
        payload_without_serial,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    content_digest = hashlib.sha256(canonical_payload).hexdigest()
    seed = f"https://github.com/sebia1993/multi-target-ping-monitor/tree/{normalized_identity}#runtime-sbom:{content_digest}"
    return f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, seed)}"


def finalize(sbom_path: Path, identity: str) -> dict[str, object]:
    payload = json.loads(sbom_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("SBOM root must be a JSON object")
    if payload.get("bomFormat") != "CycloneDX" or payload.get("specVersion") != "1.6":
        raise RuntimeError("SBOM must be CycloneDX 1.6 JSON")
    components = payload.get("components")
    if not isinstance(components, list) or not components:
        raise RuntimeError("SBOM must include runtime components")

    payload["serialNumber"] = deterministic_serial_number(payload, identity)
    validate_serial_number(payload["serialNumber"])
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=sbom_path.parent,
            prefix=f".{sbom_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(rendered)
        temp_path.replace(sbom_path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return {
        "path": str(sbom_path),
        "serialNumber": payload["serialNumber"],
        "components": len(components),
        "sha256": hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Add a deterministic action-compatible serialNumber to a reproducible CycloneDX SBOM."
    )
    parser.add_argument("--sbom", type=Path, required=True)
    parser.add_argument("--identity", required=True)
    args = parser.parse_args()
    print(json.dumps(finalize(args.sbom, args.identity), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
