"""Keep release publishing scoped to explicit, protected-main requests."""
import json
from pathlib import Path

from app import __version__

ROOT = Path(__file__).resolve().parents[1]


def test_explicit_release_request_matches_application_version():
    request = json.loads((ROOT / ".github/release-request.json").read_text(encoding="utf-8"))
    assert request["schema_version"] == 1
    assert request["publish"] is True
    assert request["tag"] == f"v{__version__}"
    assert "\n" not in request["title"]
    assert (ROOT / "docs/releases" / f"{request['tag']}.md").is_file()


def test_release_push_is_explicit_and_keeps_existing_safeguards():
    workflow = (ROOT / ".github/workflows/release-windows.yml").read_text(encoding="utf-8")
    push = workflow.split("  push:", 1)[1].split("  workflow_dispatch:", 1)[0]
    assert "branches: [main]" in push
    assert '".github/release-request.json"' in push
    assert "app/__init__.py" not in push
    assert '$env:GITHUB_REF -ne "refs/heads/main"' in workflow
    assert 'ref: ${{ github.sha }}' in workflow
    assert '$request.tag -ne $expectedTag' in workflow
    assert "Verify report exports from the release ZIP" in workflow
    assert workflow.index("Verify report exports from the release ZIP") < workflow.index("Attest ZIP build provenance")
    assert 'Expand-Archive -LiteralPath $env:ZIP_PATH' in workflow
    assert '"--report-smoke-dir"' in workflow
    assert "retention-days: 1" in workflow
