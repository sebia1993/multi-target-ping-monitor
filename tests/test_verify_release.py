from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import tomllib
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

import scripts.verify_release as verify_release
from app.core.models import STATUS_ERROR, STATUS_OK, HopInfo, MetricSnapshot, PingResult


def test_custom_target_smoke_runs_read_only_ping_and_trace(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(verify_release, "validate_target", lambda target: (True, ""))

    class FakePingRunner:
        def __init__(self, timeout_ms: int) -> None:
            self.timeout_ms = timeout_ms

        def ping(self, target: str) -> PingResult:
            calls.append(("ping", target))
            return PingResult(target, True, 1.0, STATUS_OK, datetime.now())

    monkeypatch.setattr(verify_release, "CommandPingRunner", FakePingRunner)
    monkeypatch.setattr(
        verify_release,
        "run_traceroute",
        lambda target, max_hops, timeout_ms: (
            calls.append(("tracert", target)) or [HopInfo(index=1, address="192.0.2.1")]
        ),
    )

    verify_release.run_custom_target_smoke("192.0.2.1")

    assert calls == [("ping", "192.0.2.1"), ("tracert", "192.0.2.1")]


def test_custom_target_smoke_fails_without_echo_reply(monkeypatch) -> None:
    monkeypatch.setattr(verify_release, "validate_target", lambda target: (True, ""))

    class FakePingRunner:
        def __init__(self, timeout_ms: int) -> None:
            self.timeout_ms = timeout_ms

        def ping(self, target: str) -> PingResult:
            return PingResult(target, False, None, STATUS_ERROR, datetime.now())

    monkeypatch.setattr(verify_release, "CommandPingRunner", FakePingRunner)

    try:
        verify_release.run_custom_target_smoke("203.0.113.1")
    except RuntimeError as exc:
        assert "Custom target ping did not succeed" in str(exc)
    else:
        raise AssertionError("Expected custom target smoke to fail")


def test_live_worker_smoke_requires_trace_and_update(monkeypatch) -> None:
    monkeypatch.setattr(verify_release, "MeasurementWorker", _SuccessfulWorker)

    verify_release.run_live_worker_smoke("8.8.8.8")


def test_live_worker_smoke_fails_on_worker_error(monkeypatch) -> None:
    monkeypatch.setattr(verify_release, "MeasurementWorker", _ErrorWorker)

    try:
        verify_release.run_live_worker_smoke("8.8.8.8")
    except RuntimeError as exc:
        assert "Live worker smoke failed" in str(exc)
    else:
        raise AssertionError("Expected live worker smoke to fail")


def test_release_policy_accepts_windowed_non_admin_package(monkeypatch, tmp_path) -> None:
    _write_policy_tree(tmp_path)
    monkeypatch.setattr(verify_release, "ROOT", tmp_path)

    verify_release.run_release_policy_check()


def test_application_version_has_one_executable_source() -> None:
    root = Path(__file__).resolve().parents[1]
    app_init = (root / "app" / "__init__.py").read_text(encoding="utf-8")

    assert app_init.count('__version__ = "0.2.0"') == 1
    for path in [
        root / "pyproject.toml",
        root / "build_windows_exe.ps1",
        *sorted((root / "scripts").glob("*")),
        *sorted((root / ".github" / "workflows").glob("*.yml")),
    ]:
        if path.is_file():
            assert "0.2.0" not in path.read_text(encoding="utf-8-sig"), path


def test_direct_development_pins_match_input_lock_and_pyproject() -> None:
    root = Path(__file__).resolve().parents[1]
    dev_input_pins = _parse_input_pins(root / "requirements-dev.in")
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    pyproject_dev_pins = _parse_exact_pins(pyproject["project"]["optional-dependencies"]["dev"])
    dev_lock_pins = _parse_lock_pins(root / "requirements-dev.lock")

    assert dev_input_pins == pyproject_dev_pins
    assert dev_input_pins.keys() <= dev_lock_pins.keys()
    assert {name: dev_lock_pins[name] for name in dev_input_pins} == dev_input_pins
    assert {
        "cyclonedx-python-lib": "10.3.0",
        "lxml": "6.1.0",
        "pip-audit": "2.10.1",
        "pytest": "9.0.3",
    }.items() <= dev_input_pins.items()


def test_release_policy_rejects_admin_manifest(monkeypatch, tmp_path) -> None:
    _write_policy_tree(tmp_path, build_script="python -m PyInstaller --windowed --uac-admin app\\main.py")
    monkeypatch.setattr(verify_release, "ROOT", tmp_path)

    try:
        verify_release.run_release_policy_check()
    except RuntimeError as exc:
        assert "elevated privileges" in str(exc)
    else:
        raise AssertionError("Expected release policy check to fail")


def test_release_policy_rejects_external_api_clients(monkeypatch, tmp_path) -> None:
    _write_policy_tree(tmp_path, app_source="import requests\n")
    monkeypatch.setattr(verify_release, "ROOT", tmp_path)

    try:
        verify_release.run_release_policy_check()
    except RuntimeError as exc:
        assert "External API client dependency" in str(exc)
    else:
        raise AssertionError("Expected release policy check to fail")


def test_packaged_build_info_check_requires_complete_metadata(tmp_path) -> None:
    internal = tmp_path / "_internal"
    internal.mkdir()
    (internal / verify_release.BUILD_INFO_FILE_NAME).write_text(
        json.dumps(
            {
                "program_version": "0.1.0",
                "build_id": "test-build",
                "build_time": "2026-08-13T10:00:00+09:00",
                "git_commit": "abc123",
                "git_branch": "main",
                "distribution": "Windows Portable EXE",
                "config_schema_version": "2",
                "source_state": "커밋과 일치",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    verify_release.run_packaged_build_info_check(tmp_path)


def test_run_pytest_uses_release_timeout(monkeypatch) -> None:
    calls = []

    def fake_run_command(command, *, timeout, env=None):
        calls.append((command, timeout, env))

    monkeypatch.setattr(verify_release, "run_command", fake_run_command)

    verify_release.run_pytest()

    assert calls == [([verify_release.sys.executable, "-m", "pytest"], verify_release.PYTEST_TIMEOUT_SECONDS, None)]
    assert verify_release.PYTEST_TIMEOUT_SECONDS >= 600


def test_main_runs_source_checks_by_default(monkeypatch) -> None:
    calls: list[str] = []

    _stub_release_checks(monkeypatch, calls)
    monkeypatch.setattr(verify_release.sys, "argv", ["verify_release.py"])

    assert verify_release.main() == 0
    assert calls == ["pytest", "compileall", "policy", "qt", "export", "soak"]


def test_main_exe_mode_runs_packaged_smoke_without_source_checks(monkeypatch) -> None:
    calls: list[str] = []

    _stub_release_checks(monkeypatch, calls)
    monkeypatch.setattr(verify_release.sys, "argv", ["verify_release.py", "--exe"])

    assert verify_release.main() == 0
    assert calls == ["exe"]


def test_run_soak_smoke_uses_platform_script_path(monkeypatch) -> None:
    calls = []

    def fake_run_command(command, *, timeout, env=None):
        calls.append((command, timeout, env))

    monkeypatch.setattr(verify_release, "run_command", fake_run_command)

    verify_release.run_soak_smoke()

    assert calls
    assert calls[0][0][1] == str(Path("scripts") / "soak_test.py")
    assert calls[0][1] == 90


def test_field_verification_docs_match_current_graph_controls() -> None:
    text = (Path(__file__).resolve().parents[1] / "docs" / "field_verification.md").read_text(encoding="utf-8")

    assert "`그래프 확대` 버튼" not in text
    assert "`전체 보기`" not in text
    assert "`최근 보기`" not in text
    assert "시간 범위 선택" in text
    assert "이름 버튼" in text
    assert "일시중지/삭제 버튼" in text
    assert "python scripts\\run_stability_soak_suite.py --dry-run" in text
    assert "python scripts\\run_stability_soak_suite.py" in text
    assert "python scripts\\run_stability_soak_suite.py --resume --run-id <RUN_ID>" in text
    assert "python scripts\\run_stability_soak_suite.py --validate-only --run-id <RUN_ID>" in text
    assert "python scripts\\soak_test.py --profile long --duration-seconds 1800 --no-ui" in text
    assert "python scripts\\soak_test.py --profile long4h" in text
    assert "python scripts\\soak_test.py --profile long8h" in text
    assert "python scripts\\soak_test.py --profile long24h" in text
    assert "python scripts\\soak_test.py --profile ui10" in text
    assert "python scripts\\soak_test.py --profile ui20" in text
    assert "python scripts\\soak_test.py --profile ui50" in text
    assert "`max_active_threads`" in text
    assert "`max_pending_ping_count`" in text


def test_publish_release_notes_include_traceable_zip_metadata() -> None:
    text = (Path(__file__).resolve().parents[1] / "scripts" / "publish_release.ps1").read_text(encoding="utf-8-sig")

    assert "git rev-parse HEAD" in text
    assert "git rev-parse --short HEAD" not in text
    assert "- ZIP SHA256: $ZipHash" in text
    assert "- 기준 커밋 SHA: $Head" in text
    assert "- 압축 파일: $($ZipItem.Name)" in text
    assert "IPv4 주소는 한 줄에 하나씩" in text
    assert "APP_STARTUP_FAILED 또는 APP_UNEXPECTED_ERROR" in text
    assert "%LOCALAPPDATA%\\MultiPingCheck\\logs\\multipingcheck.log" in text
    assert "- 기준 커밋: $Head" in text
    assert "source_commit = $Head" in text
    assert '"--manifest", $ManifestItem.FullName' in text
    assert '"--expected-source-commit", $Head' in text


def test_publish_release_allows_detached_packaging_but_rejects_detached_upload() -> None:
    text = (Path(__file__).resolve().parents[1] / "scripts" / "publish_release.ps1").read_text(encoding="utf-8-sig")

    detached_check = '$DetachedHead = $Branch -eq "HEAD"'
    upload_gate = "if ($DetachedHead -and -not $SkipUpload) {"
    packaging_label = '$Branch = "detached"'
    assert detached_check in text
    assert upload_gate in text
    assert packaging_label in text
    assert text.index(detached_check) < text.index(upload_gate) < text.index(packaging_label)
    assert 'if ($LASTEXITCODE -ne 0 -or -not $Branch -or $Branch -eq "HEAD")' not in text
    assert 'Invoke-Checked "git" @("push", "origin", $Branch)' in text


def test_release_windows_workflow_matches_publish_contract() -> None:
    text = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release-windows.yml").read_text(
        encoding="utf-8"
    )

    assert "workflow_dispatch:" in text
    for input_name in ("tag:", "title:", "notes:"):
        assert input_name in text
    assert "contents: write" in text
    assert "id-token: write" in text
    assert "attestations: write" in text
    assert "runs-on: windows-latest" in text
    assert "fetch-depth: 0" in text
    assert '$env:GITHUB_REF -ne "refs/heads/main"' in text
    assert "github.ref_name" not in text
    assert "${{ github.ref }}" not in text
    assert "ref: ${{ github.sha }}" in text
    assert "ref: main" not in text
    assert "$expectedSha = $env:GITHUB_SHA.Trim().ToLowerInvariant()" in text
    assert "$actualSha = (git rev-parse HEAD).Trim().ToLowerInvariant()" in text
    assert "if ($actualSha -ne $expectedSha)" in text
    assert "Release source mismatch" in text
    assert "GH_TOKEN: ${{ github.token }}" in text
    assert 'version = (python -c "from app import __version__' in text
    assert 'if (-not $releaseTitle) { $releaseTitle = "MultiPingCheck $expectedTag" }' in text
    assert '"title=$releaseTitle" >> $env:GITHUB_OUTPUT' in text
    assert "zip_path=release/MultiPingCheck_$safeTag.zip" in text
    assert "checksum_path=release/MultiPingCheck_$safeTag.zip.sha256" in text
    assert "sbom_path=release/MultiPingCheck_${safeTag}_sbom.cdx.json" in text
    assert "manifest_path=release/MultiPingCheck_${safeTag}_release-manifest.json" in text
    assert ".\\scripts\\publish_release.ps1 -Tag $env:RESOLVED_TAG" in text
    assert "-SkipUpload" in text
    assert "actions/attest-build-provenance@4d101475d8b20a2381f78447822ac1eab6504dd8 # v4.2.2" in text
    assert "actions/attest-sbom@c604332985a26aa8cf1bdc465b92731239ec6b9e # v4.1.0" in text
    assert "git tag -a $env:RESOLVED_TAG" in text
    assert 'git cat-file -t "refs/tags/$env:RESOLVED_TAG"' in text
    assert '"created=false" >> $env:GITHUB_OUTPUT' in text
    assert '"created=true" >> $env:GITHUB_OUTPUT' in text
    assert "--verify-tag" in text
    assert "--latest" in text
    assert "--prerelease" not in text
    assert "--draft" not in text
    assert "release/MultiPingCheck_v0.2.0" not in text
    assert 'default: "v0.2.0"' not in text
    assert 'default: "MultiPingCheck v0.2.0"' not in text


def test_release_workflow_cleans_only_a_tag_created_by_failed_publication() -> None:
    text = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release-windows.yml").read_text(
        encoding="utf-8"
    )

    assert "if: failure() && steps.tag.outputs.created == 'true' && steps.publish.outcome == 'failure'" in text
    assert '"attempted=false" >> $env:GITHUB_OUTPUT' in text
    assert '"attempted=true" >> $env:GITHUB_OUTPUT' in text
    assert 'if ("${{ steps.publish.outputs.attempted }}" -eq "true")' in text
    assert 'git push origin ":refs/tags/$env:RESOLVED_TAG"' in text
    assert 'git ls-remote --tags origin "refs/tags/$env:RESOLVED_TAG"' in text
    assert "$cleanupFailed = $true" in text
    assert 'throw "failed to clean up newly created tag"' in text


def test_ci_workflow_consolidates_quality_and_windows_package_checks() -> None:
    root = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    ci = (root / "ci.yml").read_text(encoding="utf-8")

    assert "push:" in ci
    assert "pull_request:" in ci
    assert "Quality, tests, and security" in ci
    assert "Windows package validation" in ci
    assert "python -m pip install --require-hashes -r requirements-dev.lock" in ci
    assert "python -m ruff check app scripts tests" in ci
    assert "python -m ruff format --check" in ci
    assert "python -m ruff check --select I" in ci
    assert "python -m pytest -q" in ci
    assert "python -m pip_audit -r requirements.lock --require-hashes" in ci
    assert "python -m pip_audit -r requirements-dev.lock --require-hashes" in ci
    assert "python scripts/scan_secrets.py" in ci
    assert "python scripts\\verify_release.py" in ci
    assert "build_windows_exe.ps1" in ci
    assert "python scripts\\verify_release.py --exe" in ci
    assert "git checkout --detach $env:GITHUB_SHA" in ci
    assert ".\\scripts\\publish_release.ps1 -SkipUpload -SkipBuild" in ci
    assert "cyclonedx-py requirements requirements.lock" in ci
    assert not (root / "windows-fast-check.yml").exists()
    assert not (root / "windows-release-verify.yml").exists()

    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert 'select = ["E9", "F63", "F7", "F82"]' in pyproject


def test_all_workflow_actions_are_exact_reviewed_node24_pins() -> None:
    root = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    workflow_text = "\n".join(path.read_text(encoding="utf-8") for path in sorted(root.glob("*.yml")))
    action_refs = re.findall(r"^\s*uses:\s*([^\s#]+)", workflow_text, flags=re.MULTILINE)

    assert action_refs
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", action_ref) for action_ref in action_refs)
    allowed_pins = {
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
        "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97",
        "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
        "actions/attest-build-provenance@4d101475d8b20a2381f78447822ac1eab6504dd8",
        "actions/attest-sbom@c604332985a26aa8cf1bdc465b92731239ec6b9e",
    }
    assert set(action_refs) == allowed_pins


class _Signal:
    def __init__(self) -> None:
        self._callbacks = []

    def connect(self, callback) -> None:
        self._callbacks.append(callback)

    def emit(self, *args) -> None:
        for callback in self._callbacks:
            callback(*args)


class _SuccessfulWorker:
    def __init__(self, *args, **kwargs) -> None:
        self.trace_completed = _Signal()
        self.measurement_updated = _Signal()
        self.error_message = _Signal()

    def run(self) -> None:
        snapshot = MetricSnapshot(
            hop_index=1,
            address="8.8.8.8",
            hostname=None,
            samples=1,
            sent=1,
            received=1,
            timeout_count=0,
            current_latency_ms=1.0,
            avg_latency_ms=1.0,
            min_latency_ms=1.0,
            max_latency_ms=1.0,
            loss_percent=0.0,
            recent_loss_percent=0.0,
            jitter_ms=None,
            status=STATUS_OK,
            is_target=True,
        )
        self.trace_completed.emit([HopInfo(index=1, address="8.8.8.8", is_target=True)])
        self.measurement_updated.emit([snapshot], snapshot, [snapshot], ["정상"], [object()], [object()])


class _ErrorWorker:
    def __init__(self, *args, **kwargs) -> None:
        self.trace_completed = _Signal()
        self.measurement_updated = _Signal()
        self.error_message = _Signal()

    def run(self) -> None:
        self.error_message.emit("simulated failure")


def _stub_release_checks(monkeypatch, calls: list[str]) -> None:
    monkeypatch.setattr(verify_release, "run_pytest", lambda: calls.append("pytest"))
    monkeypatch.setattr(verify_release, "run_compileall", lambda: calls.append("compileall"))
    monkeypatch.setattr(verify_release, "run_release_policy_check", lambda: calls.append("policy"))
    monkeypatch.setattr(verify_release, "run_qt_smoke", lambda: calls.append("qt"))
    monkeypatch.setattr(verify_release, "run_export_smoke", lambda: calls.append("export"))
    monkeypatch.setattr(verify_release, "run_soak_smoke", lambda: calls.append("soak"))
    monkeypatch.setattr(verify_release, "run_exe_smoke", lambda: calls.append("exe"))


def _write_policy_tree(
    root,
    *,
    build_script: str = (
        "python -m PyInstaller --windowed --exclude-module numpy --exclude-module PIL "
        "--exclude-module lxml --exclude-module PySide6.QtQuick --exclude-module PySide6.QtPdf "
        "--add-data metadata;. app\\main.py\npython scripts\\generate_build_info.py"
    ),
    spec: str = "a = Analysis(excludes=['numpy', 'PIL', 'lxml', 'PySide6.QtQuick', 'PySide6.QtPdf'])\nexe = EXE(console=False)",
    requirements: str = (
        "PySide6==6.10.3 \\\n"
        "    --hash=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
        "openpyxl==3.1.5 \\\n"
        "    --hash=sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n"
    ),
    app_source: str = "import subprocess\n",
) -> None:
    (root / "app").mkdir()
    (root / "app" / "main.py").write_text(app_source, encoding="utf-8")
    (root / "build_windows_exe.ps1").write_text(build_script, encoding="utf-8")
    (root / "MultiPingCheck.spec").write_text(spec, encoding="utf-8")
    (root / "requirements.lock").write_text(requirements, encoding="utf-8")
    dev_requirements = (
        requirements
        + "pefile==2024.8.26 ; sys_platform == 'win32' \\\n"
        + "    --hash=sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc\n"
        + "pywin32-ctypes==0.2.3 ; sys_platform == 'win32' \\\n"
        + "    --hash=sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd\n"
    )
    (root / "requirements-dev.lock").write_text(dev_requirements, encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\ndynamic = ["version"]\n[tool.setuptools.dynamic]\nversion = {attr = "app.__version__"}\n',
        encoding="utf-8",
    )


def _parse_input_pins(path: Path) -> dict[str, str]:
    requirements = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("-r "):
            continue
        assert not line.startswith("-"), f"unsupported requirements input directive: {line}"
        requirements.append(line)
    return _parse_exact_pins(requirements)


def _parse_exact_pins(requirements: list[str]) -> dict[str, str]:
    pins: dict[str, str] = {}
    for text in requirements:
        requirement = Requirement(text)
        specifiers = list(requirement.specifier)
        assert requirement.marker is None, f"environment marker is not allowed for a direct development pin: {text}"
        assert len(specifiers) == 1 and specifiers[0].operator == "==", f"development pin must be exact: {text}"
        assert "*" not in specifiers[0].version, f"development pin must not use a wildcard: {text}"
        name = canonicalize_name(requirement.name)
        assert name not in pins, f"duplicate development pin: {name}"
        pins[name] = specifiers[0].version
    assert pins, "no direct development pins found"
    return pins


def _parse_lock_pins(path: Path) -> dict[str, str]:
    pin_pattern = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)==([^\\\s;]+)(?:\s*;[^\\]+)?\s*\\?$")
    pins: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("--hash"):
            continue
        if "==" not in line:
            continue
        match = pin_pattern.fullmatch(line)
        assert match is not None, f"unparseable lock pin: {line}"
        name = canonicalize_name(match.group(1))
        assert name not in pins, f"duplicate lock pin: {name}"
        pins[name] = match.group(2)
    assert pins, "no lock pins found"
    return pins
