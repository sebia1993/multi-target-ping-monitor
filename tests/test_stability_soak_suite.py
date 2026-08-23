from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.storage import atomic_write as atomic_write_module
from scripts import run_stability_soak_suite as suite

ROOT = Path(__file__).resolve().parents[1]


def test_soak_suite_defaults_cover_long_and_ui_profiles() -> None:
    args = suite.parse_args([])

    assert args.profiles == ["long4h", "long8h", "long24h", "ui10", "ui20", "ui50"]


def test_soak_suite_builds_profile_command_without_shortening_real_duration(tmp_path) -> None:
    command = suite.build_profile_command(
        "long4h",
        output_dir=tmp_path / "long4h",
        python_executable="python",
    )

    assert command == [
        "python",
        str(Path("scripts") / "soak_test.py"),
        "--profile",
        "long4h",
        "--output-dir",
        str(tmp_path / "long4h"),
    ]
    assert "--duration-seconds" not in command


def test_soak_suite_duration_override_is_explicit_for_smoke_only(tmp_path) -> None:
    command = suite.build_profile_command(
        "ui10",
        output_dir=tmp_path / "ui10",
        python_executable="python",
        override_duration_seconds=3.0,
    )

    assert command[-2:] == ["--duration-seconds", "3.0"]


def test_soak_suite_rejects_duration_override_for_long4h_evidence(tmp_path) -> None:
    with pytest.raises(ValueError, match="fixed-duration evidence"):
        suite.build_profile_command(
            "long4h",
            output_dir=tmp_path / "long4h",
            python_executable="python",
            override_duration_seconds=3.0,
        )

    with pytest.raises(SystemExit):
        suite.parse_args(
            [
                "--profiles",
                "long4h",
                "--override-duration-seconds",
                "3",
            ]
        )


def test_soak_suite_dry_run_writes_manifest_without_running_profiles(tmp_path) -> None:
    exit_code = suite.main(
        [
            "--dry-run",
            "--run-id",
            "dry-run",
            "--output-dir",
            str(tmp_path),
            "--profiles",
            "long4h",
            "ui10",
        ]
    )

    manifest_path = tmp_path / "dry-run" / "stability_soak_suite.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert payload["planned_only"] is True
    assert payload["passed"] is False
    assert payload["profiles_requested"] == ["long4h", "ui10"]
    assert [result["status"] for result in payload["results"]] == ["planned", "planned"]
    assert all(Path(result["output_dir"]).name in {"long4h", "ui10"} for result in payload["results"])
    assert payload["results"][0]["thresholds"]["minimum_duration_seconds"] == 13_680.0
    assert payload["results"][1]["thresholds"]["max_ui_event_gap_seconds"] == 0.2


def test_soak_suite_manifest_preserves_existing_file_after_replace_failure(tmp_path, monkeypatch) -> None:
    manifest_path = tmp_path / "stability_soak_suite.json"
    manifest_path.write_text('{"status":"existing"}', encoding="utf-8")
    original_replace = atomic_write_module._replace_path

    def locked_replace(source, target):
        if target == manifest_path:
            raise PermissionError("locked")
        return original_replace(source, target)

    monkeypatch.setattr(atomic_write_module, "EXPORT_IO_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(atomic_write_module, "_replace_path", locked_replace)

    with pytest.raises(PermissionError):
        suite.write_manifest(
            manifest_path,
            started_at="2026-01-01T01:00:00",
            profiles=["release"],
            results=[],
            finished_at=None,
        )

    assert manifest_path.read_text(encoding="utf-8") == '{"status":"existing"}'
    assert list(tmp_path.glob(".stability_soak_suite.json.*")) == []


def test_soak_suite_evidence_report_file_retries_transient_replace_error(tmp_path, monkeypatch, capsys) -> None:
    report_path = tmp_path / "stability_soak_evidence.json"
    attempts = 0
    original_replace = atomic_write_module._replace_path

    def flaky_replace(source, target):
        nonlocal attempts
        attempts += 1
        if target == report_path and attempts < 3:
            raise OSError("sharing violation")
        return original_replace(source, target)

    monkeypatch.setattr(atomic_write_module, "EXPORT_IO_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(atomic_write_module, "_replace_path", flaky_replace)

    suite.emit_evidence_report({"passed": True, "profiles": []}, report_path=report_path)

    assert attempts == 3
    assert json.loads(report_path.read_text(encoding="utf-8")) == {"passed": True, "profiles": []}
    assert json.loads(capsys.readouterr().out) == {"passed": True, "profiles": []}
    assert list(tmp_path.glob(".stability_soak_evidence.json.*")) == []


def test_soak_suite_profile_thresholds_capture_long_run_evidence_limits() -> None:
    thresholds = suite.profile_thresholds("long24h")

    assert thresholds["expected_duration_seconds"] == 86_400.0
    assert thresholds["minimum_duration_seconds"] == 82_080.0
    assert thresholds["targets"] == 50
    assert thresholds["max_active_threads"] == 40
    assert thresholds["max_memory_growth_mb"] == 256.0
    assert thresholds["max_cpu_percent"] == 70.0
    assert thresholds["max_pending_ping_count"] == 28
    assert thresholds["max_same_target_overlap"] == 1
    assert thresholds["max_cadence_grid_drift_seconds"] == 0.45
    assert thresholds["max_cadence_start_gap_seconds"] == 2.0
    assert thresholds["max_process_handle_growth"] == 256


def test_soak_suite_validate_only_rejects_dry_run_manifest(tmp_path) -> None:
    suite.main(
        [
            "--dry-run",
            "--run-id",
            "dry-run",
            "--output-dir",
            str(tmp_path),
            "--profiles",
            "long4h",
        ]
    )

    exit_code = suite.main(
        [
            "--validate-only",
            "--run-id",
            "dry-run",
            "--output-dir",
            str(tmp_path),
            "--profiles",
            "long4h",
        ]
    )

    assert exit_code == 1


def test_soak_suite_validate_accepts_completed_manifest(tmp_path) -> None:
    run_root = tmp_path / "completed"
    run_root.mkdir()
    summary_path = run_root / "soak_50_targets_20260101_010101.json"
    summary_path.write_text(
        json.dumps(_summary("release", duration_seconds=5.0), ensure_ascii=False),
        encoding="utf-8",
    )
    manifest_path = run_root / "stability_soak_suite.json"
    suite.write_manifest(
        manifest_path,
        started_at="2026-01-01T01:00:00",
        profiles=["release"],
        results=[
            {
                "profile": "release",
                "status": "passed",
                "summary_json": str(summary_path),
                "failures": [],
            }
        ],
        finished_at="2026-01-01T01:00:05",
    )

    assert suite.validate_manifest(manifest_path, ["release"]) == []


def test_soak_suite_load_manifest_retries_transient_read_error(tmp_path, monkeypatch) -> None:
    manifest_path = tmp_path / "stability_soak_suite.json"
    suite.write_manifest(
        manifest_path,
        started_at="2026-01-01T01:00:00",
        profiles=["release"],
        results=[],
        finished_at=None,
    )
    attempts = 0
    original_read = atomic_write_module._read_text_path

    def flaky_read(path, *, encoding):
        nonlocal attempts
        if path == manifest_path:
            attempts += 1
            if attempts < 3:
                raise PermissionError("temporarily locked")
        return original_read(path, encoding=encoding)

    monkeypatch.setattr(atomic_write_module, "EXPORT_IO_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(atomic_write_module, "_read_text_path", flaky_read)

    payload = suite.load_manifest(manifest_path)

    assert attempts == 3
    assert payload is not None
    assert payload["profiles_requested"] == ["release"]


def test_soak_suite_validate_retries_transient_summary_read_error(tmp_path, monkeypatch) -> None:
    run_root = tmp_path / "completed"
    run_root.mkdir()
    summary_path = run_root / "soak_50_targets_20260101_010101.json"
    summary_path.write_text(
        json.dumps(_summary("release", duration_seconds=5.0), ensure_ascii=False),
        encoding="utf-8",
    )
    manifest_path = run_root / "stability_soak_suite.json"
    suite.write_manifest(
        manifest_path,
        started_at="2026-01-01T01:00:00",
        profiles=["release"],
        results=[
            {
                "profile": "release",
                "status": "passed",
                "summary_json": str(summary_path),
                "failures": [],
            }
        ],
        finished_at="2026-01-01T01:00:05",
    )
    attempts = 0
    original_read = atomic_write_module._read_text_path

    def flaky_read(path, *, encoding):
        nonlocal attempts
        if path == summary_path:
            attempts += 1
            if attempts < 3:
                raise PermissionError("temporarily locked")
        return original_read(path, encoding=encoding)

    monkeypatch.setattr(atomic_write_module, "EXPORT_IO_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(atomic_write_module, "_read_text_path", flaky_read)

    assert suite.validate_manifest(manifest_path, ["release"]) == []
    assert attempts == 3


def test_soak_suite_evidence_report_summarizes_thresholds_and_checks(tmp_path) -> None:
    run_root = tmp_path / "evidence"
    run_root.mkdir()
    summary_path = run_root / "soak_50_targets_20260101_010101.json"
    summary = _summary("release", duration_seconds=5.0, ping_results=200, session_log_rows=205)
    summary["session_log_min_expected_rows"] = 200
    summary["session_log_row_delta"] = 5
    summary_path.write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
    manifest_path = run_root / "stability_soak_suite.json"
    suite.write_manifest(
        manifest_path,
        started_at="2026-01-01T01:00:00",
        profiles=["release"],
        results=[
            {
                "profile": "release",
                "status": "passed",
                "summary_json": str(summary_path),
                "thresholds": suite.profile_thresholds("release"),
                "failures": [],
            }
        ],
        finished_at="2026-01-01T01:00:05",
    )

    report = suite.build_evidence_report(manifest_path, ["release"], validation_failures=[])
    profile_report = report["profiles"][0]

    assert report["passed"] is True
    assert profile_report["summary_exists"] is True
    assert profile_report["thresholds"]["minimum_duration_seconds"] == 4.75
    assert profile_report["measurements"]["session_log_rows"] == 205
    assert profile_report["measurements"]["session_log_row_delta"] == 5
    assert profile_report["measurements"]["cadence_max_abs_grid_drift_seconds"] == 0.05
    assert profile_report["measurements"]["max_same_target_overlap"] == 1
    assert profile_report["measurements"]["session_resume_verified"] is True
    assert profile_report["checks"]["evidence_schema_ok"] is True
    assert profile_report["checks"]["duration_ok"] is True
    assert profile_report["checks"]["pending_ping_ok"] is True
    assert profile_report["checks"]["cadence_drift_ok"] is True
    assert profile_report["checks"]["cadence_gap_ok"] is True
    assert profile_report["checks"]["same_target_overlap_ok"] is True
    assert profile_report["checks"]["process_handles_ok"] is None
    assert profile_report["checks"]["session_resume_ok"] is True
    assert profile_report["checks"]["session_loader_ownership_ok"] is True
    assert profile_report["checks"]["clean_shutdown_ok"] is True
    assert profile_report["checks"]["session_log_ok"] is True
    assert profile_report["checks"]["session_log_delta_ok"] is True


def test_soak_suite_validate_only_can_print_evidence_report(tmp_path, capsys) -> None:
    run_root = tmp_path / "validate-report"
    run_root.mkdir()
    summary_path = run_root / "soak_50_targets_20260101_010101.json"
    summary = _summary("release", duration_seconds=5.0, ping_results=100, session_log_rows=100)
    summary["session_log_min_expected_rows"] = 100
    summary["session_log_row_delta"] = 0
    summary_path.write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
    suite.write_manifest(
        run_root / "stability_soak_suite.json",
        started_at="2026-01-01T01:00:00",
        profiles=["release"],
        results=[
            {
                "profile": "release",
                "status": "passed",
                "summary_json": str(summary_path),
                "thresholds": suite.profile_thresholds("release"),
                "failures": [],
            }
        ],
        finished_at="2026-01-01T01:00:05",
    )

    report_path = run_root / "stability_soak_evidence.json"
    exit_code = suite.main(
        [
            "--validate-only",
            "--evidence-report",
            "--evidence-report-path",
            str(report_path),
            "--run-id",
            "validate-report",
            "--output-dir",
            str(tmp_path),
            "--profiles",
            "release",
        ]
    )
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert exit_code == 0
    assert report["validation_failures"] == []
    assert report["profiles"][0]["checks"]["session_log_ok"] is True
    assert json.loads(report_path.read_text(encoding="utf-8")) == report


def test_soak_suite_validate_accepts_downloaded_artifact_paths(tmp_path) -> None:
    run_id = "downloaded-run"
    run_root = tmp_path / "artifact" / run_id
    profile_root = run_root / "release"
    profile_root.mkdir(parents=True)
    summary_path = profile_root / "soak_50_targets_20260101_010101.json"
    summary_path.write_text(
        json.dumps(_summary("release", duration_seconds=5.0), ensure_ascii=False),
        encoding="utf-8",
    )
    manifest_path = run_root / "stability_soak_suite.json"
    runner_summary_path = (
        Path("C:/runner/work/ping/ping/artifacts/stability_soak_suite") / run_id / "release" / summary_path.name
    )
    suite.write_manifest(
        manifest_path,
        started_at="2026-01-01T01:00:00",
        profiles=["release"],
        results=[
            {
                "profile": "release",
                "status": "passed",
                "summary_json": str(runner_summary_path),
                "failures": [],
            }
        ],
        finished_at="2026-01-01T01:00:05",
    )

    assert suite.validate_manifest(manifest_path, ["release"]) == []


def test_soak_suite_validate_rejects_short_long_run(tmp_path) -> None:
    summary = _summary("long4h", duration_seconds=10.0)

    failures = suite.validate_summary("long4h", summary)

    assert any("duration too short" in failure for failure in failures)


def test_soak_suite_validate_rejects_session_log_row_loss() -> None:
    summary = _summary("release", duration_seconds=5.0, ping_results=200, session_log_rows=10)

    failures = suite.validate_summary("release", summary)

    assert any("session log rows too low" in failure for failure in failures)


def test_soak_suite_validate_rejects_missing_stability_fields() -> None:
    failures = suite.validate_summary("release", {"profile": "release", "duration_seconds": 5.0})

    assert any("required evidence fields missing" in failure for failure in failures)


def test_soak_suite_resume_reuses_existing_passed_profile(tmp_path, monkeypatch) -> None:
    run_root = tmp_path / "resume-run"
    run_root.mkdir()
    summary_path = run_root / "soak_50_targets_20260101_010101.json"
    summary_path.write_text(
        json.dumps(_summary("release", duration_seconds=5.0), ensure_ascii=False),
        encoding="utf-8",
    )
    suite.write_manifest(
        run_root / "stability_soak_suite.json",
        started_at="2026-01-01T01:00:00",
        profiles=["release"],
        results=[
            {
                "profile": "release",
                "status": "passed",
                "summary_json": str(summary_path),
                "failures": [],
            }
        ],
        finished_at="2026-01-01T01:00:05",
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("passed profile should be reused")

    monkeypatch.setattr(suite, "run_profile", fail_if_called)

    exit_code = suite.main(
        [
            "--resume",
            "--run-id",
            "resume-run",
            "--output-dir",
            str(tmp_path),
            "--profiles",
            "release",
        ]
    )

    assert exit_code == 0


def test_soak_suite_loads_latest_summary(tmp_path) -> None:
    old = tmp_path / "soak_50_targets_20260101_010101.json"
    new = tmp_path / "soak_50_targets_20260101_020202.json"
    old.write_text('{"failures": ["old"]}', encoding="utf-8")
    new.write_text('{"failures": []}', encoding="utf-8")

    summary = suite.load_latest_summary(tmp_path)

    assert summary is not None
    assert summary["path"] == new
    assert summary["data"]["failures"] == []


def test_soak_suite_loads_only_summaries_created_after_run_start(tmp_path) -> None:
    stale = tmp_path / "soak_50_targets_20260101_010101.json"
    fresh = tmp_path / "soak_50_targets_20260101_020202.json"
    stale.write_text('{"failures": ["stale"]}', encoding="utf-8")
    fresh.write_text('{"failures": []}', encoding="utf-8")
    os.utime(stale, (100.0, 100.0))
    os.utime(fresh, (200.0, 200.0))

    summary = suite.load_latest_summary(tmp_path, since=150.0)

    assert summary is not None
    assert summary["path"] == fresh
    assert summary["data"]["failures"] == []


def test_soak_suite_allows_filesystem_mtime_grace_for_new_summary(tmp_path) -> None:
    fresh = tmp_path / "soak_50_targets_20260101_020202.json"
    fresh.write_text('{"failures": []}', encoding="utf-8")
    os.utime(fresh, (149.5, 149.5))

    summary = suite.load_latest_summary(tmp_path, since=150.0)

    assert summary is not None
    assert summary["path"] == fresh


def test_soak_suite_run_profile_rejects_success_without_new_summary(tmp_path, monkeypatch) -> None:
    args = SimpleNamespace(
        python_executable="python",
        override_duration_seconds=None,
        dry_run=False,
    )
    profile_root = tmp_path / "run" / "release"
    profile_root.mkdir(parents=True)
    stale_summary = profile_root / "soak_50_targets_20260101_010101.json"
    stale_summary.write_text(
        json.dumps(_summary("release", duration_seconds=5.0), ensure_ascii=False),
        encoding="utf-8",
    )
    os.utime(stale_summary, (100.0, 100.0))

    monkeypatch.setattr(suite.subprocess, "run", lambda _command, cwd: SimpleNamespace(returncode=0))

    result = suite.run_profile("release", args=args, run_root=tmp_path / "run")

    assert result["status"] == "failed"
    assert result["summary_json"] is None
    assert result["failures"] == ["summary JSON was not created"]


def test_soak_suite_result_records_session_and_thread_evidence(tmp_path, monkeypatch) -> None:
    args = SimpleNamespace(
        python_executable="python",
        override_duration_seconds=None,
        dry_run=False,
    )

    def fake_run(_command, cwd):
        profile_root = tmp_path / "run" / "release"
        profile_root.mkdir(parents=True, exist_ok=True)
        summary = _summary("release", duration_seconds=5.0, ping_results=200, session_log_rows=205)
        summary["session_log_min_expected_rows"] = 200
        summary["session_log_row_delta"] = 5
        summary["max_active_threads"] = 24
        (profile_root / "soak_50_targets_20260101_010101.json").write_text(
            json.dumps(summary, ensure_ascii=False),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(suite.subprocess, "run", fake_run)

    result = suite.run_profile("release", args=args, run_root=tmp_path / "run")

    assert result["status"] == "passed"
    assert result["session_log_rows"] == 205
    assert result["session_log_min_expected_rows"] == 200
    assert result["session_log_row_delta"] == 5
    assert result["max_active_threads"] == 24
    assert result["thresholds"]["targets"] == 50


def test_soak_suite_records_summary_paths_relative_to_run_root(tmp_path) -> None:
    run_root = tmp_path / "portable-run"
    summary_path = run_root / "release" / "soak_50_targets_20260101_010101.json"
    summary_path.parent.mkdir(parents=True)
    summary_path.write_text(
        json.dumps(_summary("release", duration_seconds=5.0), ensure_ascii=False),
        encoding="utf-8",
    )

    recorded = suite._manifest_path_value(summary_path, run_root=run_root)
    manifest_path = run_root / "stability_soak_suite.json"
    suite.write_manifest(
        manifest_path,
        started_at="2026-01-01T01:00:00",
        profiles=["release"],
        results=[
            {
                "profile": "release",
                "status": "passed",
                "summary_json": recorded,
                "failures": [],
            }
        ],
        finished_at="2026-01-01T01:00:05",
    )

    assert recorded == str(Path("release") / summary_path.name)
    assert suite.validate_manifest(manifest_path, ["release"]) == []


def test_manual_stability_soak_workflow_is_manual_only() -> None:
    text = (ROOT / ".github" / "workflows" / "stability-soak.yml").read_text(encoding="utf-8")

    assert "workflow_dispatch:" in text
    assert "\n  push:" not in text
    assert "\n  schedule:" not in text
    assert "self-hosted-windows" in text
    assert "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1" in text
    assert "--override-duration-seconds" in text
    assert "fixed-duration evidence profiles and cannot use an override" in text
    assert "Cadence Drift" in text
    assert "Overlap" in text
    assert "Pending" in text
    assert "Handles Growth" in text
    assert "Resume" in text
    assert "Loader Ownership" in text
    assert "Memory Growth" in text
    assert "--evidence-report" in text
    assert "stability_soak_evidence.json" in text
    assert text.count('throw "stability soak evidence gate failed"') == 2
    assert "Join-Path (Split-Path $manifest -Parent) $result.summary_json" in text
    assert text.count("if: always()") >= 4


def test_manual_stability_soak_blocks_long_profiles_on_github_hosted_windows() -> None:
    text = (ROOT / ".github" / "workflows" / "stability-soak.yml").read_text(encoding="utf-8")

    github_hosted_job = text.split("  self_hosted_windows:", maxsplit=1)[0]

    assert "long8h" in github_hosted_job
    assert "long24h" in github_hosted_job
    assert "runner_mode=self-hosted-windows" in github_hosted_job
    assert "timeout-minutes: 360" in github_hosted_job


def _summary(
    profile: str,
    *,
    duration_seconds: float,
    ping_results: int = 10,
    session_log_rows: int | None = None,
) -> dict[str, object]:
    profile_defaults = suite.SOAK_PROFILES[profile]
    interval_seconds = int(profile_defaults["interval_seconds"])
    expected_updates = max(int(float(profile_defaults["duration_seconds"]) // max(interval_seconds, 1)), 1)
    rows = ping_results if session_log_rows is None else session_log_rows
    cadence_targets = max(round(int(profile_defaults["targets"]) * (1 - float(profile_defaults["timeout_ratio"]))), 1)
    return {
        "evidence_schema_version": 2,
        "profile": profile,
        "platform": "posix",
        "targets": profile_defaults["targets"],
        "timeout_ratio": profile_defaults["timeout_ratio"],
        "duration_seconds": duration_seconds,
        "with_ui": profile_defaults["with_ui"],
        "updates": expected_updates,
        "errors": [],
        "failures": [],
        "stopped_cleanly": True,
        "session_log_rows": rows,
        "session_log_segments": 1,
        "ping_calls": ping_results,
        "ping_results": ping_results,
        "traceroute_calls": max(int(float(profile_defaults["duration_seconds"]) // 30.0), 1),
        "active_threads_final": 1,
        "max_active_threads": min(int(profile_defaults["max_active_threads"]), 24),
        "max_active_ping_count": 20,
        "cpu_percent": 1.0,
        "memory_growth_bytes": 1024,
        "max_update_gap_seconds": 1.0,
        "avg_update_gap_seconds": 1.0,
        "max_ui_event_gap_seconds": 0.02,
        "max_ui_event_process_seconds": 0.01,
        "diagnostic_samples": expected_updates,
        "max_pending_ping_count": 0,
        "max_log_queue_depth": 1,
        "max_backoff_target_count": 1,
        "cadence_target_count": cadence_targets,
        "cadence_probe_starts": cadence_targets * expected_updates,
        "cadence_max_abs_grid_drift_seconds": 0.05,
        "cadence_max_start_gap_seconds": 1.05,
        "cadence_avg_start_gap_seconds": 1.0,
        "max_same_target_overlap": 1,
        "session_resume_verified": True,
        "session_loader_wait_completed": True,
        "session_loader_reserved_before_cleanup": True,
        "session_loader_released_after_cleanup": True,
        "session_lifecycle_errors": [],
    }
