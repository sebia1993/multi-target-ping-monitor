from __future__ import annotations

import json
import sys
import threading
from argparse import Namespace
from types import SimpleNamespace

import pytest

from app.storage import atomic_write as atomic_write_module
from scripts import soak_test as soak_test_module
from scripts.soak_test import (
    EVIDENCE_SCHEMA_VERSION,
    HEADLESS_EVENT_PROCESS_BUDGET_MS,
    EventLoopStats,
    ProbeCadenceEvidence,
    SimulatedPingRunner,
    build_summary,
    connect_window,
    evaluate_summary,
    minimum_probe_starts_per_target,
    parse_args,
    process_application_events,
    write_diagnostics_csv,
    write_health_csv,
    write_summary_json,
)


class _FakeSignal:
    def __init__(self) -> None:
        self.slots: list[object] = []

    def connect(self, slot) -> None:
        self.slots.append(slot)


def test_soak_release_profile_sets_fast_fifty_target_defaults() -> None:
    args = parse_args(["--profile", "release"])

    assert args.profile == "release"
    assert args.duration_seconds == 5.0
    assert args.targets == 50
    assert args.timeout_ratio == 0.8
    assert args.timeout_delay_seconds == 0.05
    assert args.event_poll_seconds == 0.02
    assert args.sample_seconds == 0.5
    assert args.progress_seconds == 0.0
    assert args.max_cpu_percent == 250.0
    assert args.with_ui is False
    assert args.event_process_max_milliseconds == HEADLESS_EVENT_PROCESS_BUDGET_MS
    assert args.max_ui_event_process_seconds == 2.0


def test_soak_long_profile_sets_thirty_minute_fifty_target_defaults() -> None:
    args = parse_args(["--profile", "long"])

    assert args.profile == "long"
    assert args.duration_seconds == 1800.0
    assert args.targets == 50
    assert args.timeout_ratio == 0.8
    assert args.timeout_delay_seconds == 1.5
    assert args.max_cpu_percent == 80.0
    assert args.with_ui is False


def test_soak_long_duration_profiles_define_four_eight_and_twenty_four_hour_runs() -> None:
    four = parse_args(["--profile", "long4h"])
    eight = parse_args(["--profile", "long8h"])
    twenty_four = parse_args(["--profile", "long24h"])

    assert four.duration_seconds == 14_400.0
    assert eight.duration_seconds == 28_800.0
    assert twenty_four.duration_seconds == 86_400.0
    assert {four.targets, eight.targets, twenty_four.targets} == {50}
    assert twenty_four.max_memory_growth_mb == 256.0
    assert {
        four.event_process_max_milliseconds,
        eight.event_process_max_milliseconds,
        twenty_four.event_process_max_milliseconds,
    } == {HEADLESS_EVENT_PROCESS_BUDGET_MS}


def test_headless_profiles_bound_each_qt_event_drain() -> None:
    for profile in ("default", "release", "long", "long4h", "long8h", "long24h"):
        args = parse_args(["--profile", profile])

        assert args.with_ui is False
        assert args.event_process_max_milliseconds == HEADLESS_EVENT_PROCESS_BUDGET_MS


def test_process_application_events_uses_bounded_qt_overload() -> None:
    from PySide6.QtCore import QEventLoop

    calls: list[tuple[object, ...]] = []
    app = SimpleNamespace(processEvents=lambda *args: calls.append(args))

    process_application_events(app, HEADLESS_EVENT_PROCESS_BUDGET_MS)
    process_application_events(app, 0)

    assert calls == [
        (QEventLoop.ProcessEventsFlag.AllEvents, HEADLESS_EVENT_PROCESS_BUDGET_MS),
        (),
    ]


def test_soak_long_duration_profiles_reject_shortened_evidence_runs() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--profile", "long4h", "--duration-seconds", "30"])


def test_soak_ui_profile_drives_offscreen_window_by_default() -> None:
    args = parse_args(["--profile", "ui"])

    assert args.profile == "ui"
    assert args.duration_seconds == 60.0
    assert args.targets == 50
    assert args.with_ui is True


def test_soak_ui_freeze_profiles_measure_ten_twenty_and_fifty_targets() -> None:
    ten = parse_args(["--profile", "ui10"])
    twenty = parse_args(["--profile", "ui20"])
    fifty = parse_args(["--profile", "ui50"])

    assert [ten.targets, twenty.targets, fifty.targets] == [10, 20, 50]
    assert ten.with_ui is True
    assert twenty.with_ui is True
    assert fifty.with_ui is True
    assert ten.duration_seconds == 600.0
    assert twenty.duration_seconds == 600.0
    assert fifty.duration_seconds == 600.0
    assert ten.event_poll_seconds == 0.01
    assert twenty.event_poll_seconds == 0.01
    assert fifty.event_poll_seconds == 0.01
    assert ten.max_ui_event_gap_seconds == 0.2
    assert twenty.max_ui_event_gap_seconds == 0.2
    assert fifty.max_ui_event_gap_seconds == 0.2
    assert ten.max_ui_event_process_seconds == 0.2
    assert twenty.max_ui_event_process_seconds == 0.2
    assert fifty.max_ui_event_process_seconds == 0.2
    assert ten.event_process_max_milliseconds == 10
    assert twenty.event_process_max_milliseconds == 10
    assert fifty.event_process_max_milliseconds == 10


def test_soak_window_connection_matches_running_main_window_state() -> None:
    running_states: list[bool] = []
    window = SimpleNamespace(
        worker=None,
        on_trace_completed=object(),
        on_route_changed=object(),
        on_measurement_updated=object(),
        on_diagnostics_updated=object(),
        on_session_log_ready=object(),
        on_status_message=object(),
        on_worker_finished=object(),
        _set_running=running_states.append,
    )
    worker = SimpleNamespace(
        trace_completed=_FakeSignal(),
        route_changed=_FakeSignal(),
        measurement_updated=_FakeSignal(),
        diagnostics_updated=_FakeSignal(),
        session_log_ready=_FakeSignal(),
        status_message=_FakeSignal(),
        finished=_FakeSignal(),
    )

    connect_window(window, worker)

    assert window.worker is worker
    assert running_states == [True]
    assert worker.measurement_updated.slots == [window.on_measurement_updated]
    assert worker.finished.slots == [window.on_worker_finished]


def test_soak_profile_allows_explicit_cli_overrides() -> None:
    args = parse_args(
        [
            "--profile",
            "release",
            "--duration-seconds",
            "12",
            "--targets",
            "7",
            "--no-ui",
            "--max-cpu-percent",
            "90",
        ]
    )

    assert args.profile == "release"
    assert args.duration_seconds == 12.0
    assert args.targets == 7
    assert args.with_ui is False
    assert args.max_cpu_percent == 90.0


def test_probe_cadence_uses_direct_due_lateness_instead_of_nearest_start() -> None:
    evidence = ProbeCadenceEvidence(1.0)
    evidence.scheduled(
        "198.51.100.1",
        scheduled_due=100.0,
        started_at=100.0,
        interval_seconds=1.0,
        include_in_cadence=True,
    )
    evidence.scheduled(
        "198.51.100.1",
        scheduled_due=101.0,
        started_at=101.6,
        interval_seconds=1.0,
        include_in_cadence=True,
    )
    evidence.started("198.51.100.1", started_at=100.0)
    evidence.finished("198.51.100.1")
    evidence.started("198.51.100.1", started_at=101.6)
    evidence.finished("198.51.100.1")

    summary = evidence.summary()

    # round() would hide this as 0.4s against the next slot. Direct comparison
    # with the selected current due preserves the full 0.6s scheduler delay.
    assert summary["cadence_max_abs_grid_drift_seconds"] == pytest.approx(0.6)
    assert summary["cadence_probe_starts"] == 2


def test_probe_cadence_keeps_bounded_timestamped_top_lateness_samples() -> None:
    evidence = ProbeCadenceEvidence(1.0)
    for index in range(12):
        target = f"198.51.100.{index + 1}"
        scheduled_due = 100.0 + index
        lateness = index / 10
        evidence.scheduled(
            target,
            scheduled_due=scheduled_due,
            started_at=scheduled_due + lateness,
            interval_seconds=1.0,
            include_in_cadence=True,
            elapsed_seconds=float(index),
            observed_at_iso=f"2026-08-25T00:00:{index:02d}.000+00:00",
        )

    evidence.scheduled(
        "198.51.100.50",
        scheduled_due=200.0,
        started_at=205.0,
        interval_seconds=1.0,
        include_in_cadence=False,
        elapsed_seconds=100.0,
        observed_at_iso="2026-08-25T00:01:40.000+00:00",
    )
    summary = evidence.summary()
    samples = summary["top_cadence_due_lateness_samples"]

    assert summary["cadence_max_abs_grid_drift_seconds"] == pytest.approx(1.1)
    assert len(samples) == 10
    assert samples[0]["lateness_seconds"] == summary["cadence_max_abs_grid_drift_seconds"]
    assert [sample["lateness_seconds"] for sample in samples] == sorted(
        (sample["lateness_seconds"] for sample in samples),
        reverse=True,
    )
    assert samples[0]["target"] == "198.51.100.12"
    assert samples[0]["lateness_seconds"] == pytest.approx(1.1)
    assert samples[0]["scheduled_due_monotonic"] == pytest.approx(111.0)
    assert samples[0]["submitted_at_monotonic"] == pytest.approx(112.1)
    assert samples[0]["elapsed_seconds"] == pytest.approx(11.0)
    assert samples[0]["observed_at_iso"] == "2026-08-25T00:00:11.000+00:00"
    assert all(sample["target"] != "198.51.100.50" for sample in samples)


def test_probe_cadence_skips_timestamp_work_for_non_top_candidate(monkeypatch) -> None:
    evidence = ProbeCadenceEvidence(1.0)
    for index in range(10):
        scheduled_due = 100.0 + index
        evidence.scheduled(
            f"198.51.100.{index + 1}",
            scheduled_due=scheduled_due,
            started_at=scheduled_due + 1.0 + index / 10,
            interval_seconds=1.0,
            include_in_cadence=True,
            observed_at_iso=f"2026-08-25T00:00:{index:02d}.000+00:00",
        )

    class UnexpectedDatetime:
        @classmethod
        def now(cls):
            raise AssertionError("non-top cadence sample must not format a wall timestamp")

    monkeypatch.setattr(soak_test_module, "datetime", UnexpectedDatetime)
    evidence.scheduled(
        "198.51.100.50",
        scheduled_due=200.0,
        started_at=200.01,
        interval_seconds=1.0,
        include_in_cadence=True,
    )

    samples = evidence.summary()["top_cadence_due_lateness_samples"]
    assert len(samples) == 10
    assert all(sample["target"] != "198.51.100.50" for sample in samples)


def test_probe_cadence_accepts_intentionally_skipped_due_slots_without_drift() -> None:
    evidence = ProbeCadenceEvidence(1.0)
    for due in (100.0, 104.0):
        evidence.scheduled(
            "198.51.100.1",
            scheduled_due=due,
            started_at=due + 0.1,
            interval_seconds=1.0,
            include_in_cadence=True,
        )
        evidence.started("198.51.100.1", started_at=due + 0.1)
        evidence.finished("198.51.100.1")

    summary = evidence.summary()

    assert summary["cadence_max_abs_grid_drift_seconds"] == pytest.approx(0.1)
    assert summary["cadence_max_start_gap_seconds"] == pytest.approx(4.0)


def test_ten_second_fifty_target_timeout_stress_preserves_healthy_cadence(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "soak_test.py",
            "--profile",
            "long",
            "--duration-seconds",
            "10",
            "--output-dir",
            str(tmp_path),
            "--session-log-root",
            str(tmp_path / "session_logs"),
            "--progress-seconds",
            "0",
        ],
    )

    assert soak_test_module.main() == 0
    summary_path = next(tmp_path.glob("soak_*_targets_*.json"))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))

    assert summary["targets"] == 50
    assert summary["timeout_ratio"] == pytest.approx(0.8)
    assert summary["probe_target_count"] == 50
    assert summary["probe_min_starts_per_target"] >= 1
    assert summary["cadence_target_count"] == 10
    assert summary["cadence_probe_starts"] >= 90
    assert summary["cadence_max_start_gap_seconds"] <= 2.0
    assert summary["cadence_max_abs_grid_drift_seconds"] <= 0.45
    assert summary["max_same_target_overlap"] == 1


def test_event_loop_stats_keep_top_gap_and_process_samples() -> None:
    stats = EventLoopStats()
    diagnostics = {
        "active_ping_count": 2,
        "pending_ping_count": 1,
        "log_queue_depth": 3,
    }

    stats.record(
        10.0,
        elapsed_seconds=0.0,
        event_process_seconds=0.01,
        updates=0,
        diagnostic_samples=0,
        last_diagnostics=diagnostics,
    )
    stats.record(
        10.05,
        elapsed_seconds=0.05,
        event_process_seconds=0.02,
        updates=1,
        diagnostic_samples=1,
        last_diagnostics=diagnostics,
    )
    stats.record(
        10.35,
        elapsed_seconds=0.35,
        event_process_seconds=0.18,
        updates=2,
        diagnostic_samples=2,
        last_diagnostics=diagnostics,
    )

    assert stats.tick_count == 3
    assert stats.max_gap_seconds == pytest.approx(0.3)
    assert stats.avg_gap_seconds == pytest.approx(0.175)
    assert stats.max_process_seconds == pytest.approx(0.18)
    assert stats.avg_process_seconds == pytest.approx(0.07)
    assert stats.top_gap_samples[0]["event_gap_seconds"] == pytest.approx(0.3)
    assert stats.top_gap_samples[0]["pending_ping_count"] == 1
    assert stats.top_process_samples[0]["event_process_seconds"] == pytest.approx(0.18)


def test_soak_evaluation_accepts_stable_thirty_minute_run() -> None:
    args = _args()
    summary = _summary(
        updates=1778,
        diagnostic_samples=1778,
        max_update_gap_seconds=2.031,
        avg_update_gap_seconds=1.013,
    )

    assert evaluate_summary(summary, args) == []


def test_soak_evaluation_rejects_slow_average_ui_updates() -> None:
    args = _args()
    summary = _summary(
        updates=1778,
        diagnostic_samples=1778,
        max_update_gap_seconds=2.031,
        avg_update_gap_seconds=1.6,
    )

    failures = evaluate_summary(summary, args)

    assert any("average update gap too high" in failure for failure in failures)


def test_soak_evaluation_rejects_slow_ui_event_processing() -> None:
    args = _args()
    summary = _summary(
        updates=1778,
        diagnostic_samples=1778,
        max_update_gap_seconds=2.031,
        avg_update_gap_seconds=1.013,
        max_ui_event_process_seconds=0.35,
    )

    failures = evaluate_summary(summary, args)

    assert any("UI event processing too slow" in failure for failure in failures)


def test_soak_evaluation_rejects_missing_session_persistence() -> None:
    args = _args()
    summary = _summary(
        updates=1778,
        diagnostic_samples=1778,
        max_update_gap_seconds=2.031,
        avg_update_gap_seconds=1.013,
        ping_calls=89_000,
        ping_results=89_000,
        session_log_rows=0,
        session_log_segments=0,
    )

    failures = evaluate_summary(summary, args)

    assert "session log was not created" in failures
    assert any("session log rows too low" in failure for failure in failures)


def test_soak_evaluation_allows_in_flight_pings_at_shutdown() -> None:
    args = _args()
    summary = _summary(
        updates=1778,
        diagnostic_samples=1778,
        max_update_gap_seconds=2.031,
        avg_update_gap_seconds=1.013,
        ping_calls=89_010,
        ping_results=89_000,
        session_log_rows=89_000,
        session_log_segments=1,
    )

    assert evaluate_summary(summary, args) == []


def test_soak_evidence_schema_accepts_fixed_cadence_and_session_lifecycle() -> None:
    args = _args()
    summary = _summary(
        updates=1778,
        diagnostic_samples=1778,
        max_update_gap_seconds=2.031,
        avg_update_gap_seconds=1.013,
    )
    summary.update(_required_evidence())

    assert evaluate_summary(summary, args) == []


def test_soak_evidence_schema_rejects_previous_cadence_semantics() -> None:
    args = _args()
    summary = _summary(
        updates=1778,
        diagnostic_samples=1778,
        max_update_gap_seconds=2.031,
        avg_update_gap_seconds=1.013,
    )
    previous_schema_version = EVIDENCE_SCHEMA_VERSION - 1
    summary["evidence_schema_version"] = previous_schema_version

    failures = evaluate_summary(summary, args)

    assert f"unsupported evidence schema: {previous_schema_version} != {EVIDENCE_SCHEMA_VERSION}" in failures


@pytest.mark.parametrize(
    "field",
    [
        "session_resume_verified",
        "session_loader_wait_completed",
        "session_loader_reserved_before_cleanup",
        "session_loader_released_after_cleanup",
    ],
)
def test_soak_evidence_schema_fails_closed_when_lifecycle_evidence_is_missing(field) -> None:
    args = _args()
    summary = _summary(
        updates=1778,
        diagnostic_samples=1778,
        max_update_gap_seconds=2.031,
        avg_update_gap_seconds=1.013,
    )
    summary.update(_required_evidence())
    summary[field] = False

    failures = evaluate_summary(summary, args)

    assert f"session lifecycle evidence failed: {field}" in failures


def test_soak_evidence_schema_rejects_same_target_overlap_and_missing_cadence() -> None:
    args = _args()
    summary = _summary(
        updates=1778,
        diagnostic_samples=1778,
        max_update_gap_seconds=2.031,
        avg_update_gap_seconds=1.013,
    )
    summary.update(_required_evidence())
    summary["max_same_target_overlap"] = 2
    summary["cadence_target_count"] = 0

    failures = evaluate_summary(summary, args)

    assert any("same-target probe overlap" in failure for failure in failures)
    assert "no healthy target cadence evidence was recorded" in failures


def test_soak_evidence_schema_rejects_incomplete_cadence_history() -> None:
    args = _args()
    summary = _summary(
        updates=1778,
        diagnostic_samples=1778,
        max_update_gap_seconds=2.031,
        avg_update_gap_seconds=1.013,
    )
    summary["cadence_probe_starts"] = 10

    failures = evaluate_summary(summary, args)

    assert any("too few cadence probe starts" in failure for failure in failures)


def test_soak_evidence_schema_rejects_incomplete_per_target_probe_coverage() -> None:
    args = _args()
    summary = _summary(
        updates=1778,
        diagnostic_samples=1778,
        max_update_gap_seconds=2.031,
        avg_update_gap_seconds=1.013,
    )
    summary["probe_target_count"] = 49
    summary["probe_min_starts_per_target"] = (
        minimum_probe_starts_per_target(
            duration_seconds=args.duration_seconds,
            interval_seconds=args.interval_seconds,
        )
        - 1
    )

    failures = evaluate_summary(summary, args)

    assert "probe target coverage too low: 49 < 50" in failures
    assert any("per-target probe starts too low" in failure for failure in failures)


def test_soak_evidence_schema_rejects_unclean_shutdown_and_excessive_drift() -> None:
    args = _args()
    summary = _summary(
        updates=1778,
        diagnostic_samples=1778,
        max_update_gap_seconds=2.031,
        avg_update_gap_seconds=1.013,
    )
    summary.update(_required_evidence())
    summary["stopped_cleanly"] = False
    summary["cadence_max_abs_grid_drift_seconds"] = 0.75
    summary["cadence_max_start_gap_seconds"] = 3.0

    failures = evaluate_summary(summary, args)

    assert "worker did not stop cleanly" in failures
    assert any("cadence due lateness too high" in failure for failure in failures)
    assert any("cadence start gap too high" in failure for failure in failures)


def test_soak_evidence_schema_fails_closed_when_a_required_field_is_absent() -> None:
    args = _args()
    summary = _summary(
        updates=1778,
        diagnostic_samples=1778,
        max_update_gap_seconds=2.031,
        avg_update_gap_seconds=1.013,
    )
    summary.pop("cadence_max_start_gap_seconds")

    failures = evaluate_summary(summary, args)

    assert failures == ["required evidence fields missing: cadence_max_start_gap_seconds"]


def test_soak_evidence_schema_requires_bounded_windows_handle_evidence() -> None:
    args = _args()
    summary = _summary(
        updates=1778,
        diagnostic_samples=1778,
        max_update_gap_seconds=2.031,
        avg_update_gap_seconds=1.013,
    )
    summary["platform"] = "nt"

    missing_failures = evaluate_summary(summary, args)

    assert any("Windows process handle fields missing" in failure for failure in missing_failures)

    summary.update(
        {
            "process_handle_samples": 360,
            "process_handle_count_final": 800,
            "max_process_handle_count": 800,
            "process_handle_growth": 300,
        }
    )

    growth_failures = evaluate_summary(summary, args)

    assert any("process handle growth too high" in failure for failure in growth_failures)


def test_soak_summary_reports_session_log_row_delta(tmp_path) -> None:
    args = parse_args(
        [
            "--profile",
            "release",
            "--output-dir",
            str(tmp_path),
            "--session-log-root",
            str(tmp_path / "session_logs"),
        ]
    )

    summary = build_summary(
        args=args,
        elapsed=5.0,
        updates=[],
        diagnostics_rows=[],
        health_rows=[{"current_memory_bytes": 1000, "active_threads": 1}],
        event_loop_stats=EventLoopStats(),
        errors=[],
        session_log_paths=[],
        ping_calls={"198.51.100.1": 5},
        ping_results={"198.51.100.1": 4},
        traceroute_calls=1,
        cpu_seconds=0.1,
        current_memory_bytes=1200,
        peak_memory_bytes=1500,
        diagnostics_csv_path=tmp_path / "diagnostics.csv",
        health_csv_path=tmp_path / "health.csv",
        stopped_cleanly=True,
    )

    assert summary["session_log_min_expected_rows"] == 4
    assert summary["session_log_row_delta"] == -4


def test_simulated_ping_runner_updates_counters_under_lock() -> None:
    lock = threading.Lock()
    calls = _LockCheckedCounter(lock)
    results = _LockCheckedCounter(lock)
    runner = SimulatedPingRunner(
        timeout_ms=1000,
        timeout_start_index=1,
        timeout_delay_seconds=0,
        calls=calls,
        results=results,
        counter_lock=lock,
    )

    runner.ping("198.51.100.1")

    assert calls["198.51.100.1"] == 1
    assert results["198.51.100.1"] == 1


def test_soak_csv_writes_retry_transient_replace_error(tmp_path, monkeypatch) -> None:
    path = tmp_path / "diagnostics.csv"
    attempts = 0
    original_replace = atomic_write_module._replace_path

    def flaky_replace(source, target):
        nonlocal attempts
        attempts += 1
        if target == path and attempts < 3:
            raise OSError("sharing violation")
        return original_replace(source, target)

    monkeypatch.setattr(atomic_write_module, "EXPORT_IO_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(atomic_write_module, "_replace_path", flaky_replace)

    write_diagnostics_csv(path, [{"elapsed_seconds": 1.0, "timestamp": "2026-01-01T00:00:00"}])

    assert attempts == 3
    assert "elapsed_seconds" in path.read_text(encoding="utf-8")
    assert list(tmp_path.glob(".diagnostics.csv.*")) == []


def test_soak_summary_json_preserves_existing_file_after_replace_failure(tmp_path, monkeypatch) -> None:
    path = tmp_path / "soak_50_targets_20260101_010101.json"
    path.write_text('{"status":"existing"}', encoding="utf-8")
    original_replace = atomic_write_module._replace_path

    def locked_replace(source, target):
        if target == path:
            raise PermissionError("locked")
        return original_replace(source, target)

    monkeypatch.setattr(atomic_write_module, "EXPORT_IO_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(atomic_write_module, "_replace_path", locked_replace)

    with pytest.raises(PermissionError):
        write_summary_json(path, {"status": "new"})

    assert path.read_text(encoding="utf-8") == '{"status":"existing"}'
    assert list(tmp_path.glob(".soak_50_targets_20260101_010101.json.*")) == []


def test_soak_health_csv_writes_atomically(tmp_path) -> None:
    path = tmp_path / "health.csv"

    write_health_csv(path, [{"elapsed_seconds": 1.0, "current_memory_bytes": 1024}])

    assert "current_memory_bytes" in path.read_text(encoding="utf-8")


def test_soak_evaluation_rejects_resource_pressure() -> None:
    args = _args()
    summary = _summary(
        updates=1778,
        diagnostic_samples=1778,
        max_update_gap_seconds=2.031,
        avg_update_gap_seconds=1.013,
        max_pending_ping_count=99,
        max_log_queue_depth=999,
        max_active_threads=99,
    )

    failures = evaluate_summary(summary, args)

    assert any("pending ping count too high" in failure for failure in failures)
    assert any("log queue depth too high" in failure for failure in failures)
    assert any("active thread count too high" in failure for failure in failures)


def test_soak_evaluation_rejects_memory_and_cpu_growth() -> None:
    args = _args()
    summary = _summary(
        updates=1778,
        diagnostic_samples=1778,
        max_update_gap_seconds=2.031,
        avg_update_gap_seconds=1.013,
        memory_growth_bytes=200 * 1024 * 1024,
        cpu_percent=95.0,
    )

    failures = evaluate_summary(summary, args)

    assert any("memory growth too high" in failure for failure in failures)
    assert any("CPU usage too high" in failure for failure in failures)


def _args() -> Namespace:
    return Namespace(
        duration_seconds=1800.0,
        interval_seconds=1,
        timeout_delay_seconds=1.5,
        timeout_ratio=0.8,
        targets=50,
        max_update_gap_seconds=None,
        max_average_update_gap_seconds=None,
        max_ui_event_gap_seconds=2.0,
        max_ui_event_process_seconds=0.2,
        max_pending_pings=None,
        max_log_queue_depth=None,
        max_active_threads=40,
        max_memory_growth_mb=96.0,
        max_cpu_percent=80.0,
        no_require_backoff=False,
        session_log_root=None,
    )


def _summary(
    *,
    updates: int,
    diagnostic_samples: int,
    max_update_gap_seconds: float,
    avg_update_gap_seconds: float,
    ping_calls: int = 89_000,
    ping_results: int | None = None,
    session_log_rows: int = 89_000,
    session_log_segments: int = 1,
    max_pending_ping_count: int = 18,
    max_log_queue_depth: int = 8,
    max_active_threads: int = 24,
    memory_growth_bytes: int = 5_100_000,
    cpu_percent: float = 2.7,
    max_ui_event_process_seconds: float = 0.075,
) -> dict[str, object]:
    completed = ping_calls if ping_results is None else ping_results
    summary = {
        "errors": [],
        "stopped_cleanly": True,
        "updates": updates,
        "diagnostic_samples": diagnostic_samples,
        "max_update_gap_seconds": max_update_gap_seconds,
        "avg_update_gap_seconds": avg_update_gap_seconds,
        "max_ui_event_gap_seconds": 0.075,
        "max_ui_event_process_seconds": max_ui_event_process_seconds,
        "max_pending_ping_count": max_pending_ping_count,
        "max_log_queue_depth": max_log_queue_depth,
        "max_active_threads": max_active_threads,
        "memory_growth_bytes": memory_growth_bytes,
        "cpu_percent": cpu_percent,
        "max_backoff_target_count": 41,
        "traceroute_calls": 30,
        "ping_calls": ping_calls,
        "ping_results": completed,
        "session_log_rows": session_log_rows,
        "session_log_segments": session_log_segments,
    }
    summary.update(_required_evidence())
    return summary


def _required_evidence() -> dict[str, object]:
    return {
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "platform": "posix",
        "active_threads_final": 1,
        "max_active_ping_count": 20,
        "probe_target_count": 50,
        "probe_min_starts_per_target": 288,
        "probe_max_starts_per_target": 1780,
        "cadence_target_count": 10,
        "cadence_probe_starts": 17_780,
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


class _LockCheckedCounter(dict):
    def __init__(self, lock: threading.Lock) -> None:
        super().__init__()
        self._lock = lock

    def get(self, key, default=None):
        assert self._lock.locked()
        return super().get(key, default)

    def __setitem__(self, key, value) -> None:
        assert self._lock.locked()
        super().__setitem__(key, value)
