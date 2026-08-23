from __future__ import annotations

import json

from scripts import scan_secrets


def test_secret_scan_reports_rule_count_without_value_or_path(tmp_path, monkeypatch, capsys) -> None:
    secret = "github_pat_" + "A" * 50
    path = tmp_path / "internal-device-name.txt"
    path.write_text(secret, encoding="utf-8")
    monkeypatch.setattr(scan_secrets, "tracked_paths", lambda: [path])

    assert scan_secrets.main() == 1

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["finding_count"] == 1
    assert payload["rules"] == {"github-token": 1}
    assert secret not in output
    assert path.name not in output


def test_secret_scan_ignores_binary_and_accepts_clean_text(tmp_path) -> None:
    binary = tmp_path / "image.bin"
    binary.write_bytes(b"\x00github_pat_" + b"A" * 50)
    clean = tmp_path / "README.md"
    clean.write_text("Use a placeholder token in local configuration.", encoding="utf-8")

    assert scan_secrets.scan_paths([binary, clean]) == {}
