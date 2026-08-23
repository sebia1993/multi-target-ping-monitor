from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RULES = {
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "github-token": re.compile(r"(?:github_pat_[A-Za-z0-9_]{40,}|gh[opsu]_[A-Za-z0-9]{30,})"),
    "aws-access-key": re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}"),
    "slack-token": re.compile(r"xox[baprs]-[A-Za-z0-9-]{20,}"),
}
MAX_TEXT_BYTES = 2 * 1024 * 1024


def tracked_paths(root: Path = ROOT) -> list[Path]:
    completed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        capture_output=True,
        check=True,
    )
    return [root / item.decode("utf-8") for item in completed.stdout.split(b"\0") if item]


def scan_paths(paths: list[Path]) -> Counter[str]:
    findings: Counter[str] = Counter()
    for path in paths:
        if not path.exists():
            # A tracked path can be absent locally while its deletion is pending.
            continue
        try:
            payload = path.read_bytes()
        except OSError:
            findings["unreadable-file"] += 1
            continue
        if len(payload) > MAX_TEXT_BYTES or b"\0" in payload:
            continue
        text = payload.decode("utf-8", errors="replace")
        for rule_name, pattern in RULES.items():
            match_count = len(pattern.findall(text))
            if match_count:
                findings[rule_name] += match_count
    return findings


def main() -> int:
    paths = tracked_paths()
    findings = scan_paths(paths)
    # Values and file names are deliberately omitted so a failed CI log cannot
    # repeat a credential or reveal an internal naming clue.
    print(
        json.dumps(
            {
                "scanned_files": len(paths),
                "finding_count": sum(findings.values()),
                "rules": dict(sorted(findings.items())),
            },
            sort_keys=True,
        )
    )
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
