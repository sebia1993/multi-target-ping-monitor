from __future__ import annotations

import threading
import time
from collections import deque
from concurrent.futures import Future
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from app.core.alerts import AlertRuleConfig
from app.core.metrics import TargetMetricTracker
from app.core.models import STATUS_ERROR, STATUS_OK, STATUS_PAUSED, STATUS_TIMEOUT, HopInfo, HopObservation, PingResult
from app.storage import session_log as session_log_module
from app.storage.route_log import RouteLogWriter, route_changes_in_range
from app.storage.session_index import SESSION_STATE_ARCHIVED, SESSION_STATE_PAUSED, SessionIndexStore
from app.storage.session_log import SessionLogWriter, read_observations
from app.ui import worker as worker_module
from app.ui.worker import (
    MEASUREMENT_MODE_FINAL_HOP_ONLY,
    MEASUREMENT_MODE_FULL_ROUTE,
    PROBE_ENGINE_TCP_CONNECT,
    RECENT_OBSERVATION_LIMIT,
    MeasurementWorker,
    TargetProbeState,
    alert_history_observation_limit,
)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_alert_history_limit_keeps_both_timer_window_boundaries() -> None:
    config = AlertRuleConfig(timer_window_seconds=300)
    limit = alert_history_observation_limit(config, interval_seconds=1)
    tracker = TargetMetricTracker("198.51.100.10", recent_observation_limit=limit)
    started_at = datetime(2026, 1, 1, 12, 0, 0)

    for second in range(301):
        tracker.add_result(
            PingResult(
                "198.51.100.10",
                False,
                None,
                STATUS_TIMEOUT,
                started_at + timedelta(seconds=second),
            )
        )

    assert limit >= 301
    assert len(tracker.observations) == 301
    assert (tracker.observations[-1].timestamp - tracker.observations[0].timestamp).total_seconds() == 300


def test_worker_reports_invalid_target_without_starting_network() -> None:
    _app()
    worker = MeasurementWorker("https://example.com", interval_seconds=0, max_cycles=1)
    errors: list[str] = []
    worker.error_message.connect(errors.append)

    worker.run()

    assert errors
    assert "IPv4" in errors[0]


def test_worker_cleans_partially_created_executors_when_startup_fails(monkeypatch) -> None:
    _app()
    created: list[object] = []

    class FailingExecutor:
        def __init__(self, *, max_workers: int) -> None:
            self.max_workers = max_workers
            self.shutdown_calls = 0
            created.append(self)
            if len(created) == 2:
                raise RuntimeError("executor allocation failed")

        def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
            assert wait is True
            assert cancel_futures is True
            self.shutdown_calls += 1

    monkeypatch.setattr(worker_module, "ThreadPoolExecutor", FailingExecutor)
    worker = MeasurementWorker(
        "198.51.100.10",
        interval_seconds=0,
        max_cycles=1,
        measurement_mode=MEASUREMENT_MODE_FINAL_HOP_ONLY,
    )
    errors: list[str] = []
    worker.error_message.connect(errors.append)

    worker.run()

    assert any("WORKER_UNEXPECTED_ERROR: RuntimeError" in error for error in errors)
    assert created[0].shutdown_calls == 1


def test_worker_closes_session_writer_when_startup_fails_after_creation(monkeypatch, tmp_path) -> None:
    _app()
    writer = SessionLogWriter(tmp_path / "startup_failed.samples.csv")
    original_close = writer.close
    closed: list[bool] = []

    def close_writer() -> None:
        original_close()
        closed.append(True)

    writer.close = close_writer

    def create_log(cls, target: str, root=None):
        return writer

    def fail_route_log(_path: Path):
        raise PermissionError("route log directory locked")

    monkeypatch.setattr(worker_module.SessionLogWriter, "create", classmethod(create_log))
    monkeypatch.setattr(worker_module.RouteLogWriter, "create_for_session", fail_route_log)
    worker = MeasurementWorker(
        "198.51.100.10",
        interval_seconds=0,
        max_cycles=1,
        measurement_mode=MEASUREMENT_MODE_FINAL_HOP_ONLY,
    )
    errors: list[str] = []
    worker.error_message.connect(errors.append)

    worker.run()

    assert closed == [True]
    assert any("WORKER_UNEXPECTED_ERROR: PermissionError" in error for error in errors)


def test_worker_emits_route_change_when_refreshed_trace_differs() -> None:
    _app()
    worker = MeasurementWorker("198.51.100.10", interval_seconds=0, max_cycles=1)
    changes: list[object] = []
    worker.route_changed.connect(changes.append)

    first: Future[list[HopInfo]] = Future()
    first.set_result(
        [
            HopInfo(index=1, address="192.0.2.1", hostname="gateway"),
            HopInfo(index=2, address="198.51.100.10", hostname="target", is_target=True),
        ]
    )
    metrics, hops = worker._refresh_trace_result(None, [], first, first_check=True)

    second: Future[list[HopInfo]] = Future()
    second.set_result(
        [
            HopInfo(index=1, address="192.0.2.254", hostname="backup"),
            HopInfo(index=2, address="198.51.100.10", hostname="target", is_target=True),
        ]
    )
    worker._refresh_trace_result(metrics, hops, second)

    assert len(changes) == 1
    assert getattr(changes[0], "changed_hops") == (1,)


def test_worker_persists_route_change_snapshots(tmp_path) -> None:
    _app()
    worker = MeasurementWorker("198.51.100.10", interval_seconds=0, max_cycles=1)
    route_log_path = tmp_path / "routes.csv"

    with RouteLogWriter(route_log_path) as route_log:
        first: Future[list[HopInfo]] = Future()
        first.set_result(
            [
                HopInfo(index=1, address="192.0.2.1", hostname="gateway"),
                HopInfo(index=2, address="198.51.100.10", hostname="target", is_target=True),
            ]
        )
        metrics, hops = worker._refresh_trace_result(None, [], first, route_log, first_check=True)

        second: Future[list[HopInfo]] = Future()
        second.set_result(
            [
                HopInfo(index=1, address="192.0.2.254", hostname="backup"),
                HopInfo(index=2, address="198.51.100.10", hostname="target", is_target=True),
            ]
        )
        worker._refresh_trace_result(metrics, hops, second, route_log)

    changes = route_changes_in_range(route_log_path, datetime(2026, 1, 1), datetime(2099, 1, 1))

    assert len(changes) == 1
    assert changes[0].changed_hops == (1,)
    assert "Hop 1" in changes[0].summary


def test_worker_continues_when_route_log_write_fails() -> None:
    _app()
    worker = MeasurementWorker("198.51.100.10", interval_seconds=0, max_cycles=1)
    route_log = _FailingRouteLog()
    errors: list[str] = []
    changes: list[object] = []
    worker.error_message.connect(errors.append)
    worker.route_changed.connect(changes.append)

    first: Future[list[HopInfo]] = Future()
    first.set_result(
        [
            HopInfo(index=1, address="192.0.2.1", hostname="gateway"),
            HopInfo(index=2, address="198.51.100.10", hostname="target", is_target=True),
        ]
    )
    metrics, hops = worker._refresh_trace_result(None, [], first, route_log, first_check=True)

    second: Future[list[HopInfo]] = Future()
    second.set_result(
        [
            HopInfo(index=1, address="192.0.2.254", hostname="backup"),
            HopInfo(index=2, address="198.51.100.10", hostname="target", is_target=True),
        ]
    )
    refreshed_metrics, refreshed_hops = worker._refresh_trace_result(metrics, hops, second, route_log)

    assert route_log.calls == 2
    assert refreshed_metrics is not None
    assert [hop.address for hop in refreshed_hops] == ["192.0.2.254", "198.51.100.10"]
    assert len(changes) == 1
    assert getattr(changes[0], "changed_hops") == (1,)
    assert errors == [
        "경로 로그 저장 중 오류가 발생했습니다. 측정은 계속 진행합니다. (ROUTE_LOG_WRITE_FAILED: PermissionError)"
    ]


def test_worker_reports_route_log_recovery_and_reports_a_later_failure_again() -> None:
    _app()
    worker = MeasurementWorker("198.51.100.10", interval_seconds=0, max_cycles=1)
    outcomes = [PermissionError("locked"), None, PermissionError("locked again")]
    errors: list[str] = []
    statuses: list[str] = []

    class IntermittentRouteLog:
        def write_snapshot(self, *_args) -> None:
            outcome = outcomes.pop(0)
            if outcome is not None:
                raise outcome

    worker.error_message.connect(errors.append)
    worker.status_message.connect(statuses.append)
    worker._route_history.record(
        [HopInfo(index=1, address="198.51.100.10", is_target=True)],
        datetime(2026, 1, 1, 12, 0, 0),
    )
    route_log = IntermittentRouteLog()

    worker._write_route_log_snapshot(route_log, None)
    worker._write_route_log_snapshot(route_log, None)
    worker._write_route_log_snapshot(route_log, None)

    assert len(errors) == 2
    assert all("ROUTE_LOG_WRITE_FAILED: PermissionError" in error for error in errors)
    assert statuses == ["경로 로그 저장이 정상화되었습니다."]


def test_worker_rejects_domain_without_dns_lookup() -> None:
    _app()

    worker = MeasurementWorker("missing.example", interval_seconds=0, max_cycles=1)
    errors: list[str] = []
    worker.error_message.connect(errors.append)

    worker.run()

    assert errors == ["IPv4 주소만 입력 가능합니다: missing.example"]


def test_worker_emits_trace_and_one_measurement_cycle(monkeypatch) -> None:
    _app()
    monkeypatch.setattr(
        worker_module,
        "run_traceroute",
        lambda target, timeout_ms, stop_event: [
            HopInfo(index=1, address="192.0.2.1", hostname="gateway"),
            HopInfo(index=2, address="198.51.100.10", hostname="target.example"),
        ],
    )
    monkeypatch.setattr(worker_module, "CommandPingRunner", _FakePingRunner)

    worker = MeasurementWorker("198.51.100.10", interval_seconds=0, max_cycles=1)
    traces: list[list[HopInfo]] = []
    updates: list[tuple[object, object, object, object, object, object]] = []
    statuses: list[str] = []
    worker.trace_completed.connect(traces.append)
    worker.measurement_updated.connect(lambda *args: updates.append(args))
    worker.status_message.connect(statuses.append)

    worker.run()

    assert len(traces) == 1
    assert [hop.index for hop in traces[0]] == [1, 2]
    assert len(updates) == 1
    snapshots = updates[0][0]
    target_snapshot = updates[0][1]
    target_snapshots = updates[0][2]
    observations = updates[0][4]
    assert len(snapshots) == 2
    assert len(target_snapshots) == 1
    assert target_snapshot.sent == 1
    assert len(observations) == 3
    assert statuses[-1] == "측정이 완료되었습니다."


def test_worker_uses_target_fallback_when_traceroute_returns_no_hops(monkeypatch) -> None:
    _app()
    monkeypatch.setattr(worker_module, "run_traceroute", lambda target, timeout_ms, stop_event: [])
    monkeypatch.setattr(worker_module, "CommandPingRunner", _FakePingRunner)

    worker = MeasurementWorker("203.0.113.10", interval_seconds=0, max_cycles=1)
    traces: list[list[HopInfo]] = []
    updates: list[tuple[object, object, object, object, object, object]] = []
    worker.trace_completed.connect(traces.append)
    worker.measurement_updated.connect(lambda *args: updates.append(args))

    worker.run()

    assert len(traces) == 1
    assert traces[0][0].is_target is True
    assert traces[0][0].address == "203.0.113.10"
    assert updates


def test_worker_emits_target_ping_before_slow_traceroute_finishes(monkeypatch) -> None:
    _app()
    monkeypatch.setattr(worker_module, "CommandPingRunner", _FakePingRunner)

    worker = MeasurementWorker(
        "198.51.100.10",
        interval_seconds=0,
        max_cycles=1,
        traceroute_probe=_DelayedTracerouteProbe(
            [
                HopInfo(index=1, address="192.0.2.1", hostname="gateway"),
                HopInfo(index=2, address="198.51.100.10", hostname="target.example"),
            ],
            delay_seconds=2.0,
        ),
    )
    traces: list[list[HopInfo]] = []
    updates: list[tuple[object, object, object, object, object, object]] = []
    worker.trace_completed.connect(traces.append)
    worker.measurement_updated.connect(lambda *args: updates.append(args))

    worker.run()

    assert updates[0][0] == []
    assert updates[0][1].sent >= 1
    assert traces == [] or traces[0]


def test_worker_final_hop_only_skips_traceroute_and_hop_pings() -> None:
    _app()
    traceroute_probe = _FakeTracerouteProbe(
        [
            HopInfo(index=1, address="192.0.2.1", hostname="gateway"),
            HopInfo(index=2, address="198.51.100.10", hostname="target.example"),
        ]
    )
    ping_calls: list[tuple[str, int]] = []

    worker = MeasurementWorker(
        "198.51.100.10",
        interval_seconds=0,
        max_cycles=1,
        targets=["198.51.100.10", "203.0.113.10"],
        ping_probe_factory=lambda timeout_ms: _RecordingPingRunner(timeout_ms, ping_calls),
        traceroute_probe=traceroute_probe,
        measurement_mode=MEASUREMENT_MODE_FINAL_HOP_ONLY,
    )
    traces: list[list[HopInfo]] = []
    changes: list[object] = []
    updates: list[tuple[object, object, object, object, object, object]] = []
    diagnostics: list[object] = []
    worker.trace_completed.connect(traces.append)
    worker.route_changed.connect(changes.append)
    worker.measurement_updated.connect(lambda *args: updates.append(args))
    worker.diagnostics_updated.connect(diagnostics.append)

    worker.run()

    assert traceroute_probe.calls == []
    assert traces == []
    assert changes == []
    assert len(updates) == 1
    assert updates[0][0] == []
    target_snapshots = {snapshot.address: snapshot for snapshot in updates[0][2]}
    assert set(target_snapshots) == {"198.51.100.10", "203.0.113.10"}
    assert all(snapshot.sent == 1 for snapshot in target_snapshots.values())
    assert {target for target, _timeout in ping_calls} == {"198.51.100.10", "203.0.113.10"}
    assert getattr(diagnostics[-1], "tracert_status") == "final hop only"


def test_worker_pause_targets_skips_scheduling_and_marks_snapshot() -> None:
    _app()
    ping_calls: list[tuple[str, int]] = []
    worker = MeasurementWorker(
        "198.51.100.10",
        interval_seconds=0,
        max_cycles=1,
        targets=["198.51.100.10", "203.0.113.10"],
        ping_probe_factory=lambda timeout_ms: _RecordingPingRunner(timeout_ms, ping_calls),
        measurement_mode=MEASUREMENT_MODE_FINAL_HOP_ONLY,
    )
    worker.pause_targets(["203.0.113.10"])
    updates: list[tuple[object, object, object, object, object, object]] = []
    diagnostics: list[object] = []
    worker.measurement_updated.connect(lambda *args: updates.append(args))
    worker.diagnostics_updated.connect(diagnostics.append)

    worker.run()

    target_snapshots = {snapshot.address: snapshot for snapshot in updates[0][2]}
    assert {target for target, _timeout in ping_calls} == {"198.51.100.10"}
    assert target_snapshots["203.0.113.10"].status == STATUS_PAUSED
    assert target_snapshots["203.0.113.10"].sent == 0
    assert getattr(diagnostics[-1], "paused_target_count") == 1


def test_worker_applies_target_specific_interval_override() -> None:
    _app()
    worker = MeasurementWorker(
        "198.51.100.10",
        interval_seconds=1,
        max_cycles=1,
        targets=["198.51.100.10", "203.0.113.10"],
        measurement_mode=MEASUREMENT_MODE_FINAL_HOP_ONLY,
    )

    worker.set_target_interval_seconds(["203.0.113.10"], 5)
    state = TargetProbeState("203.0.113.10", last_started_at=100.0)
    result = PingResult("203.0.113.10", True, 12.0, STATUS_OK, datetime.now())
    state.record_result(result, worker._target_base_interval_seconds("203.0.113.10"), now=101.0)

    assert worker.target_interval_overrides() == {"203.0.113.10": 5}
    assert worker._target_base_interval_seconds("198.51.100.10") == 1
    assert state.current_interval_seconds == 5
    assert state.next_due == 105.0

    worker.set_interval_seconds(2)

    assert worker.target_interval_overrides() == {}
    assert worker._target_base_interval_seconds("203.0.113.10") == 2


def test_worker_auto_promotes_final_hop_only_to_full_route_on_latency_alert() -> None:
    _app()
    traceroute_probe = _FakeTracerouteProbe(
        [
            HopInfo(index=1, address="192.0.2.1", hostname="gateway"),
            HopInfo(index=2, address="198.51.100.10", hostname="target.example"),
        ]
    )
    ping_calls: list[str] = []

    worker = MeasurementWorker(
        "198.51.100.10",
        interval_seconds=0,
        max_cycles=2,
        ping_probe_factory=lambda timeout_ms: _HighLatencyPingRunner(timeout_ms, ping_calls),
        traceroute_probe=traceroute_probe,
        measurement_mode=MEASUREMENT_MODE_FINAL_HOP_ONLY,
    )
    traces: list[list[HopInfo]] = []
    updates: list[tuple[object, object, object, object, object, object]] = []
    statuses: list[str] = []
    worker.trace_completed.connect(traces.append)
    worker.measurement_updated.connect(lambda *args: updates.append(args))
    worker.status_message.connect(statuses.append)

    worker.run()

    assert worker.measurement_mode == MEASUREMENT_MODE_FULL_ROUTE
    assert traceroute_probe.calls == [("198.51.100.10", 1000, False)]
    assert [hop.index for hop in traces[0]] == [1, 2]
    assert updates[-1][0]
    assert any("자동 전체 경로 전환" in status for status in statuses)
    assert "192.0.2.1" in ping_calls


def test_worker_route_adjustment_can_be_disabled() -> None:
    _app()
    traceroute_probe = _FakeTracerouteProbe(
        [
            HopInfo(index=1, address="192.0.2.1", hostname="gateway"),
            HopInfo(index=2, address="198.51.100.10", hostname="target.example"),
        ]
    )
    ping_calls: list[str] = []

    worker = MeasurementWorker(
        "198.51.100.10",
        interval_seconds=0,
        max_cycles=2,
        ping_probe_factory=lambda timeout_ms: _HighLatencyPingRunner(timeout_ms, ping_calls),
        traceroute_probe=traceroute_probe,
        measurement_mode=MEASUREMENT_MODE_FINAL_HOP_ONLY,
        auto_full_route_on_alert=False,
    )
    statuses: list[str] = []
    worker.status_message.connect(statuses.append)

    worker.run()

    assert worker.measurement_mode == MEASUREMENT_MODE_FINAL_HOP_ONLY
    assert traceroute_probe.calls == []
    assert not any("자동 전체 경로 전환" in status for status in statuses)


def test_worker_route_adjustment_uses_configured_alert_threshold() -> None:
    _app()
    traceroute_probe = _FakeTracerouteProbe(
        [
            HopInfo(index=1, address="192.0.2.1", hostname="gateway"),
            HopInfo(index=2, address="198.51.100.10", hostname="target.example"),
        ]
    )
    ping_calls: list[str] = []

    worker = MeasurementWorker(
        "198.51.100.10",
        interval_seconds=0,
        max_cycles=2,
        ping_probe_factory=lambda timeout_ms: _HighLatencyPingRunner(timeout_ms, ping_calls),
        traceroute_probe=traceroute_probe,
        measurement_mode=MEASUREMENT_MODE_FINAL_HOP_ONLY,
        alert_rule_config=AlertRuleConfig(latency_threshold_ms=500.0),
    )

    worker.run()

    assert worker.measurement_mode == MEASUREMENT_MODE_FINAL_HOP_ONLY
    assert traceroute_probe.calls == []


def test_worker_route_adjustment_can_restore_final_hop_on_recovery() -> None:
    _app()
    traceroute_probe = _FakeTracerouteProbe(
        [
            HopInfo(index=1, address="192.0.2.1", hostname="gateway"),
            HopInfo(index=2, address="198.51.100.10", hostname="target.example"),
        ]
    )
    ping_calls: dict[str, int] = {}

    worker = MeasurementWorker(
        "198.51.100.10",
        interval_seconds=0,
        max_cycles=3,
        ping_probe_factory=lambda timeout_ms: _RecoveringLatencyPingRunner(timeout_ms, ping_calls),
        traceroute_probe=traceroute_probe,
        measurement_mode=MEASUREMENT_MODE_FINAL_HOP_ONLY,
        auto_restore_final_hop_on_recovery=True,
    )
    statuses: list[str] = []
    worker.status_message.connect(statuses.append)

    worker.run()

    assert worker.measurement_mode == MEASUREMENT_MODE_FINAL_HOP_ONLY
    assert traceroute_probe.calls == [("198.51.100.10", 1000, False)]
    assert any("자동 전체 경로 전환" in status for status in statuses)
    assert any("자동 최종 IP만 복원" in status for status in statuses)


def test_worker_does_not_stack_duplicate_pings_for_slow_targets(monkeypatch) -> None:
    _app()
    monkeypatch.setattr(worker_module, "run_traceroute", lambda target, timeout_ms, stop_event: [])
    calls: dict[str, int] = {}

    worker = MeasurementWorker(
        "198.51.100.10",
        interval_seconds=1,
        max_cycles=2,
        targets=["198.51.100.10", "203.0.113.10"],
        ping_probe_factory=lambda timeout_ms: _SlowTargetPingRunner(timeout_ms, calls),
    )
    updates: list[tuple[object, object, object, object, object, object]] = []
    worker.measurement_updated.connect(lambda *args: updates.append(args))

    started_at = time.monotonic()
    worker.run()
    elapsed = time.monotonic() - started_at

    # This includes ThreadPoolExecutor cleanup for simulated 1.5s timeout probes.
    # The call-count assertions below catch duplicate stacking; keep this ceiling focused on gross stalls.
    assert elapsed < 6.0
    assert len(updates) == 2
    assert calls["198.51.100.10"] == 2
    assert calls["203.0.113.10"] == 1
    final_target_snapshots = {snapshot.address: snapshot for snapshot in updates[-1][2]}
    assert final_target_snapshots["198.51.100.10"].sent == 2
    assert final_target_snapshots["203.0.113.10"].sent == 1


def test_worker_reuses_ping_probe_per_executor_thread(monkeypatch) -> None:
    _app()
    monkeypatch.setattr(worker_module, "run_traceroute", lambda target, timeout_ms, stop_event: [])
    counters = {"instances": 0, "pings": 0, "closed": 0}

    worker = MeasurementWorker(
        "198.51.100.10",
        interval_seconds=0,
        max_cycles=3,
        ping_probe_factory=lambda timeout_ms: _CountingPingRunner(timeout_ms, counters),
    )

    worker.run()

    assert counters == {"instances": 1, "pings": 3, "closed": 1}


def test_probe_pool_closes_remaining_probes_after_close_error() -> None:
    events: list[str] = []
    pool = worker_module._ThreadLocalPingProbePool(lambda: object())
    with pool._lock:
        pool._probes.extend(
            [
                _CloseFailingProbe("first", events),
                _RecordingCloseProbe("second", events),
            ]
        )

    pool.close()
    pool.close()

    assert events == ["first", "second"]
    assert pool._probes == []


def test_worker_finishes_session_when_probe_close_fails(monkeypatch, tmp_path) -> None:
    _app()
    monkeypatch.setattr(worker_module, "run_traceroute", lambda target, timeout_ms, stop_event: [])
    worker = MeasurementWorker(
        "198.51.100.10",
        interval_seconds=0,
        max_cycles=1,
        measurement_mode=MEASUREMENT_MODE_FINAL_HOP_ONLY,
        session_log_root=tmp_path,
        ping_probe_factory=lambda timeout_ms: _CloseFailingProbeRunner(timeout_ms),
    )
    errors: list[str] = []
    worker.error_message.connect(errors.append)

    worker.run()

    sessions = SessionIndexStore.create(tmp_path).list_sessions(target="198.51.100.10")
    assert errors == []
    assert len(sessions) == 1
    assert sessions[0].state == SESSION_STATE_ARCHIVED


def test_worker_backs_off_repeated_timeout_targets(monkeypatch) -> None:
    _app()
    monkeypatch.setattr(worker_module, "run_traceroute", lambda target, timeout_ms, stop_event: [])
    calls: dict[str, int] = {}

    worker = MeasurementWorker(
        "198.51.100.1",
        interval_seconds=1,
        max_cycles=5,
        targets=["198.51.100.1", "198.51.100.2"],
        ping_probe_factory=lambda timeout_ms: _BackoffPingRunner(timeout_ms, calls),
    )
    updates: list[tuple[object, object, object, object, object, object]] = []
    diagnostics: list[object] = []
    worker.measurement_updated.connect(lambda *args: updates.append(args))
    worker.diagnostics_updated.connect(diagnostics.append)

    worker.run()

    assert len(updates) == 5
    assert calls["198.51.100.1"] >= calls["198.51.100.2"]
    assert calls["198.51.100.2"] < len(updates)
    assert any(getattr(item, "backoff_target_count", 0) >= 1 for item in diagnostics)


def test_target_probe_state_resets_backoff_after_recovery() -> None:
    state = TargetProbeState("198.51.100.2")
    failed = PingResult("198.51.100.2", False, None, STATUS_TIMEOUT, datetime.now())
    recovered = PingResult("198.51.100.2", True, 10.0, STATUS_OK, datetime.now())

    for index in range(10):
        state.last_scheduled_due = float(index)
        state.record_result(failed, base_interval_seconds=1, now=float(index))

    assert state.consecutive_failures == 10
    assert state.current_interval_seconds == 5.0

    state.record_result(recovered, base_interval_seconds=1, now=11.0)

    assert state.consecutive_failures == 0
    assert state.current_interval_seconds == 1
    assert state.next_due == 12.0


def test_target_probe_state_uses_due_time_instead_of_completion_time() -> None:
    state = TargetProbeState("198.51.100.10")
    result = PingResult("198.51.100.10", True, 10.0, STATUS_OK, datetime.now())

    assert state.is_due(1, now=100.0) is True
    assert state.mark_started(100.0) == 100.0
    state.record_result(result, base_interval_seconds=1, now=100.4)

    assert state.next_due == 101.0
    assert state.next_due != pytest.approx(101.4)


def test_periodic_trace_due_counts_t0_without_completion_drift() -> None:
    next_due = 60.0
    trace_calls = 1  # immediate t0 trace

    for second in range(14_400):
        if second < next_due:
            continue
        trace_calls += 1
        next_due = worker_module._advance_periodic_due(next_due, 60.0, float(second))

    assert trace_calls == 240
    assert next_due == 14_400.0


def test_scheduler_reserves_capacity_and_prioritizes_responsive_targets(monkeypatch) -> None:
    monkeypatch.setattr(worker_module, "MAX_TARGET_PING_WORKERS", 20)
    timeout_targets = [f"198.51.100.{index}" for index in range(1, 41)]
    responsive_targets = [f"198.51.100.{index}" for index in range(41, 51)]
    targets = timeout_targets + responsive_targets
    observed: list[tuple[str, float, float, float]] = []
    worker = MeasurementWorker(
        targets[0],
        interval_seconds=1,
        max_cycles=None,
        targets=targets,
        probe_schedule_observer=lambda *event: observed.append(event),
    )
    states: dict[str, TargetProbeState] = {}
    for target in targets:
        state = TargetProbeState(target)
        state.configured_interval_seconds = 1
        state.current_interval_seconds = 1
        state.next_due = 100.0 if target in timeout_targets else 101.0
        if target in timeout_targets:
            state.consecutive_failures = 3
            state.current_interval_seconds = 2
            state.last_status = STATUS_TIMEOUT
        else:
            state.last_status = STATUS_OK
        states[target] = state

    class PendingExecutor:
        def __init__(self) -> None:
            self.targets: list[str] = []

        def submit(self, _callback, target: str) -> Future[PingResult]:
            self.targets.append(target)
            return Future()

    executor = PendingExecutor()
    active_targets = set(timeout_targets[:15])
    futures: dict[Future[PingResult], TargetProbeState] = {}

    # Five slots stay unused while only backoff targets are due. They are then
    # immediately available for healthy/unknown targets at their next due slot.
    assert worker._schedule_target_pings(executor, futures, active_targets, states, now=100.0) == set()
    scheduled = worker._schedule_target_pings(executor, futures, active_targets, states, now=101.0)

    assert len(scheduled) == 5
    assert scheduled.issubset(responsive_targets)
    assert len(active_targets.intersection(timeout_targets)) == 15
    assert all(event[0] in responsive_targets for event in observed)


def test_scheduler_submits_ten_responsive_targets_in_one_due_wave(monkeypatch) -> None:
    monkeypatch.setattr(worker_module, "MAX_TARGET_PING_WORKERS", 25)
    timeout_targets = [f"198.51.100.{index}" for index in range(1, 41)]
    responsive_targets = [f"198.51.100.{index}" for index in range(41, 51)]
    targets = timeout_targets + responsive_targets
    worker = MeasurementWorker(
        targets[0],
        interval_seconds=1,
        max_cycles=None,
        targets=targets,
    )
    states: dict[str, TargetProbeState] = {}
    for target in targets:
        state = TargetProbeState(target)
        state.configured_interval_seconds = 1
        state.current_interval_seconds = 1
        state.next_due = 100.0
        if target in timeout_targets:
            state.consecutive_failures = 10
            state.current_interval_seconds = 5
            state.last_status = STATUS_TIMEOUT
        else:
            state.last_status = STATUS_OK
        states[target] = state

    class PendingExecutor:
        def __init__(self) -> None:
            self.targets: list[str] = []

        def submit(self, _callback, target: str) -> Future[PingResult]:
            self.targets.append(target)
            return Future()

    executor = PendingExecutor()
    active_targets = set(timeout_targets[:15])
    futures: dict[Future[PingResult], TargetProbeState] = {}

    scheduled = worker._schedule_target_pings(executor, futures, active_targets, states, now=100.0)

    assert scheduled == set(responsive_targets)
    assert executor.targets == responsive_targets


def test_measurement_scheduler_requests_high_thread_priority() -> None:
    class PriorityWorker(MeasurementWorker):
        def __init__(self) -> None:
            super().__init__("198.51.100.1", interval_seconds=1, max_cycles=1)
            self.requested_priority = None

        def isRunning(self) -> bool:
            return True

        def setPriority(self, priority) -> None:
            self.requested_priority = priority

    worker = PriorityWorker()

    worker._prefer_cadence_thread_priority()

    assert worker.requested_priority == worker_module.QThread.Priority.HighPriority


def test_scheduler_does_not_resubmit_interval_zero_target_within_same_round() -> None:
    target = "198.51.100.10"
    worker = MeasurementWorker(
        target,
        interval_seconds=0,
        max_cycles=1,
        targets=[target],
    )
    state = TargetProbeState(target)

    class PendingExecutor:
        def submit(self, _callback, _target: str) -> Future[PingResult]:
            return Future()

    executor = PendingExecutor()
    active_targets: set[str] = set()
    futures: dict[Future[PingResult], TargetProbeState] = {}

    scheduled = worker._schedule_target_pings(
        executor,
        futures,
        active_targets,
        {target: state},
        now=100.0,
    )
    assert scheduled == {target}

    # Simulate the fast future completing before the same round's inner refill.
    active_targets.clear()
    assert (
        worker._schedule_target_pings(
            executor,
            futures,
            active_targets,
            {target: state},
            now=100.1,
            excluded_targets=scheduled,
        )
        == set()
    )
    assert state.scheduled_count == 1


def test_target_probe_state_skips_missed_slots_without_burst() -> None:
    state = TargetProbeState("198.51.100.10")
    result = PingResult("198.51.100.10", True, 10.0, STATUS_OK, datetime.now())

    assert state.is_due(1, now=10.0) is True
    state.mark_started(10.0)
    state.record_result(result, base_interval_seconds=1, now=13.2)

    assert state.next_due == 14.0
    assert state.skipped_slot_count == 3
    assert state.is_due(1, now=13.2) is False
    assert state.is_due(1, now=14.0) is True


def test_target_probe_state_rebases_interval_change_from_scheduled_due() -> None:
    state = TargetProbeState("198.51.100.10")
    result = PingResult("198.51.100.10", True, 10.0, STATUS_OK, datetime.now())

    assert state.is_due(1, now=100.0) is True
    state.mark_started(100.0)
    state.record_result(result, base_interval_seconds=1, now=100.2)

    assert state.is_due(5, now=101.0) is False
    assert state.next_due == 105.0


def test_target_probe_state_pause_resume_catches_up_once() -> None:
    state = TargetProbeState("198.51.100.10")
    result = PingResult("198.51.100.10", True, 10.0, STATUS_OK, datetime.now())

    assert state.is_due(2, now=20.0) is True
    state.mark_started(20.0)
    state.record_result(result, base_interval_seconds=2, now=20.1)
    # The worker skips this state while paused. On resume, only one overdue probe
    # may be reserved; record_result advances over every missed slot.
    assert state.is_due(2, now=27.0) is True
    state.mark_started(27.0)
    state.record_result(result, base_interval_seconds=2, now=27.1)

    assert state.scheduled_count == 2
    assert state.skipped_slot_count == 2
    assert state.next_due == 28.0


def test_worker_keeps_twenty_targets_responsive_with_many_timeouts(monkeypatch) -> None:
    _app()
    monkeypatch.setattr(worker_module, "run_traceroute", lambda target, timeout_ms, stop_event: [])
    targets = [f"198.51.100.{index}" for index in range(1, 21)]
    calls: dict[str, int] = {}

    worker = MeasurementWorker(
        targets[0],
        interval_seconds=1,
        max_cycles=2,
        targets=targets,
        ping_probe_factory=lambda timeout_ms: _TwentyTargetPingRunner(timeout_ms, calls),
    )
    updates: list[tuple[object, object, object, object, object, object]] = []
    worker.measurement_updated.connect(lambda *args: updates.append(args))

    started_at = time.monotonic()
    worker.run()
    elapsed = time.monotonic() - started_at

    # The expected runtime is dominated by one 1.5s timeout wave plus executor cleanup.
    # Serial probing would take more than 20 seconds across these timeout targets.
    # Keep the ceiling low enough to catch that regression while allowing whole-suite Windows scheduler jitter.
    serial_timeout_floor = 15 * 1.5
    assert elapsed < serial_timeout_floor / 4
    assert len(updates) == 2
    final_target_snapshots = {snapshot.address: snapshot for snapshot in updates[-1][2]}
    assert len(final_target_snapshots) == 20
    assert final_target_snapshots["198.51.100.1"].sent == 2
    assert final_target_snapshots["198.51.100.20"].sent == 1
    assert calls["198.51.100.1"] == 2
    assert calls["198.51.100.20"] == 1


def test_worker_rotates_limited_probe_capacity_across_all_targets(monkeypatch, tmp_path) -> None:
    _app()
    monkeypatch.setattr(worker_module, "MAX_TARGET_PING_WORKERS", 2)
    targets = [f"198.51.100.{index}" for index in range(1, 6)]
    calls: list[tuple[str, int]] = []

    worker = MeasurementWorker(
        targets[0],
        interval_seconds=0,
        max_cycles=1,
        targets=targets,
        measurement_mode=MEASUREMENT_MODE_FINAL_HOP_ONLY,
        session_log_root=tmp_path,
        ping_probe_factory=lambda timeout_ms: _RecordingPingRunner(timeout_ms, calls),
    )

    worker.run()

    assert {target for target, _timeout_ms in calls} == set(targets)
    assert len(calls) == len(targets)


def test_worker_stop_request_before_run_does_not_emit_trace(monkeypatch) -> None:
    _app()
    monkeypatch.setattr(worker_module, "run_traceroute", lambda target, timeout_ms, stop_event: [])

    worker = MeasurementWorker("198.51.100.10", interval_seconds=0, max_cycles=1)
    traces: list[list[HopInfo]] = []
    statuses: list[str] = []
    worker.trace_completed.connect(traces.append)
    worker.status_message.connect(statuses.append)
    worker.request_stop()

    worker.run()

    assert traces == []
    assert statuses[-1] == "측정이 중지되었습니다."


def test_worker_does_not_sleep_after_final_cycle(monkeypatch) -> None:
    _app()
    monkeypatch.setattr(
        worker_module,
        "run_traceroute",
        lambda target, timeout_ms, stop_event: [HopInfo(index=1, address="198.51.100.10")],
    )
    monkeypatch.setattr(worker_module, "CommandPingRunner", _FakePingRunner)

    worker = MeasurementWorker("198.51.100.10", interval_seconds=5, max_cycles=1, timeout_ms=100)
    started_at = time.monotonic()
    worker.run()
    elapsed = time.monotonic() - started_at

    # This test guards against sleeping for the 5s interval after the final cycle.
    # The measured time also includes Windows thread scheduling and session-log cleanup.
    assert elapsed < 4.0


def test_worker_accumulates_multiple_measurement_cycles(monkeypatch) -> None:
    _app()
    monkeypatch.setattr(
        worker_module,
        "run_traceroute",
        lambda target, timeout_ms, stop_event: [
            HopInfo(index=1, address="192.0.2.1", hostname="gateway"),
            HopInfo(index=2, address="198.51.100.10", hostname="target.example"),
        ],
    )
    monkeypatch.setattr(worker_module, "CommandPingRunner", _FakePingRunner)

    worker = MeasurementWorker("198.51.100.10", interval_seconds=0, max_cycles=5)
    updates: list[tuple[object, object, object, object, object, object]] = []
    worker.measurement_updated.connect(lambda *args: updates.append(args))

    worker.run()

    assert len(updates) == 5
    snapshots = updates[-1][0]
    target_snapshot = updates[-1][1]
    observations = updates[-1][4]
    assert all(snapshot.sent == 5 for snapshot in snapshots)
    assert target_snapshot.sent == 5
    assert len(observations) == 15


def test_worker_fast_stability_run_accumulates_many_cycles(monkeypatch) -> None:
    _app()
    traceroute_probe = _FakeTracerouteProbe(
        [
            HopInfo(index=1, address="192.0.2.1", hostname="gateway"),
            HopInfo(index=2, address="198.51.100.10", hostname="target.example"),
        ]
    )
    pattern_calls: dict[str, int] = {}

    worker = MeasurementWorker(
        "198.51.100.10",
        interval_seconds=0,
        max_cycles=60,
        ping_probe_factory=lambda timeout_ms: _PatternPingRunner(timeout_ms, pattern_calls),
        traceroute_probe=traceroute_probe,
    )
    updates: list[tuple[object, object, object, object, object, object]] = []
    worker.measurement_updated.connect(lambda *args: updates.append(args))

    worker.run()

    assert len(updates) == 60
    snapshots = updates[-1][0]
    target_snapshot = updates[-1][1]
    observations = updates[-1][4]
    assert all(snapshot.sent == 60 for snapshot in snapshots)
    assert target_snapshot.sent == 60
    assert len(observations) == 180


def test_worker_keeps_recent_observations_and_streams_full_session_log(monkeypatch, tmp_path) -> None:
    _app()
    monkeypatch.setattr(
        worker_module,
        "run_traceroute",
        lambda target, timeout_ms, stop_event: [
            HopInfo(index=1, address="192.0.2.1", hostname="gateway"),
            HopInfo(index=2, address="198.51.100.10", hostname="target.example"),
        ],
    )
    monkeypatch.setattr(worker_module, "CommandPingRunner", _FakePingRunner)

    log_path = tmp_path / "session_samples.csv"

    def create_log(cls, target: str, root=None):
        return cls(log_path)

    monkeypatch.setattr(worker_module.SessionLogWriter, "create", classmethod(create_log))

    worker = MeasurementWorker("198.51.100.10", interval_seconds=0, max_cycles=120)
    updates: list[tuple[object, object, object, object, object, object]] = []
    log_paths: list[str] = []
    worker.measurement_updated.connect(lambda *args: updates.append(args))
    worker.session_log_ready.connect(log_paths.append)

    worker.run()

    assert log_paths == [str(log_path)]
    assert len(updates[-1][4]) == RECENT_OBSERVATION_LIMIT
    assert _count_csv_rows(log_path) == 360
    sessions = SessionIndexStore.create(tmp_path).list_sessions(target="198.51.100.10")
    assert len(sessions) == 1
    assert sessions[0].sample_path == log_path
    assert sessions[0].samples == 360
    assert sessions[0].state == SESSION_STATE_ARCHIVED


def test_worker_can_write_session_logs_under_custom_root(monkeypatch, tmp_path) -> None:
    _app()
    monkeypatch.setattr(worker_module, "run_traceroute", lambda target, timeout_ms, stop_event: [])
    monkeypatch.setattr(worker_module, "CommandPingRunner", _FakePingRunner)
    log_root = tmp_path / "soak_session_logs"

    worker = MeasurementWorker(
        "198.51.100.10",
        interval_seconds=0,
        max_cycles=2,
        session_log_root=log_root,
    )
    worker.resumed_from_session_id = "previous-session"
    log_paths: list[str] = []
    worker.session_log_ready.connect(log_paths.append)

    worker.run()

    assert len(log_paths) == 1
    log_path = Path(log_paths[0])
    assert log_path.exists()
    assert log_path.is_relative_to(log_root)
    sessions = SessionIndexStore.create(log_root).list_sessions(target="198.51.100.10")
    assert len(sessions) == 1
    assert sessions[0].sample_path == log_path
    assert sessions[0].samples >= 2
    assert sessions[0].state == SESSION_STATE_ARCHIVED
    assert sessions[0].resumed_from_session_id == "previous-session"


def test_worker_marks_session_paused_when_session_log_write_fails(monkeypatch, tmp_path) -> None:
    _app()
    monkeypatch.setattr(worker_module, "run_traceroute", lambda target, timeout_ms, stop_event: [])
    monkeypatch.setattr(worker_module, "CommandPingRunner", _FakePingRunner)
    log_path = tmp_path / "failed.samples.csv"

    def create_log(cls, target: str, root=None):
        return _FailingSessionLogWriter(log_path)

    monkeypatch.setattr(worker_module.SessionLogWriter, "create", classmethod(create_log))

    worker = MeasurementWorker("198.51.100.10", interval_seconds=0, max_cycles=1)
    errors: list[str] = []
    worker.error_message.connect(errors.append)

    worker.run()

    sessions = SessionIndexStore.create(tmp_path).list_sessions(target="198.51.100.10")
    assert len(sessions) == 1
    assert sessions[0].state == SESSION_STATE_PAUSED
    assert "SESSION_LOG_WRITE_FAILED: RuntimeError" in sessions[0].last_error
    assert any("SESSION_LOG_WRITE_FAILED" in error for error in errors)


def test_async_session_log_writer_rejects_new_writes_after_background_failure(tmp_path) -> None:
    writer = _SignalFailingSessionLogWriter(tmp_path / "async_failed.samples.csv")
    session_log = worker_module._AsyncSessionLogWriter(writer)
    observation = HopObservation(
        datetime.now(),
        1,
        "198.51.100.10",
        None,
        True,
        10.0,
        STATUS_OK,
        is_target=True,
    )

    session_log.write_many([observation])

    assert writer.failed.wait(1.0)
    deadline = time.monotonic() + 1.0
    while session_log._current_error() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert isinstance(session_log._current_error(), RuntimeError)
    with pytest.raises(worker_module._SessionLogWriterFailed) as raised:
        session_log.write_many([observation])
    assert isinstance(raised.value.original, RuntimeError)
    assert session_log.queue_depth == 0
    with pytest.raises(RuntimeError):
        session_log.close()


def test_async_session_log_writer_retries_nonfatal_index_checkpoint(tmp_path) -> None:
    writer = SessionLogWriter(tmp_path / "checkpoint_retry.samples.csv")
    attempts: list[int] = []
    first_attempt = threading.Event()

    def checkpoint(count: int, _timestamp: datetime, _segments: list[object]) -> None:
        attempts.append(count)
        if len(attempts) == 1:
            first_attempt.set()
            raise PermissionError("index temporarily locked")

    session_log = worker_module._AsyncSessionLogWriter(writer, checkpoint)
    first = HopObservation(
        datetime(2026, 1, 1, 12, 0, 0),
        0,
        "198.51.100.10",
        "Target",
        True,
        10.0,
        STATUS_OK,
        is_target=True,
    )
    second = HopObservation(
        datetime(2026, 1, 1, 12, 0, 1),
        0,
        "198.51.100.10",
        "Target",
        True,
        11.0,
        STATUS_OK,
        is_target=True,
    )

    session_log.write_many([first])
    assert first_attempt.wait(1.0)
    session_log.write_many([second])
    session_log.close()

    assert attempts[0] == 1
    assert attempts[-1] == 2
    assert session_log.samples_written == 2
    assert session_log.metadata_error is None
    assert len(read_observations(writer.path)) == 2


def test_async_session_log_writer_preserves_csv_when_index_checkpoint_keeps_failing(tmp_path) -> None:
    writer = SessionLogWriter(tmp_path / "checkpoint_failed.samples.csv")
    attempts: list[int] = []

    def checkpoint(count: int, _timestamp: datetime, _segments: list[object]) -> None:
        attempts.append(count)
        raise PermissionError("index locked")

    session_log = worker_module._AsyncSessionLogWriter(writer, checkpoint)
    observations = [
        HopObservation(
            datetime(2026, 1, 1, 12, 0, second),
            0,
            "198.51.100.10",
            "Target",
            True,
            10.0 + second,
            STATUS_OK,
            is_target=True,
        )
        for second in range(2)
    ]

    session_log.write_many(observations)
    session_log.close()

    assert attempts
    assert session_log.samples_written == 2
    assert isinstance(session_log.metadata_error, PermissionError)
    assert len(read_observations(writer.path)) == 2


def test_async_session_log_writer_coalesces_rapid_batches(tmp_path) -> None:
    class CountingWriter:
        def __init__(self) -> None:
            self.path = tmp_path / "coalesced.samples.csv"
            self.paths = [self.path]
            self.write_calls: list[list[HopObservation]] = []

        def write_many(self, observations) -> None:
            self.write_calls.append(list(observations))

        def close(self) -> None:
            return

    writer = CountingWriter()
    session_log = worker_module._AsyncSessionLogWriter(writer)
    for second in range(20):
        session_log.write_many(
            [
                HopObservation(
                    datetime(2026, 1, 1, 12, 0, second),
                    0,
                    "198.51.100.10",
                    "Target",
                    True,
                    10.0,
                    STATUS_OK,
                    is_target=True,
                )
            ]
        )

    session_log.close()

    assert sum(len(batch) for batch in writer.write_calls) == 20
    assert len(writer.write_calls) < 20


def test_async_session_log_writer_stops_measurement_instead_of_growing_queue_without_bound(tmp_path) -> None:
    started = threading.Event()
    release = threading.Event()

    class SlowSessionLogWriter:
        def __init__(self) -> None:
            self.path = tmp_path / "slow.samples.csv"
            self.paths = [self.path]
            self.written: list[HopObservation] = []

        def write_many(self, observations) -> None:
            started.set()
            assert release.wait(2.0)
            self.written.extend(observations)

        def close(self) -> None:
            return

    observation = HopObservation(
        datetime.now(),
        0,
        "198.51.100.10",
        None,
        True,
        10.0,
        STATUS_OK,
        is_target=True,
    )
    writer = SlowSessionLogWriter()
    session_log = worker_module._AsyncSessionLogWriter(writer, max_queue_batches=1)
    session_log.write_many([observation])
    assert started.wait(1.0)
    session_log.write_many([observation])

    with pytest.raises(worker_module._SessionLogWriterFailed) as exc_info:
        session_log.write_many([observation])

    assert isinstance(exc_info.value.original, worker_module.SessionLogBackpressureError)
    assert session_log.queue_depth == 1
    assert worker_module._session_log_error_summary(exc_info.value) == "SESSION_LOG_BACKPRESSURE: queue_full"
    release.set()
    with pytest.raises(worker_module.SessionLogBackpressureError):
        session_log.close()
    assert len(writer.written) == 2


def test_session_log_error_summary_unwraps_background_writer_failure() -> None:
    original = RuntimeError("simulated async write failure")
    wrapped = worker_module._SessionLogWriterFailed(original)

    assert worker_module._session_log_error_summary(wrapped) == "SESSION_LOG_WRITE_FAILED: RuntimeError"


def test_worker_marks_session_paused_when_session_log_close_fails(monkeypatch, tmp_path) -> None:
    _app()
    monkeypatch.setattr(worker_module, "run_traceroute", lambda target, timeout_ms, stop_event: [])
    monkeypatch.setattr(worker_module, "CommandPingRunner", _FakePingRunner)
    log_path = tmp_path / "close_failed.samples.csv"

    def create_log(cls, target: str, root=None):
        return _CloseFailingSessionLogWriter(log_path)

    monkeypatch.setattr(worker_module.SessionLogWriter, "create", classmethod(create_log))

    worker = MeasurementWorker("198.51.100.10", interval_seconds=0, max_cycles=1)
    errors: list[str] = []
    worker.error_message.connect(errors.append)

    worker.run()

    sessions = SessionIndexStore.create(tmp_path).list_sessions(target="198.51.100.10")
    assert len(sessions) == 1
    assert sessions[0].state == SESSION_STATE_PAUSED
    assert sessions[0].samples >= 1
    assert "SESSION_LOG_WRITE_FAILED: PermissionError" in sessions[0].last_error
    assert any("SESSION_LOG_WRITE_FAILED" in error for error in errors)


def test_worker_marks_session_paused_when_session_log_close_hangs(monkeypatch, tmp_path) -> None:
    _app()
    monkeypatch.setattr(worker_module, "run_traceroute", lambda target, timeout_ms, stop_event: [])
    monkeypatch.setattr(worker_module, "CommandPingRunner", _FakePingRunner)
    monkeypatch.setattr(worker_module, "SESSION_LOG_CLOSE_TIMEOUT_SECONDS", 0.05)
    log_path = tmp_path / "close_hung.samples.csv"
    release_close = threading.Event()

    def create_log(cls, target: str, root=None):
        return _BlockingCloseSessionLogWriter(log_path, release_close)

    monkeypatch.setattr(worker_module.SessionLogWriter, "create", classmethod(create_log))

    worker = MeasurementWorker("198.51.100.10", interval_seconds=0, max_cycles=1)
    errors: list[str] = []
    worker.error_message.connect(errors.append)

    try:
        worker.run()
    finally:
        release_close.set()

    sessions = SessionIndexStore.create(tmp_path).list_sessions(target="198.51.100.10")
    assert len(sessions) == 1
    assert sessions[0].state == SESSION_STATE_PAUSED
    assert "SESSION_LOG_WRITE_FAILED: TimeoutError" in sessions[0].last_error
    assert any("SESSION_LOG_WRITE_FAILED" in error for error in errors)


def test_worker_keeps_measurement_archived_when_rebuildable_segment_index_fails(
    monkeypatch,
    tmp_path,
) -> None:
    _app()
    monkeypatch.setattr(worker_module, "run_traceroute", lambda target, timeout_ms, stop_event: [])
    monkeypatch.setattr(worker_module, "CommandPingRunner", _FakePingRunner)
    monkeypatch.setattr(session_log_module, "SEGMENT_INDEX_IO_RETRY_DELAY_SECONDS", 0)
    log_path = tmp_path / "segment_index_failed.samples.csv"

    def create_log(cls, target: str, root=None):
        return SessionLogWriter(log_path)

    def locked_replace(_source, _target):
        raise PermissionError("segment index locked")

    monkeypatch.setattr(worker_module.SessionLogWriter, "create", classmethod(create_log))
    monkeypatch.setattr(session_log_module, "_replace_path", locked_replace)

    worker = MeasurementWorker("198.51.100.10", interval_seconds=0, max_cycles=1)
    errors: list[str] = []
    worker.error_message.connect(errors.append)

    worker.run()

    sessions = SessionIndexStore.create(tmp_path).list_sessions(target="198.51.100.10")
    observations = read_observations(log_path)
    assert len(sessions) == 1
    assert sessions[0].state == SESSION_STATE_ARCHIVED
    assert sessions[0].last_error == ""
    assert observations
    assert sessions[0].samples == len(observations)
    assert errors == []


def test_worker_reports_session_index_finish_failure_without_crashing(monkeypatch, tmp_path) -> None:
    _app()
    monkeypatch.setattr(worker_module, "run_traceroute", lambda target, timeout_ms, stop_event: [])
    monkeypatch.setattr(worker_module, "CommandPingRunner", _FakePingRunner)
    log_path = tmp_path / "index_finish_failed.samples.csv"

    def create_log(cls, target: str, root=None):
        return cls(log_path)

    def fail_finish(self, session_id: str, **kwargs) -> None:
        raise PermissionError("index locked during finish")

    monkeypatch.setattr(worker_module.SessionLogWriter, "create", classmethod(create_log))
    monkeypatch.setattr(SessionIndexStore, "finish_session", fail_finish)

    worker = MeasurementWorker("198.51.100.10", interval_seconds=0, max_cycles=1)
    errors: list[str] = []
    worker.error_message.connect(errors.append)

    worker.run()

    assert any("SESSION_INDEX_FINISH_FAILED: PermissionError" in error for error in errors)


def test_worker_recovers_exact_session_count_when_index_checkpoints_fail(monkeypatch, tmp_path) -> None:
    _app()
    monkeypatch.setattr(worker_module, "run_traceroute", lambda target, timeout_ms, stop_event: [])
    monkeypatch.setattr(worker_module, "CommandPingRunner", _FakePingRunner)

    def fail_checkpoint(self, session_id: str, count: int, last_timestamp: datetime, **kwargs) -> None:
        raise PermissionError("index temporarily locked")

    monkeypatch.setattr(SessionIndexStore, "add_samples", fail_checkpoint)
    worker = MeasurementWorker(
        "198.51.100.10",
        interval_seconds=0,
        max_cycles=2,
        session_log_root=tmp_path,
    )
    errors: list[str] = []
    worker.error_message.connect(errors.append)

    worker.run()

    sessions = SessionIndexStore.create(tmp_path).list_sessions(target="198.51.100.10")
    assert len(sessions) == 1
    assert sessions[0].state == SESSION_STATE_ARCHIVED
    assert sessions[0].samples == len(read_observations(sessions[0].sample_path))
    assert sessions[0].samples >= 2
    assert not any("SESSION_LOG_WRITE_FAILED" in error for error in errors)


def test_worker_thread_start_stop_repeats_without_lingering(monkeypatch) -> None:
    _app()

    for _ in range(3):
        worker = MeasurementWorker(
            "198.51.100.10",
            interval_seconds=1,
            max_cycles=None,
            traceroute_probe=_BlockingTracerouteProbe(),
        )

        worker.start()
        time.sleep(0.05)
        worker.request_stop()

        assert worker.wait(3000) is True
        assert worker.isRunning() is False


def test_worker_stop_request_with_slow_active_pings_finishes_without_deadlock() -> None:
    _app()
    targets = [f"198.51.100.{index}" for index in range(1, 9)]
    calls: list[str] = []

    worker = MeasurementWorker(
        targets[0],
        interval_seconds=1,
        max_cycles=None,
        targets=targets,
        measurement_mode=MEASUREMENT_MODE_FINAL_HOP_ONLY,
        ping_probe_factory=lambda timeout_ms: _StopDelayPingRunner(timeout_ms, calls, delay_seconds=0.25),
    )

    worker.start()
    deadline = time.monotonic() + 2.0
    while not calls and time.monotonic() < deadline:
        time.sleep(0.01)

    stopped_at = time.monotonic()
    worker.request_stop()

    assert worker.wait(3000) is True
    assert time.monotonic() - stopped_at < 3.0
    assert calls
    assert worker.isRunning() is False


def test_worker_stop_request_is_bounded_by_active_probe_timeout() -> None:
    _app()
    targets = [f"198.51.100.{index}" for index in range(1, 21)]
    calls: list[tuple[str, int]] = []

    worker = MeasurementWorker(
        targets[0],
        interval_seconds=1,
        max_cycles=None,
        timeout_ms=120,
        targets=targets,
        measurement_mode=MEASUREMENT_MODE_FINAL_HOP_ONLY,
        ping_probe_factory=lambda timeout_ms: _TimeoutBoundPingRunner(timeout_ms, calls),
    )

    worker.start()
    deadline = time.monotonic() + 2.0
    while not calls and time.monotonic() < deadline:
        time.sleep(0.01)

    stopped_at = time.monotonic()
    worker.request_stop()

    assert worker.wait(2500) is True
    assert time.monotonic() - stopped_at < 2.5
    assert calls
    assert {timeout_ms for _target, timeout_ms in calls} == {120}
    assert worker.isRunning() is False


def test_worker_drains_active_ping_results_before_closing_session_log(tmp_path) -> None:
    _app()
    target_count = 6
    targets = [f"198.51.100.{index}" for index in range(1, target_count + 1)]
    calls: list[str] = []
    calls_lock = threading.Lock()
    all_started = threading.Event()
    release_pings = threading.Event()

    worker = MeasurementWorker(
        targets[0],
        interval_seconds=1,
        max_cycles=None,
        timeout_ms=1000,
        targets=targets,
        measurement_mode=MEASUREMENT_MODE_FINAL_HOP_ONLY,
        ping_probe_factory=lambda timeout_ms: _GatedPingRunner(
            timeout_ms,
            calls,
            target_count=target_count,
            all_started=all_started,
            release=release_pings,
            lock=calls_lock,
        ),
        session_log_root=tmp_path,
    )

    worker.start()
    assert all_started.wait(2.0)
    worker.request_stop()
    release_pings.set()

    assert worker.wait(3000) is True
    sessions = SessionIndexStore.create(tmp_path).list_sessions(recover_missing=True)
    assert len(sessions) == 1
    observations = read_observations(sessions[0].sample_path)
    assert len(observations) == len(calls) == target_count
    assert {observation.address for observation in observations} == set(targets)


def test_worker_cancels_pending_measurement_futures_on_shutdown() -> None:
    trace_future: Future[list[HopInfo]] = Future()
    target_future: Future[PingResult] = Future()
    hop_future: Future[PingResult] = Future()

    MeasurementWorker._cancel_pending_futures(
        trace_future,
        {target_future: "198.51.100.10"},
        {hop_future: "192.0.2.1"},
    )

    assert trace_future.cancelled() is True
    assert target_future.cancelled() is True
    assert hop_future.cancelled() is True


def test_worker_applies_runtime_add_and_remove_requests() -> None:
    _app()
    worker = MeasurementWorker(
        "198.51.100.10",
        interval_seconds=1,
        max_cycles=None,
        targets=["198.51.100.10"],
    )
    target_trackers = {
        "198.51.100.10": TargetMetricTracker(
            "198.51.100.10",
            recent_observation_limit=RECENT_OBSERVATION_LIMIT,
        )
    }
    target_states = {"198.51.100.10": TargetProbeState("198.51.100.10")}
    active_target_pings = {"198.51.100.10"}

    assert worker.add_targets(["203.0.113.10", "invalid.example", "198.51.100.10"]) == ["203.0.113.10"]
    assert worker.add_targets(["203.0.113.10"]) == []
    assert worker._apply_pending_target_changes(target_trackers, target_states, active_target_pings) is False

    assert worker.targets == ["198.51.100.10", "203.0.113.10"]
    assert set(target_trackers) == {"198.51.100.10", "203.0.113.10"}
    assert set(target_states) == {"198.51.100.10", "203.0.113.10"}

    worker.pause_targets(["198.51.100.10"])
    worker.set_target_interval_seconds(["198.51.100.10"], 5)
    assert worker.paused_targets() == {"198.51.100.10"}
    assert worker.target_interval_overrides() == {"198.51.100.10": 5}

    assert worker.remove_targets(["198.51.100.10"]) == ["198.51.100.10"]
    assert worker._apply_pending_target_changes(target_trackers, target_states, active_target_pings) is True

    assert worker.target == "203.0.113.10"
    assert worker.targets == ["203.0.113.10"]
    assert set(target_trackers) == {"203.0.113.10"}
    assert set(target_states) == {"203.0.113.10"}
    assert active_target_pings == {"198.51.100.10"}
    assert worker.paused_targets() == set()
    assert worker.target_interval_overrides() == {}


def test_worker_discards_in_flight_result_from_removed_and_readded_target() -> None:
    _app()
    target = "198.51.100.10"
    worker = MeasurementWorker(target, interval_seconds=1, max_cycles=None, targets=[target])
    target_trackers = {target: TargetMetricTracker(target, recent_observation_limit=RECENT_OBSERVATION_LIMIT)}
    old_state = TargetProbeState(target)
    target_states = {target: old_state}
    active_target_pings = {target}
    old_future: Future[PingResult] = Future()
    old_future.set_result(PingResult(target, False, None, STATUS_TIMEOUT, datetime.now()))

    assert worker.remove_targets([target]) == [target]
    assert worker._apply_pending_target_changes(target_trackers, target_states, active_target_pings) is True
    assert worker.add_targets([target]) == [target]
    assert worker._apply_pending_target_changes(target_trackers, target_states, active_target_pings) is True
    assert target_states[target] is not old_state
    assert active_target_pings == {target}

    class CapturingSessionLog:
        def __init__(self) -> None:
            self.observations: list[HopObservation] = []

        def write_many(self, observations) -> None:
            self.observations.extend(observations)

    session_log = CapturingSessionLog()
    recent_observations: deque[HopObservation] = deque()
    worker._collect_completed_ping_results(
        {old_future: old_state},
        active_target_pings,
        {},
        set(),
        None,
        [],
        target_trackers,
        target_states,
        session_log,
        recent_observations,
        timeout=0,
    )

    assert target_trackers[target].snapshot().sent == 0
    assert active_target_pings == set()
    assert session_log.observations == []
    assert list(recent_observations) == []


def test_worker_can_cancel_pending_runtime_addition_before_apply() -> None:
    _app()
    worker = MeasurementWorker(
        "198.51.100.10",
        interval_seconds=1,
        max_cycles=None,
        targets=["198.51.100.10"],
    )
    target_trackers = {
        "198.51.100.10": TargetMetricTracker(
            "198.51.100.10",
            recent_observation_limit=RECENT_OBSERVATION_LIMIT,
        )
    }
    target_states = {"198.51.100.10": TargetProbeState("198.51.100.10")}
    active_target_pings: set[str] = set()

    assert worker.add_targets(["203.0.113.10"]) == ["203.0.113.10"]
    assert worker.remove_targets(["203.0.113.10"]) == []
    assert worker._apply_pending_target_changes(target_trackers, target_states, active_target_pings) is False

    assert worker.targets == ["198.51.100.10"]
    assert set(target_trackers) == {"198.51.100.10"}
    assert set(target_states) == {"198.51.100.10"}


def test_worker_records_ping_exceptions_as_error_results(monkeypatch) -> None:
    _app()
    worker = MeasurementWorker("198.51.100.10", interval_seconds=0, max_cycles=1)
    monkeypatch.setattr(worker_module, "CommandPingRunner", _FailingPingRunner)

    results = worker._ping_unique_targets([HopInfo(index=1, address="192.0.2.1")])

    assert results["192.0.2.1"].status == STATUS_ERROR
    assert results["198.51.100.10"].status == STATUS_ERROR


def test_worker_uses_injected_probe_boundaries(monkeypatch) -> None:
    _app()
    traceroute_probe = _FakeTracerouteProbe(
        [
            HopInfo(index=1, address="192.0.2.1", hostname="gateway"),
            HopInfo(index=2, address="198.51.100.10", hostname="target.example"),
        ]
    )
    ping_calls: list[tuple[str, int]] = []

    worker = MeasurementWorker(
        "198.51.100.10",
        interval_seconds=0,
        max_cycles=1,
        timeout_ms=750,
        ping_probe_factory=lambda timeout_ms: _RecordingPingRunner(timeout_ms, ping_calls),
        traceroute_probe=traceroute_probe,
    )
    traces: list[list[HopInfo]] = []
    updates: list[tuple[object, object, object, object, object, object]] = []
    worker.trace_completed.connect(traces.append)
    worker.measurement_updated.connect(lambda *args: updates.append(args))

    worker.run()

    assert traceroute_probe.calls == [("198.51.100.10", 750, False)]
    assert [hop.index for hop in traces[0]] == [1, 2]
    assert {target for target, _timeout in ping_calls} == {
        "192.0.2.1",
        "198.51.100.10",
    }
    assert {timeout for _target, timeout in ping_calls} == {750}
    assert updates


def test_worker_uses_tcp_connect_probe_engine(monkeypatch) -> None:
    _app()
    calls: list[tuple[object, ...]] = []

    class RecordingTcpConnectRunner:
        def __init__(self, timeout_ms: int, port: int) -> None:
            calls.append(("init", timeout_ms, port))

        def ping(self, target: str) -> PingResult:
            calls.append(("ping", target))
            return PingResult(target, True, 14.0, STATUS_OK, datetime.now())

        def close(self) -> None:
            calls.append(("close",))

    monkeypatch.setattr(worker_module, "TcpConnectRunner", RecordingTcpConnectRunner)

    worker = MeasurementWorker(
        "198.51.100.10",
        interval_seconds=0,
        max_cycles=1,
        timeout_ms=650,
        measurement_mode=MEASUREMENT_MODE_FINAL_HOP_ONLY,
        probe_engine=PROBE_ENGINE_TCP_CONNECT,
        tcp_port=8443,
    )
    updates: list[tuple[object, object, object, object, object, object]] = []
    diagnostics: list[object] = []
    worker.measurement_updated.connect(lambda *args: updates.append(args))
    worker.diagnostics_updated.connect(diagnostics.append)

    worker.run()

    assert ("init", 650, 8443) in calls
    assert ("ping", "198.51.100.10") in calls
    assert ("close",) in calls
    assert updates[0][1].status == STATUS_OK
    assert diagnostics
    assert getattr(diagnostics[-1], "target_probe_engine") == "TCP Connect:8443"
    assert getattr(diagnostics[-1], "route_probe_engine") == "disabled"
    assert getattr(diagnostics[-1], "tcp_port") == 8443


def test_worker_uses_icmp_for_full_route_hops_with_tcp_target_engine(monkeypatch) -> None:
    _app()
    calls: list[tuple[object, ...]] = []

    class RecordingTcpConnectRunner:
        def __init__(self, timeout_ms: int, port: int) -> None:
            calls.append(("tcp_init", timeout_ms, port))

        def ping(self, target: str) -> PingResult:
            calls.append(("tcp_ping", target))
            return PingResult(target, True, 14.0, STATUS_OK, datetime.now())

        def close(self) -> None:
            calls.append(("tcp_close",))

    class RecordingCommandPingRunner:
        def __init__(self, timeout_ms: int) -> None:
            calls.append(("icmp_init", timeout_ms))

        def ping(self, target: str) -> PingResult:
            calls.append(("icmp_ping", target))
            return PingResult(target, True, 2.0, STATUS_OK, datetime.now())

        def close(self) -> None:
            calls.append(("icmp_close",))

    monkeypatch.setattr(worker_module, "TcpConnectRunner", RecordingTcpConnectRunner)
    monkeypatch.setattr(worker_module, "CommandPingRunner", RecordingCommandPingRunner)
    traceroute_probe = _FakeTracerouteProbe(
        [
            HopInfo(index=1, address="192.0.2.1", hostname="gateway"),
            HopInfo(index=2, address="198.51.100.10", hostname="target.example"),
        ]
    )
    worker = MeasurementWorker(
        "198.51.100.10",
        interval_seconds=0,
        max_cycles=1,
        timeout_ms=650,
        traceroute_probe=traceroute_probe,
        measurement_mode=MEASUREMENT_MODE_FULL_ROUTE,
        probe_engine=PROBE_ENGINE_TCP_CONNECT,
        tcp_port=8443,
    )
    diagnostics: list[object] = []
    worker.diagnostics_updated.connect(diagnostics.append)

    worker.run()

    assert ("tcp_ping", "198.51.100.10") in calls
    assert ("icmp_ping", "192.0.2.1") in calls
    assert ("tcp_ping", "192.0.2.1") not in calls
    assert ("icmp_ping", "198.51.100.10") not in calls
    assert diagnostics
    assert getattr(diagnostics[-1], "target_probe_engine") == "TCP Connect:8443"
    assert getattr(diagnostics[-1], "route_probe_engine") == "tracert/ICMP"


class _FakePingRunner:
    def __init__(self, timeout_ms: int) -> None:
        self.timeout_ms = timeout_ms

    def ping(self, target: str) -> PingResult:
        latency = {
            "192.0.2.1": 2.0,
            "198.51.100.10": 20.0,
            "203.0.113.10": 30.0,
        }.get(target, 10.0)
        return PingResult(target, True, latency, STATUS_OK, datetime.now())


class _FailingSessionLogWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.paths = [path]

    def write_many(self, _observations) -> None:
        raise RuntimeError("simulated write failure")

    def close(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)


class _SignalFailingSessionLogWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.paths = [path]
        self.failed = threading.Event()

    def write_many(self, _observations) -> None:
        self.failed.set()
        raise RuntimeError("simulated async write failure")

    def close(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)


class _CloseFailingSessionLogWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.paths = [path]

    def write_many(self, _observations) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch()

    def close(self) -> None:
        raise PermissionError("locked during close")


class _BlockingCloseSessionLogWriter:
    def __init__(self, path: Path, release_event: threading.Event) -> None:
        self.path = path
        self.paths = [path]
        self.release_event = release_event

    def write_many(self, _observations) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch()

    def close(self) -> None:
        self.release_event.wait(5.0)


class _SlowTargetPingRunner:
    def __init__(self, timeout_ms: int, calls: dict[str, int]) -> None:
        self.timeout_ms = timeout_ms
        self.calls = calls

    def ping(self, target: str) -> PingResult:
        self.calls[target] = self.calls.get(target, 0) + 1
        if target == "203.0.113.10":
            time.sleep(1.5)
            return PingResult(target, False, None, STATUS_TIMEOUT, datetime.now())
        return PingResult(target, True, 20.0, STATUS_OK, datetime.now())


class _StopDelayPingRunner:
    def __init__(self, timeout_ms: int, calls: list[str], *, delay_seconds: float) -> None:
        self.timeout_ms = timeout_ms
        self.calls = calls
        self.delay_seconds = delay_seconds

    def ping(self, target: str) -> PingResult:
        self.calls.append(target)
        time.sleep(self.delay_seconds)
        return PingResult(target, False, None, STATUS_TIMEOUT, datetime.now())


class _TimeoutBoundPingRunner:
    def __init__(self, timeout_ms: int, calls: list[tuple[str, int]]) -> None:
        self.timeout_ms = timeout_ms
        self.calls = calls

    def ping(self, target: str) -> PingResult:
        self.calls.append((target, self.timeout_ms))
        time.sleep(self.timeout_ms / 1000)
        return PingResult(target, False, None, STATUS_TIMEOUT, datetime.now())


class _GatedPingRunner:
    def __init__(
        self,
        timeout_ms: int,
        calls: list[str],
        *,
        target_count: int,
        all_started: threading.Event,
        release: threading.Event,
        lock: threading.Lock,
    ) -> None:
        self.timeout_ms = timeout_ms
        self.calls = calls
        self.target_count = target_count
        self.all_started = all_started
        self.release = release
        self.lock = lock

    def ping(self, target: str) -> PingResult:
        with self.lock:
            self.calls.append(target)
            if len(self.calls) >= self.target_count:
                self.all_started.set()
        self.release.wait(self.timeout_ms / 1000)
        return PingResult(target, True, 10.0, STATUS_OK, datetime.now())


class _CountingPingRunner:
    def __init__(self, timeout_ms: int, counters: dict[str, int]) -> None:
        self.timeout_ms = timeout_ms
        self.counters = counters
        self.counters["instances"] += 1

    def ping(self, target: str) -> PingResult:
        self.counters["pings"] += 1
        return PingResult(target, True, 20.0, STATUS_OK, datetime.now())

    def close(self) -> None:
        self.counters["closed"] += 1


class _CloseFailingProbe:
    def __init__(self, name: str, events: list[str]) -> None:
        self.name = name
        self.events = events

    def close(self) -> None:
        self.events.append(self.name)
        raise PermissionError("probe close locked")


class _RecordingCloseProbe:
    def __init__(self, name: str, events: list[str]) -> None:
        self.name = name
        self.events = events

    def close(self) -> None:
        self.events.append(self.name)


class _CloseFailingProbeRunner:
    def __init__(self, timeout_ms: int) -> None:
        self.timeout_ms = timeout_ms

    def ping(self, target: str) -> PingResult:
        return PingResult(target, True, 20.0, STATUS_OK, datetime.now())

    def close(self) -> None:
        raise PermissionError("probe close locked")


class _TwentyTargetPingRunner:
    def __init__(self, timeout_ms: int, calls: dict[str, int]) -> None:
        self.timeout_ms = timeout_ms
        self.calls = calls

    def ping(self, target: str) -> PingResult:
        self.calls[target] = self.calls.get(target, 0) + 1
        if int(target.rsplit(".", 1)[1]) > 5:
            time.sleep(1.5)
            return PingResult(target, False, None, STATUS_TIMEOUT, datetime.now())
        return PingResult(target, True, 10.0, STATUS_OK, datetime.now())


class _BackoffPingRunner:
    def __init__(self, timeout_ms: int, calls: dict[str, int]) -> None:
        self.timeout_ms = timeout_ms
        self.calls = calls

    def ping(self, target: str) -> PingResult:
        self.calls[target] = self.calls.get(target, 0) + 1
        if target == "198.51.100.2":
            return PingResult(target, False, None, STATUS_TIMEOUT, datetime.now())
        return PingResult(target, True, 10.0, STATUS_OK, datetime.now())


class _FailingPingRunner:
    def __init__(self, timeout_ms: int) -> None:
        self.timeout_ms = timeout_ms

    def ping(self, target: str) -> PingResult:
        raise OSError("simulated ping failure")


class _FakeTracerouteProbe:
    def __init__(self, hops: list[HopInfo]) -> None:
        self.hops = hops
        self.calls: list[tuple[str, int, bool]] = []

    def trace(self, target: str, timeout_ms: int, stop_event) -> list[HopInfo]:
        self.calls.append((target, timeout_ms, stop_event.is_set()))
        return self.hops


class _RecordingPingRunner:
    def __init__(self, timeout_ms: int, calls: list[tuple[str, int]]) -> None:
        self.timeout_ms = timeout_ms
        self.calls = calls

    def ping(self, target: str) -> PingResult:
        self.calls.append((target, self.timeout_ms))
        latency = 20.0 if target == "198.51.100.10" else 2.0
        return PingResult(target, True, latency, STATUS_OK, datetime.now())


class _HighLatencyPingRunner:
    def __init__(self, timeout_ms: int, calls: list[str]) -> None:
        self.timeout_ms = timeout_ms
        self.calls = calls

    def ping(self, target: str) -> PingResult:
        self.calls.append(target)
        latency = 125.0 if target == "198.51.100.10" else 2.0
        return PingResult(target, True, latency, STATUS_OK, datetime.now())


class _RecoveringLatencyPingRunner:
    def __init__(self, timeout_ms: int, calls: dict[str, int]) -> None:
        self.timeout_ms = timeout_ms
        self.calls = calls

    def ping(self, target: str) -> PingResult:
        count = self.calls.get(target, 0) + 1
        self.calls[target] = count
        latency = 125.0 if target == "198.51.100.10" and count == 1 else 20.0
        return PingResult(target, True, latency, STATUS_OK, datetime.now())


class _PatternPingRunner:
    def __init__(self, timeout_ms: int, calls: dict[str, int]) -> None:
        self.timeout_ms = timeout_ms
        self.calls = calls

    def ping(self, target: str) -> PingResult:
        count = self.calls.get(target, 0) + 1
        self.calls[target] = count
        if count % 15 == 0 and target == "198.51.100.10":
            return PingResult(target, False, None, STATUS_TIMEOUT, datetime.now())
        latency = 20.0 + (count % 7) if target == "198.51.100.10" else 2.0
        return PingResult(target, True, latency, STATUS_OK, datetime.now())


class _BlockingTracerouteProbe:
    def trace(self, target: str, timeout_ms: int, stop_event) -> list[HopInfo]:
        while not stop_event.is_set():
            time.sleep(0.01)
        return []


class _DelayedTracerouteProbe:
    def __init__(self, hops: list[HopInfo], *, delay_seconds: float = 0.15) -> None:
        self.hops = hops
        self.delay_seconds = delay_seconds

    def trace(self, target: str, timeout_ms: int, stop_event) -> list[HopInfo]:
        time.sleep(self.delay_seconds)
        return self.hops


class _FailingRouteLog:
    def __init__(self) -> None:
        self.calls = 0

    def write_snapshot(self, _snapshot, _change=None) -> None:
        self.calls += 1
        raise PermissionError("locked")


def _count_csv_rows(path: Path) -> int:
    return max(len(path.read_text(encoding="utf-8").splitlines()) - 1, 0)
