from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import (
    FIRST_COMPLETED,
    Future,
    ThreadPoolExecutor,
    as_completed,
    wait,
)
from concurrent.futures import (
    TimeoutError as FutureTimeoutError,
)
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from queue import Empty, Full, Queue

from PySide6.QtCore import QThread, Signal

from app.core.alerts import (
    JITTER_ALERT_KEY,
    LATENCY_ALERT_KEY,
    LOSS_ALERT_KEY,
    MOS_ALERT_KEY,
    SAMPLE_ALERT_KEY,
    TIMER_ALERT_KEY,
    AlertRuleConfig,
    evaluate_target_alerts,
)
from app.core.analyzer import analyze_path
from app.core.metrics import MetricsSession, TargetMetricTracker
from app.core.models import STATUS_ERROR, STATUS_PAUSED, STATUS_TIMEOUT, HopInfo, HopObservation, PingResult
from app.core.ping_runner import CommandPingRunner, TcpConnectRunner
from app.core.probes import PingProbeFactory, TracerouteProbe
from app.core.route_history import RouteHistory
from app.core.traceroute import ensure_target_hop, run_traceroute
from app.storage.route_log import RouteLogWriter
from app.storage.session_index import (
    SESSION_STATE_ARCHIVED,
    SESSION_STATE_PAUSED,
    SessionIndexStore,
    session_index_root_for_sample_path,
)
from app.storage.session_log import SessionLogWriter
from app.utils.diagnostics import operation_failure
from app.utils.validators import parse_ipv4_targets, validate_target

# 여러 IP를 동시에 측정할 때 프로그램이 과도하게 많은 ping/tracert를 만들지 않도록
# 제한값을 한곳에 모아 둡니다. 숫자를 바꾸면 성능과 안정성에 직접 영향이 있습니다.
MAX_IPV4_TARGETS = 50
RECENT_OBSERVATION_LIMIT = 300
MAX_TARGET_PING_WORKERS = 20
MAX_HOP_PING_WORKERS = 4
FINAL_TARGET_DRAIN_GRACE_SECONDS = 1.0
WORKER_POLL_SECONDS = 0.05
TRACE_REFRESH_SECONDS = 60.0
SESSION_LOG_CLOSE_TIMEOUT_SECONDS = 5.0
SESSION_LOG_QUEUE_MAX_BATCHES = 256
SESSION_LOG_BATCH_WINDOW_SECONDS = 0.05
SESSION_LOG_BATCH_MAX_OBSERVATIONS = 4096
BACKOFF_AFTER_FAILURES = 3
SLOW_BACKOFF_AFTER_FAILURES = 10
FIRST_BACKOFF_SECONDS = 2.0
SLOW_BACKOFF_SECONDS = 5.0
MEASUREMENT_MODE_FULL_ROUTE = "full_route"
MEASUREMENT_MODE_FINAL_HOP_ONLY = "final_hop_only"
MEASUREMENT_MODES = {MEASUREMENT_MODE_FULL_ROUTE, MEASUREMENT_MODE_FINAL_HOP_ONLY}
AUTO_FULL_ROUTE_ALERT_KEYS = {
    LOSS_ALERT_KEY,
    LATENCY_ALERT_KEY,
    JITTER_ALERT_KEY,
    SAMPLE_ALERT_KEY,
    TIMER_ALERT_KEY,
    MOS_ALERT_KEY,
}
PROBE_ENGINE_ICMP = "icmp"
PROBE_ENGINE_TCP_CONNECT = "tcp_connect"
PROBE_ENGINES = {PROBE_ENGINE_ICMP, PROBE_ENGINE_TCP_CONNECT}
WORKER_UNEXPECTED_ERROR_CODE = "WORKER_UNEXPECTED_ERROR"
SESSION_LOG_WRITE_FAILED_CODE = "SESSION_LOG_WRITE_FAILED"
SESSION_LOG_BACKPRESSURE_CODE = "SESSION_LOG_BACKPRESSURE"
ROUTE_LOG_WRITE_FAILED_CODE = "ROUTE_LOG_WRITE_FAILED"
ROUTE_LOG_CLOSE_FAILED_CODE = "ROUTE_LOG_CLOSE_FAILED"
SESSION_INDEX_FINISH_FAILED_CODE = "SESSION_INDEX_FINISH_FAILED"
WORKER_EXECUTOR_SHUTDOWN_FAILED_CODE = "WORKER_EXECUTOR_SHUTDOWN_FAILED"


def alert_history_observation_limit(config: AlertRuleConfig, interval_seconds: int) -> int:
    """Return enough per-target samples to evaluate every configured alert window."""

    time_windows = [0]
    if config.loss_enabled:
        time_windows.append(max(int(config.loss_window_seconds), 0))
    if config.timer_enabled:
        time_windows.append(max(int(config.timer_window_seconds), 0))
    if config.mos_enabled:
        time_windows.append(max(int(config.mos_window_seconds), 0))
    sample_windows = [2]
    if config.sample_enabled or config.jitter_enabled:
        sample_windows.append(max(int(config.sample_window_count), 2))
    cadence = max(int(interval_seconds), 1)
    timed_samples = math.ceil(max(time_windows) / cadence) + 2
    return max(RECENT_OBSERVATION_LIMIT, timed_samples, max(sample_windows) + 1)


class _SessionLogWriterFailed(RuntimeError):
    """Background session-log failure surfaced on the measurement thread."""

    def __init__(self, original: Exception) -> None:
        super().__init__(str(original))
        self.original = original


class SessionLogBackpressureError(RuntimeError):
    """저장 queue가 한계에 도달해 샘플 무결성을 보장할 수 없음을 나타냅니다."""


@dataclass
class TargetProbeState:
    """대상 IP 하나의 고정 cadence와 실패 누적 상태를 보관합니다.

    ``next_due``는 마지막 ping이 *끝난 시각*이 아니라 계획된 due-time에서
    전진합니다. 느린 ping이나 일시적인 scheduler 지연 때문에 간격이 계속
    밀리는 drift를 막고, 놓친 slot은 한 번에 하나씩 몰아서 실행하지 않고
    다음 미래 slot까지 건너뜁니다.
    """

    target: str
    next_due: float = 0.0
    consecutive_failures: int = 0
    current_interval_seconds: float = 0.0
    last_status: str = "WAITING"
    last_started_at: float = 0.0
    last_completed_at: float = 0.0
    last_scheduled_due: float = 0.0
    configured_interval_seconds: float | None = None
    scheduled_count: int = 0
    skipped_slot_count: int = 0
    max_start_lateness_seconds: float = 0.0

    def is_due(self, base_interval_seconds: float, now: float) -> bool:
        """Apply interval changes and report whether one probe may start now."""

        effective_interval = self._next_interval(base_interval_seconds)
        if self.configured_interval_seconds is None:
            self.configured_interval_seconds = base_interval_seconds
            self.current_interval_seconds = effective_interval
            self.next_due = now
        elif effective_interval != self.current_interval_seconds:
            self.configured_interval_seconds = base_interval_seconds
            self.current_interval_seconds = effective_interval
            anchor = self.last_scheduled_due or self.last_started_at
            self.next_due, skipped = self._future_due(anchor, effective_interval, now)
            self.skipped_slot_count += skipped
        return effective_interval <= 0 or now >= self.next_due

    def mark_started(self, now: float) -> None:
        """Reserve the currently due slot before submitting the Future."""

        scheduled_due = self.next_due if self.current_interval_seconds > 0 else now
        self.last_scheduled_due = scheduled_due
        self.last_started_at = now
        self.scheduled_count += 1
        self.max_start_lateness_seconds = max(
            self.max_start_lateness_seconds,
            max(now - scheduled_due, 0.0),
        )

    def record_result(self, result: PingResult, base_interval_seconds: float, now: float) -> None:
        self.last_status = result.status
        self.last_completed_at = now
        if result.success:
            self.consecutive_failures = 0
        else:
            self.consecutive_failures += 1

        next_interval = self._next_interval(base_interval_seconds)
        self.configured_interval_seconds = base_interval_seconds
        self.current_interval_seconds = next_interval
        anchor = self.last_scheduled_due or self.last_started_at or now
        self.next_due, skipped = self._future_due(anchor, next_interval, now)
        self.skipped_slot_count += skipped

    @staticmethod
    def _future_due(anchor: float, interval_seconds: float, now: float) -> tuple[float, int]:
        """Return the first future slot and the number of missed slots."""

        if interval_seconds <= 0:
            return now, 0
        if anchor <= 0:
            return now + interval_seconds, 0
        due = anchor + interval_seconds
        if due > now:
            return due, 0
        missed = math.floor((now - due) / interval_seconds) + 1
        return due + missed * interval_seconds, missed

    def _next_interval(self, base_interval_seconds: float) -> float:
        # 연속 실패가 많은 대상은 잠시 느리게 측정합니다. 응답이 없는 IP가 많아도
        # 전체 측정 루프가 막히지 않게 하는 안정성 장치입니다.
        if base_interval_seconds <= 0:
            return 0.0
        if self.consecutive_failures >= SLOW_BACKOFF_AFTER_FAILURES:
            return max(base_interval_seconds, SLOW_BACKOFF_SECONDS)
        if self.consecutive_failures >= BACKOFF_AFTER_FAILURES:
            return max(base_interval_seconds, FIRST_BACKOFF_SECONDS)
        return base_interval_seconds


@dataclass(frozen=True)
class WorkerDiagnostics:
    """측정 스레드 내부 상태를 UI에 보여주기 위한 요약 정보입니다."""

    active_ping_count: int
    pending_ping_count: int
    timeout_target_count: int
    backoff_target_count: int
    log_queue_depth: int
    average_loop_delay_ms: float
    last_update_iso: str
    tracert_status: str
    paused_target_count: int = 0
    target_probe_engine: str = "ICMP"
    route_probe_engine: str = "tracert/ICMP"
    tcp_port: int | None = None
    cadence_scheduled_count: int = 0
    cadence_skipped_slot_count: int = 0
    max_cadence_start_lateness_ms: float = 0.0


class _ThreadLocalPingProbePool:
    """각 ping 작업 스레드가 자기 전용 probe를 재사용하게 해 주는 작은 풀입니다."""

    def __init__(self, factory: Callable[[], object]) -> None:
        self._factory = factory
        self._local = threading.local()
        self._lock = threading.Lock()
        self._probes: list[object] = []

    def ping(self, target: str) -> PingResult:
        probe = getattr(self._local, "probe", None)
        if probe is None:
            probe = self._factory()
            self._local.probe = probe
            with self._lock:
                self._probes.append(probe)
        return probe.ping(target)

    def close(self) -> None:
        with self._lock:
            probes = list(self._probes)
            self._probes.clear()
        for probe in probes:
            close = getattr(probe, "close", None)
            if close:
                try:
                    close()
                except Exception:
                    pass


class _AsyncSessionLogWriter:
    """측정 결과를 별도 스레드에서 저장해 UI와 ping 루프가 디스크 I/O에 묶이지 않게 합니다."""

    def __init__(
        self,
        writer: SessionLogWriter,
        on_samples_written: Callable[[int, datetime, list[object]], None] | None = None,
        *,
        max_queue_batches: int = SESSION_LOG_QUEUE_MAX_BATCHES,
    ) -> None:
        self.path = writer.path
        self._writer = writer
        self._on_samples_written = on_samples_written
        self._queue: Queue[list[HopObservation]] = Queue(maxsize=max(int(max_queue_batches), 1))
        self._error: Exception | None = None
        self._metadata_error: Exception | None = None
        self._error_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._samples_written = 0
        self._last_timestamp: datetime | None = None
        self._pending_metadata_count = 0
        self._pending_metadata_last_timestamp: datetime | None = None
        self._closed = False
        self._close_lock = threading.Lock()
        self._close_requested = threading.Event()
        self._thread = threading.Thread(target=self._run, name="session-log-writer", daemon=True)
        self._thread.start()

    def write_many(self, observations: list[HopObservation]) -> None:
        if observations:
            with self._close_lock:
                self._raise_if_failed()
                if not self._closed:
                    try:
                        self._queue.put_nowait(list(observations))
                    except Full as exc:
                        error = SessionLogBackpressureError(
                            f"{SESSION_LOG_BACKPRESSURE_CODE}: queue_batches={self._queue.maxsize}"
                        )
                        self._set_error(error)
                        raise _SessionLogWriterFailed(error) from exc

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    @property
    def samples_written(self) -> int:
        with self._stats_lock:
            return self._samples_written

    @property
    def metadata_error(self) -> Exception | None:
        with self._error_lock:
            return self._metadata_error

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                error = self._current_error()
                if error:
                    raise error
                return
            self._closed = True
            self._close_requested.set()
        self._thread.join(SESSION_LOG_CLOSE_TIMEOUT_SECONDS)
        if self._thread.is_alive():
            raise TimeoutError("session log writer did not close within timeout")
        error = self._current_error()
        if error:
            raise error

    def _run(self) -> None:
        pending: list[HopObservation] = []
        try:
            while True:
                try:
                    item = self._queue.get(timeout=WORKER_POLL_SECONDS)
                except Empty:
                    if self._close_requested.is_set():
                        return
                    continue
                pending.extend(item)

                deadline = time.monotonic() + SESSION_LOG_BATCH_WINDOW_SECONDS
                while len(pending) < SESSION_LOG_BATCH_MAX_OBSERVATIONS:
                    try:
                        if self._close_requested.is_set():
                            extra = self._queue.get_nowait()
                        else:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                break
                            extra = self._queue.get(timeout=remaining)
                    except Empty:
                        break
                    pending.extend(extra)

                if pending:
                    written = list(pending)
                    self._writer.write_many(pending)
                    self._record_samples_written(written)
                    self._notify_samples_written(written)
                    pending = []
                if self._close_requested.is_set() and self._queue.empty():
                    self._retry_pending_metadata()
                    return
        except Exception as exc:
            self._set_error(exc)
        finally:
            try:
                self._writer.close()
            except Exception as exc:
                self._set_error(exc)

    @property
    def segment_paths(self) -> list[object]:
        return list(getattr(self._writer, "paths", [self.path]))

    def _notify_samples_written(self, observations: list[HopObservation]) -> None:
        if not observations:
            return
        last_timestamp = max(observation.timestamp for observation in observations)
        self._pending_metadata_count += len(observations)
        if self._pending_metadata_last_timestamp is None:
            self._pending_metadata_last_timestamp = last_timestamp
        else:
            self._pending_metadata_last_timestamp = max(
                self._pending_metadata_last_timestamp,
                last_timestamp,
            )
        self._retry_pending_metadata()

    def _retry_pending_metadata(self) -> None:
        if self._on_samples_written is None or self._pending_metadata_count <= 0:
            return
        try:
            self._on_samples_written(
                self._pending_metadata_count,
                self._pending_metadata_last_timestamp or datetime.now(),
                self.segment_paths,
            )
        except Exception as exc:
            with self._error_lock:
                self._metadata_error = exc
            return
        self._pending_metadata_count = 0
        self._pending_metadata_last_timestamp = None
        with self._error_lock:
            self._metadata_error = None

    def _record_samples_written(self, observations: list[HopObservation]) -> None:
        if not observations:
            return
        last_timestamp = max(observation.timestamp for observation in observations)
        with self._stats_lock:
            self._samples_written += len(observations)
            if self._last_timestamp is None:
                self._last_timestamp = last_timestamp
            else:
                self._last_timestamp = max(self._last_timestamp, last_timestamp)

    def _raise_if_failed(self) -> None:
        error = self._current_error()
        if error:
            raise _SessionLogWriterFailed(error) from error

    def _set_error(self, error: Exception) -> None:
        with self._error_lock:
            if self._error is None:
                self._error = error

    def _current_error(self) -> Exception | None:
        with self._error_lock:
            return self._error


class MeasurementWorker(QThread):
    """대상 목록을 실제로 측정하고, 그래프/표/세션 저장소로 보낼 데이터를 만드는 작업자입니다.

    MainWindow는 화면과 버튼을 담당하고, 이 클래스는 네트워크 측정의 실제 흐름을 담당합니다.
    흐름은 대략 `입력 IP 정리 -> tracert 갱신 -> 각 대상 ping -> 통계 계산 -> CSV 저장 -> UI 신호 전송`입니다.
    """

    trace_completed = Signal(object)
    route_changed = Signal(object)
    measurement_updated = Signal(object, object, object, object, object, object)
    diagnostics_updated = Signal(object)
    session_log_ready = Signal(str)
    status_message = Signal(str)
    error_message = Signal(str)

    def __init__(
        self,
        target: str,
        interval_seconds: int,
        max_cycles: int | None,
        timeout_ms: int = 1000,
        targets: list[str] | None = None,
        ping_probe_factory: PingProbeFactory | None = None,
        traceroute_probe: TracerouteProbe | None = None,
        measurement_mode: str = MEASUREMENT_MODE_FULL_ROUTE,
        probe_engine: str = PROBE_ENGINE_ICMP,
        tcp_port: int = 443,
        alert_rule_config: AlertRuleConfig | None = None,
        auto_full_route_on_alert: bool = True,
        auto_restore_final_hop_on_recovery: bool = False,
        parent=None,
        session_log_root: str | Path | None = None,
    ) -> None:
        super().__init__(parent)
        self.target = target.strip()
        self.interval_seconds = interval_seconds
        self.max_cycles = max_cycles
        self.timeout_ms = timeout_ms
        self.targets = self._normalize_targets(targets)
        self.ping_probe_factory = ping_probe_factory
        self.traceroute_probe = traceroute_probe
        self.measurement_mode = (
            measurement_mode if measurement_mode in MEASUREMENT_MODES else MEASUREMENT_MODE_FULL_ROUTE
        )
        self.probe_engine = probe_engine if probe_engine in PROBE_ENGINES else PROBE_ENGINE_ICMP
        self.tcp_port = max(min(int(tcp_port), 65535), 1)
        self.alert_rule_config = alert_rule_config or AlertRuleConfig()
        self.auto_full_route_on_alert = bool(auto_full_route_on_alert)
        self.auto_restore_final_hop_on_recovery = bool(auto_restore_final_hop_on_recovery)
        self.session_log_root = Path(session_log_root) if session_log_root is not None else None
        self.resumed_from_session_id = ""
        # 아래 값들은 UI 버튼에서 들어오는 중지/일시정지/간격 변경 요청을 안전하게 반영하기 위한 상태입니다.
        self._stop_event = threading.Event()
        self._control_lock = threading.Lock()
        self._paused_targets: set[str] = set()
        self._target_interval_overrides: dict[str, int] = {}
        self._pending_add_targets: list[str] = []
        self._pending_remove_targets: set[str] = set()
        self._target_schedule_cursor = 0
        self._ping_probe_pool: _ThreadLocalPingProbePool | None = None
        self._hop_ping_probe_pool: _ThreadLocalPingProbePool | None = None
        self._route_history = RouteHistory()
        self._route_log_write_failed = False
        self._auto_full_route_active = False
        self._tracert_status = "대기"

    def request_stop(self) -> None:
        self._stop_event.set()

    def pause_targets(self, targets: list[str]) -> None:
        with self._control_lock:
            self._paused_targets.update(target for target in targets if target in self.targets)

    def resume_targets(self, targets: list[str]) -> None:
        with self._control_lock:
            for target in targets:
                self._paused_targets.discard(target)

    def add_targets(self, targets: list[str]) -> list[str]:
        normalized, _invalid = parse_ipv4_targets("\n".join(targets))
        if not normalized:
            return []
        with self._control_lock:
            existing = set(self.targets).union(self._pending_add_targets)
            available = max(MAX_IPV4_TARGETS - len(existing), 0)
            additions = [target for target in normalized if target not in existing][:available]
            self._pending_add_targets.extend(additions)
            return additions

    def remove_targets(self, targets: list[str]) -> list[str]:
        normalized, _invalid = parse_ipv4_targets("\n".join(targets))
        if not normalized:
            return []
        with self._control_lock:
            remove_set = set(normalized)
            self._pending_add_targets = [target for target in self._pending_add_targets if target not in remove_set]
            removals = [target for target in self.targets if target in remove_set]
            self._pending_remove_targets.update(removals)
            self._paused_targets.difference_update(remove_set)
            for target in remove_set:
                self._target_interval_overrides.pop(target, None)
            return removals

    def set_interval_seconds(self, interval_seconds: int) -> None:
        with self._control_lock:
            self.interval_seconds = max(int(interval_seconds), 0)
            self._target_interval_overrides.clear()

    def set_target_interval_seconds(self, targets: list[str], interval_seconds: int) -> None:
        interval = max(int(interval_seconds), 0)
        with self._control_lock:
            for target in targets:
                if target in self.targets:
                    self._target_interval_overrides[target] = interval

    def target_interval_overrides(self) -> dict[str, int]:
        with self._control_lock:
            return dict(self._target_interval_overrides)

    def paused_targets(self) -> set[str]:
        with self._control_lock:
            return set(self._paused_targets)

    def run(self) -> None:
        """Qt가 별도 스레드에서 호출하는 메인 측정 루프입니다."""

        # 사용자가 입력한 값은 이 지점에서 다시 IPv4 목록으로 검증합니다.
        # GUI 검증을 통과했더라도 작업자 스레드에서 한 번 더 막아야 저장/측정 로직이 안전합니다.
        targets, invalid = parse_ipv4_targets("\n".join(self.targets or [self.target]))
        if invalid:
            self.error_message.emit(f"IPv4 주소만 입력 가능합니다: {', '.join(invalid[:5])}")
            return
        if not targets:
            self.error_message.emit("대상 IPv4 주소를 입력하세요.")
            return
        if len(targets) > MAX_IPV4_TARGETS:
            targets = targets[:MAX_IPV4_TARGETS]
        self.targets = targets
        self._route_history = RouteHistory()

        # tracert 기준 대상은 전체 대상 목록 중 하나여야 합니다. 그래프의 hop 경로를
        # 어느 대상 기준으로 그릴지 정하는 값이라서 여기서 엄격하게 확인합니다.
        valid, message = validate_target(self.target)
        if not valid or self.target not in self.targets:
            self.error_message.emit(message or "Tracert 대상은 등록된 IPv4 주소 중 하나여야 합니다.")
            return
        if self._stop_event.is_set():
            self.status_message.emit("측정이 중지되었습니다.")
            return

        target_history_limit = alert_history_observation_limit(self.alert_rule_config, self.interval_seconds)
        target_trackers = {
            target: TargetMetricTracker(target, recent_observation_limit=target_history_limit)
            for target in self.targets
        }
        recent_observations: deque[HopObservation] = deque(maxlen=RECENT_OBSERVATION_LIMIT)

        trace_executor: ThreadPoolExecutor | None = None
        target_executor: ThreadPoolExecutor | None = None
        hop_executor: ThreadPoolExecutor | None = None
        trace_future: Future[list[HopInfo]] | None = None
        # Future가 시작될 때의 TargetProbeState 객체를 lifecycle token으로 보관합니다.
        # 같은 IP를 삭제 후 재추가하면 새 state 객체가 만들어져 이전 결과를 구분할 수 있습니다.
        target_futures: dict[Future[PingResult], TargetProbeState] = {}
        hop_futures: dict[Future[PingResult], str] = {}
        active_target_pings: set[str] = set()
        active_hop_pings: set[str] = set()
        target_states = {target: TargetProbeState(target) for target in self.targets}
        session_writer: SessionLogWriter | None = None
        session_log: _AsyncSessionLogWriter | None = None
        route_log: RouteLogWriter | None = None
        session_index: SessionIndexStore | None = None
        session_id: str | None = None
        metrics: MetricsSession | None = None
        hops: list[HopInfo] = []
        full_cycles = 0
        next_trace_refresh_due = time.monotonic() + TRACE_REFRESH_SECONDS
        loop_delays: deque[float] = deque(maxlen=120)
        completed_normally = False
        last_error = ""

        try:
            # tracert, 최종 대상 ping, hop ping을 분리된 실행기로 돌립니다.
            # 생성 도중 실패해도 finally에서 이미 만든 실행기를 정리할 수 있게 try 안에서 만듭니다.
            trace_executor = ThreadPoolExecutor(max_workers=1)
            target_executor = ThreadPoolExecutor(max_workers=MAX_TARGET_PING_WORKERS)
            hop_executor = ThreadPoolExecutor(max_workers=MAX_HOP_PING_WORKERS)
            trace_future = self._start_trace_refresh(trace_executor) if self._uses_full_route() else None
            # 세션 CSV와 세션 인덱스는 측정 시작 직후 만들어 둡니다.
            # 프로그램이 중간에 꺼져도 Session Manager가 남은 CSV를 찾아 복구할 수 있습니다.
            self._ping_probe_pool = _ThreadLocalPingProbePool(self._new_ping_probe)
            self._hop_ping_probe_pool = _ThreadLocalPingProbePool(self._new_hop_ping_probe)
            session_writer = SessionLogWriter.create(self.target, root=self.session_log_root)
            route_log = RouteLogWriter.create_for_session(session_writer.path)
            session_index = SessionIndexStore.create(session_index_root_for_sample_path(session_writer.path))
            session_record = session_index.register_session(
                target=self.target,
                sample_path=session_writer.path,
                route_path=route_log.path,
                started_at=datetime.now(),
                interval_seconds=self.interval_seconds,
                measurement_mode=self._session_measurement_mode(),
                target_count=len(self.targets),
                probe_engine=self.probe_engine,
                tcp_port=self.tcp_port if self.probe_engine == PROBE_ENGINE_TCP_CONNECT else None,
                route_probe_engine=self._route_probe_label(),
                resumed_from_session_id=self.resumed_from_session_id,
            )
            session_id = session_record.session_id
            session_log = _AsyncSessionLogWriter(
                session_writer,
                lambda count, last_timestamp, segments: session_index.add_samples(
                    session_record.session_id,
                    count,
                    last_timestamp,
                    segments=segments,
                ),
            )
            self.session_log_ready.emit(str(session_log.path))
            if self._uses_full_route():
                self.status_message.emit("측정 중... 경로 탐색 병행")
            else:
                self._tracert_status = "final hop only"
                self.status_message.emit("측정 중... 최종 IP만")

            # 아래 while 문이 실제 장시간 측정 루프입니다. 한 바퀴마다 새 ping을 예약하고,
            # 완료된 결과를 수집한 뒤 UI와 저장소에 반영합니다.
            while not self._stop_event.is_set():
                primary_changed = self._apply_pending_target_changes(
                    target_trackers,
                    target_states,
                    active_target_pings,
                )
                if not self.targets:
                    self.status_message.emit("측정 대상이 없어 측정을 중지합니다.")
                    break
                if primary_changed:
                    metrics = None
                    hops = []
                    if trace_future is not None:
                        trace_future.cancel()
                        trace_future = None
                    if self._uses_full_route():
                        trace_future = self._start_trace_refresh(trace_executor)
                        next_trace_refresh_due = time.monotonic() + TRACE_REFRESH_SECONDS
                round_started_at = time.monotonic()
                is_final_round = self.max_cycles is not None and full_cycles + 1 >= self.max_cycles
                scheduled_hop_targets: set[str] = set()
                metrics, hops = self._refresh_trace_result(
                    metrics,
                    hops,
                    trace_future,
                    route_log,
                    first_check=(full_cycles == 0),
                )
                if trace_future is not None and trace_future.done():
                    trace_future = None
                    next_trace_refresh_due = time.monotonic() + TRACE_REFRESH_SECONDS
                scheduled_targets = self._schedule_target_pings(
                    target_executor,
                    target_futures,
                    active_target_pings,
                    target_states,
                    now=round_started_at,
                )
                self._schedule_hop_pings(
                    hop_executor,
                    hop_futures,
                    active_hop_pings,
                    metrics,
                    hops,
                    scheduled_hop_targets,
                )

                round_deadline = round_started_at + self._target_round_wait_seconds()
                while not self._stop_event.is_set() and not self._target_round_ready(
                    scheduled_targets,
                    scheduled_hop_targets,
                    active_target_pings,
                    active_hop_pings,
                    round_deadline,
                ):
                    self._collect_completed_ping_results(
                        target_futures,
                        active_target_pings,
                        hop_futures,
                        active_hop_pings,
                        metrics,
                        hops,
                        target_trackers,
                        target_states,
                        session_log,
                        recent_observations,
                        timeout=self._next_poll_timeout(round_deadline),
                    )
                    metrics, hops = self._refresh_trace_result(metrics, hops, trace_future, route_log)
                    if trace_future is not None and trace_future.done():
                        trace_future = None
                        next_trace_refresh_due = time.monotonic() + TRACE_REFRESH_SECONDS
                    self._schedule_hop_pings(
                        hop_executor,
                        hop_futures,
                        active_hop_pings,
                        metrics,
                        hops,
                        scheduled_hop_targets,
                    )

                if is_final_round:
                    self._wait_for_active_target_pings(
                        target_futures,
                        active_target_pings,
                        hop_futures,
                        active_hop_pings,
                        metrics,
                        hops,
                        target_trackers,
                        target_states,
                        session_log,
                        recent_observations,
                    )
                self._collect_completed_ping_results(
                    target_futures,
                    active_target_pings,
                    hop_futures,
                    active_hop_pings,
                    metrics,
                    hops,
                    target_trackers,
                    target_states,
                    session_log,
                    recent_observations,
                    timeout=0,
                )
                trace_future = self._maybe_adjust_route_for_alerts(
                    trace_executor,
                    trace_future,
                    target_trackers,
                )
                self._emit_measurement_update(metrics, target_trackers, recent_observations)
                loop_delays.append(max(time.monotonic() - round_started_at - max(self.interval_seconds, 0), 0.0))
                self._emit_diagnostics(
                    active_target_pings,
                    active_hop_pings,
                    target_futures,
                    hop_futures,
                    target_states,
                    session_log,
                    loop_delays,
                )

                full_cycles += 1
                self.status_message.emit(
                    self._format_ping_progress(
                        len(self.targets) - len(active_target_pings),
                        len(self.targets),
                        waiting_for_trace=(self._uses_full_route() and metrics is None),
                    )
                )
                if self.max_cycles is not None and full_cycles >= self.max_cycles:
                    break

                if self._uses_full_route() and trace_future is None and time.monotonic() >= next_trace_refresh_due:
                    trace_future = self._start_trace_refresh(trace_executor)

                self._sleep_until_next_round(
                    round_started_at,
                    target_executor,
                    target_futures,
                    active_target_pings,
                    target_states,
                    hop_futures,
                    active_hop_pings,
                    metrics,
                    hops,
                    target_trackers,
                    session_log,
                    recent_observations,
                )

            completed_normally = not self._stop_event.is_set()
            self.status_message.emit(
                "측정이 완료되었습니다." if not self._stop_event.is_set() else "측정이 중지되었습니다."
            )
        except _SessionLogWriterFailed as exc:
            last_error = _session_log_error_summary(exc)
            operation_failure(
                last_error.split(":", 1)[0],
                "measurement.session_log_write",
                exc.original,
                target=self.target,
                session_path=session_log.path if session_log is not None else None,
            )
            self.error_message.emit(
                f"세션 로그 저장 중 오류가 발생했습니다. 세션은 Pause 상태로 저장됩니다. ({last_error})"
            )
        except Exception as exc:
            last_error = _error_code_summary(WORKER_UNEXPECTED_ERROR_CODE, exc)
            operation_failure(
                WORKER_UNEXPECTED_ERROR_CODE,
                "measurement.run",
                exc,
                target=self.target,
                session_path=session_log.path if session_log is not None else None,
            )
            self.error_message.emit(f"측정 중 오류가 발생했습니다. 세션은 Pause 상태로 저장됩니다. ({last_error})")
        finally:
            self._stop_event.set()
            self._cancel_pending_futures(trace_future, target_futures, hop_futures)
            for executor_name, executor in (
                ("target", target_executor),
                ("hop", hop_executor),
                ("trace", trace_executor),
            ):
                if executor is None:
                    continue
                try:
                    executor.shutdown(wait=True, cancel_futures=True)
                except Exception as exc:
                    executor_error = _error_code_summary(WORKER_EXECUTOR_SHUTDOWN_FAILED_CODE, exc)
                    last_error = _merge_error_summaries(last_error, executor_error)
                    operation_failure(
                        WORKER_EXECUTOR_SHUTDOWN_FAILED_CODE,
                        f"measurement.{executor_name}_executor_shutdown",
                        exc,
                        target=self.target,
                        session_path=session_log.path if session_log is not None else None,
                    )
                    self.error_message.emit(f"측정 작업 정리 중 오류가 발생했습니다. ({executor_error})")
            if session_log is not None:
                try:
                    self._collect_completed_ping_results(
                        target_futures,
                        active_target_pings,
                        hop_futures,
                        active_hop_pings,
                        metrics,
                        hops,
                        target_trackers,
                        target_states,
                        session_log,
                        recent_observations,
                        timeout=0,
                    )
                    session_log.close()
                except Exception as exc:
                    session_error = _session_log_error_summary(exc)
                    last_error = _merge_error_summaries(last_error, session_error)
                    operation_failure(
                        session_error.split(":", 1)[0],
                        "measurement.session_log_close",
                        exc,
                        target=self.target,
                        session_path=session_log.path,
                    )
                    self.error_message.emit(
                        f"세션 로그 저장 중 오류가 발생했습니다. 세션은 Pause 상태로 저장됩니다. ({session_error})"
                    )
            elif session_writer is not None:
                # 비동기 writer로 소유권이 넘어가기 전에 초기화가 실패한 경우에도
                # Windows 파일 핸들이 남지 않도록 원본 writer를 직접 닫습니다.
                try:
                    session_writer.close()
                except Exception as exc:
                    session_error = _session_log_error_summary(exc)
                    last_error = _merge_error_summaries(last_error, session_error)
                    operation_failure(
                        session_error.split(":", 1)[0],
                        "measurement.session_log_startup_close",
                        exc,
                        target=self.target,
                        session_path=session_writer.path,
                    )
                    self.error_message.emit(f"세션 로그 정리 중 오류가 발생했습니다. ({session_error})")
            if route_log is not None:
                try:
                    route_log.close()
                except Exception as exc:
                    route_error = _error_code_summary(ROUTE_LOG_CLOSE_FAILED_CODE, exc)
                    last_error = _merge_error_summaries(last_error, route_error)
                    operation_failure(
                        ROUTE_LOG_CLOSE_FAILED_CODE,
                        "measurement.route_log_close",
                        exc,
                        target=self.target,
                        session_path=session_log.path if session_log is not None else None,
                    )
                    self.error_message.emit(f"경로 로그 종료 중 오류가 발생했습니다. ({route_error})")
            if session_index is not None and session_id is not None:
                state = SESSION_STATE_ARCHIVED if completed_normally and not last_error else SESSION_STATE_PAUSED
                try:
                    session_index.finish_session(
                        session_id,
                        state=state,
                        ended_at=datetime.now(),
                        segments=session_log.segment_paths if session_log is not None else None,
                        last_error=last_error,
                        samples=session_log.samples_written if session_log is not None else None,
                    )
                except Exception as exc:
                    index_error = _error_code_summary(SESSION_INDEX_FINISH_FAILED_CODE, exc)
                    operation_failure(
                        SESSION_INDEX_FINISH_FAILED_CODE,
                        "measurement.session_index_finish",
                        exc,
                        target=self.target,
                        session_path=session_log.path if session_log is not None else None,
                    )
                    self.error_message.emit(f"세션 인덱스 마감 중 오류가 발생했습니다. ({index_error})")
            if self._ping_probe_pool is not None:
                self._ping_probe_pool.close()
                self._ping_probe_pool = None
            if self._hop_ping_probe_pool is not None:
                self._hop_ping_probe_pool.close()
                self._hop_ping_probe_pool = None

    @staticmethod
    def _cancel_pending_futures(
        trace_future: Future[list[HopInfo]] | None,
        target_futures: dict[Future[PingResult], TargetProbeState],
        hop_futures: dict[Future[PingResult], str],
    ) -> None:
        if trace_future is not None:
            trace_future.cancel()
        for future in [*target_futures, *hop_futures]:
            future.cancel()

    def _schedule_target_pings(
        self,
        executor: ThreadPoolExecutor,
        futures: dict[Future[PingResult], TargetProbeState],
        active_targets: set[str],
        target_states: dict[str, TargetProbeState],
        *,
        now: float | None = None,
    ) -> set[str]:
        scheduled: set[str] = set()
        now = time.monotonic() if now is None else now
        capacity = max(min(len(self.targets), MAX_TARGET_PING_WORKERS) - len(active_targets), 0)
        if capacity == 0:
            return scheduled
        # 여러 IP를 한 번에 전부 실행하면 timeout이 많은 환경에서 스레드가 급격히 늘 수 있습니다.
        # capacity만큼만 새 ping을 예약해서 프로그램 반응성과 시스템 부하를 같이 지킵니다.
        targets = list(self.targets)
        start_index = self._target_schedule_cursor % len(targets)
        for offset in range(len(targets)):
            target_index = (start_index + offset) % len(targets)
            target = targets[target_index]
            if self._is_target_paused(target):
                continue
            state = target_states[target]
            if target in active_targets:
                continue
            interval_seconds = self._target_base_interval_seconds(target)
            if not state.is_due(interval_seconds, now):
                continue
            state.mark_started(now)
            future = executor.submit(self._ping_target, target)
            futures[future] = state
            active_targets.add(target)
            scheduled.add(target)
            self._target_schedule_cursor = (target_index + 1) % len(targets)
            capacity -= 1
            if capacity == 0:
                break
        return scheduled

    def _schedule_hop_pings(
        self,
        executor: ThreadPoolExecutor,
        futures: dict[Future[PingResult], str],
        active_hops: set[str],
        metrics: MetricsSession | None,
        hops: list[HopInfo],
        scheduled_hops: set[str],
    ) -> None:
        if metrics is None or not self._uses_full_route():
            return
        capacity = max(MAX_HOP_PING_WORKERS - len(active_hops), 0)
        if capacity == 0:
            return

        registered_targets = set(self.targets)
        for hop in hops:
            ping_target = hop.ping_target
            if (
                not ping_target
                or ping_target in registered_targets
                or ping_target in active_hops
                or ping_target in scheduled_hops
            ):
                continue
            future = executor.submit(self._ping_hop, ping_target)
            futures[future] = ping_target
            active_hops.add(ping_target)
            scheduled_hops.add(ping_target)
            capacity -= 1
            if capacity == 0:
                return

    def _collect_completed_ping_results(
        self,
        target_futures: dict[Future[PingResult], TargetProbeState],
        active_target_pings: set[str],
        hop_futures: dict[Future[PingResult], str],
        active_hop_pings: set[str],
        metrics: MetricsSession | None,
        hops: list[HopInfo],
        target_trackers: dict[str, TargetMetricTracker],
        target_states: dict[str, TargetProbeState],
        session_log: _AsyncSessionLogWriter,
        recent_observations: deque[HopObservation],
        *,
        timeout: float,
    ) -> None:
        all_futures = list(target_futures) + list(hop_futures)
        if not all_futures:
            self._sleep_for(timeout)
            return

        # Future는 "백그라운드에서 실행 중인 ping 작업표"라고 보면 됩니다.
        # 완료된 Future만 꺼내 처리하고, 아직 끝나지 않은 ping은 다음 루프에서 다시 확인합니다.
        ready = {future for future in all_futures if future.done()}
        if not ready and timeout > 0:
            done, _pending = wait(all_futures, timeout=timeout, return_when=FIRST_COMPLETED)
            ready = set(done)
        ready.update(future for future in all_futures if future.done())

        for future in ready:
            if future in target_futures:
                submitted_state = target_futures.pop(future)
                target = submitted_state.target
                active_target_pings.discard(target)
                if future.cancelled():
                    continue
                if target_states.get(target) is not submitted_state:
                    # 삭제된 대상의 실행 중 ping은 취소되지 않을 수 있습니다.
                    # 같은 IP가 다시 추가됐더라도 이전 lifecycle 결과는 새 그래프에 넣지 않습니다.
                    continue
                result = self._future_result(future, target)
                target_states[target].record_result(
                    result,
                    self._target_base_interval_seconds(target),
                    time.monotonic(),
                )
                observations = self._record_target_ping_result(result, metrics, hops, target_trackers)
                self._store_observations(session_log, recent_observations, observations)
            elif future in hop_futures:
                target = hop_futures.pop(future)
                active_hop_pings.discard(target)
                if future.cancelled():
                    continue
                result = self._future_result(future, target)
                observations = self._record_hop_ping_result(result, metrics, hops)
                self._store_observations(session_log, recent_observations, observations)

    def _record_target_ping_result(
        self,
        result: PingResult,
        metrics: MetricsSession | None,
        hops: list[HopInfo],
        target_trackers: dict[str, TargetMetricTracker],
    ) -> list[HopObservation]:
        observations: list[HopObservation] = []
        if metrics is not None:
            for hop in hops:
                if hop.ping_target == result.target:
                    observations.append(metrics.add_result(hop.index, result))

        tracker = target_trackers.get(result.target)
        if tracker:
            tracker.set_recent_observation_limit(
                alert_history_observation_limit(
                    self.alert_rule_config,
                    self._target_base_interval_seconds(result.target),
                )
            )
            observations.append(tracker.add_result(result))
        return observations

    def _record_hop_ping_result(
        self,
        result: PingResult,
        metrics: MetricsSession | None,
        hops: list[HopInfo],
    ) -> list[HopObservation]:
        if metrics is None:
            return []
        return [metrics.add_result(hop.index, result) for hop in hops if hop.ping_target == result.target]

    def _store_observations(
        self,
        session_log: _AsyncSessionLogWriter,
        recent_observations: deque[HopObservation],
        observations: list[HopObservation],
    ) -> None:
        if not observations:
            return
        session_log.write_many(observations)
        recent_observations.extend(observations)

    def _maybe_adjust_route_for_alerts(
        self,
        executor: ThreadPoolExecutor,
        trace_future: Future[list[HopInfo]] | None,
        target_trackers: dict[str, TargetMetricTracker],
    ) -> Future[list[HopInfo]] | None:
        if not self.auto_full_route_on_alert:
            return trace_future
        tracker = target_trackers.get(self.target)
        if tracker is None:
            return trace_future
        active_keys, events = evaluate_target_alerts(
            list(tracker.observations),
            current_target=self.target,
            config=self.alert_rule_config,
        )
        route_adjustment_keys = active_keys.intersection(AUTO_FULL_ROUTE_ALERT_KEYS)
        if self.measurement_mode == MEASUREMENT_MODE_FINAL_HOP_ONLY and trace_future is None and route_adjustment_keys:
            reason = "대상 알림"
            for event in events:
                if event.key in route_adjustment_keys:
                    reason = event.title
                    break
            self.measurement_mode = MEASUREMENT_MODE_FULL_ROUTE
            self._auto_full_route_active = True
            self.status_message.emit(f"자동 전체 경로 전환: {reason}")
            return self._start_trace_refresh(executor)
        if self._auto_full_route_active and not route_adjustment_keys:
            self._auto_full_route_active = False
            if self.auto_restore_final_hop_on_recovery:
                self.measurement_mode = MEASUREMENT_MODE_FINAL_HOP_ONLY
                self.status_message.emit("자동 최종 IP만 복원: 알림 정상 복구")
            else:
                self.status_message.emit("알림 정상 복구: 전체 경로 유지")
            return trace_future
        return trace_future

    def _emit_measurement_update(
        self,
        metrics: MetricsSession | None,
        target_trackers: dict[str, TargetMetricTracker],
        recent_observations: deque[HopObservation],
    ) -> None:
        """현재까지 쌓인 측정값을 UI가 바로 그릴 수 있는 형태로 묶어서 보냅니다."""

        snapshots = metrics.snapshots() if metrics is not None else []
        if not self.targets or self.target not in target_trackers:
            return
        paused_targets = self.paused_targets()
        target_snapshots = [
            self._paused_snapshot(target_trackers[target].snapshot())
            if target in paused_targets
            else target_trackers[target].snapshot()
            for target in self.targets
        ]
        target_snapshot = target_trackers[self.target].snapshot()
        if self.target in paused_targets:
            target_snapshot = self._paused_snapshot(target_snapshot)
        analysis = analyze_path(snapshots, target_snapshot)
        self.measurement_updated.emit(
            snapshots,
            target_snapshot,
            target_snapshots,
            analysis,
            list(recent_observations),
            list(target_trackers[self.target].observations),
        )

    def _sleep_until_next_round(
        self,
        round_started_at: float,
        target_executor: ThreadPoolExecutor,
        target_futures: dict[Future[PingResult], TargetProbeState],
        active_target_pings: set[str],
        target_states: dict[str, TargetProbeState],
        hop_futures: dict[Future[PingResult], str],
        active_hop_pings: set[str],
        metrics: MetricsSession | None,
        hops: list[HopInfo],
        target_trackers: dict[str, TargetMetricTracker],
        session_log: _AsyncSessionLogWriter,
        recent_observations: deque[HopObservation],
    ) -> None:
        if self.interval_seconds <= 0:
            return
        deadline = round_started_at + self.interval_seconds
        while not self._stop_event.is_set() and time.monotonic() < deadline:
            primary_changed = self._apply_pending_target_changes(
                target_trackers,
                target_states,
                active_target_pings,
            )
            _ = primary_changed
            if not self.targets:
                self.status_message.emit("측정 대상이 없어 측정을 중지합니다.")
                self._stop_event.set()
                return
            self._schedule_target_pings(
                target_executor,
                target_futures,
                active_target_pings,
                target_states,
            )
            self._collect_completed_ping_results(
                target_futures,
                active_target_pings,
                hop_futures,
                active_hop_pings,
                metrics,
                hops,
                target_trackers,
                target_states,
                session_log,
                recent_observations,
                timeout=self._next_poll_timeout(deadline),
            )

    def _emit_diagnostics(
        self,
        active_target_pings: set[str],
        active_hop_pings: set[str],
        target_futures: dict[Future[PingResult], TargetProbeState],
        hop_futures: dict[Future[PingResult], str],
        target_states: dict[str, TargetProbeState],
        session_log: _AsyncSessionLogWriter,
        loop_delays: deque[float],
    ) -> None:
        """운영자가 장시간 측정 안정성을 확인할 수 있게 내부 부하 상태를 요약합니다."""

        timeout_target_count = sum(1 for state in target_states.values() if state.last_status == STATUS_TIMEOUT)
        backoff_target_count = sum(
            1
            for target, state in target_states.items()
            if self._target_base_interval_seconds(target) > 0
            and state.current_interval_seconds > self._target_base_interval_seconds(target)
        )
        pending_ping_count = len(target_futures) + len(hop_futures)
        average_loop_delay_ms = (sum(loop_delays) / len(loop_delays) * 1000) if loop_delays else 0.0
        self.diagnostics_updated.emit(
            WorkerDiagnostics(
                active_ping_count=len(active_target_pings) + len(active_hop_pings),
                pending_ping_count=pending_ping_count,
                timeout_target_count=timeout_target_count,
                backoff_target_count=backoff_target_count,
                log_queue_depth=session_log.queue_depth,
                average_loop_delay_ms=average_loop_delay_ms,
                last_update_iso=datetime.now().isoformat(timespec="seconds"),
                tracert_status=self._tracert_status,
                paused_target_count=len(self.paused_targets()),
                target_probe_engine=self._target_probe_label(),
                route_probe_engine=self._route_probe_label(),
                tcp_port=self.tcp_port if self.probe_engine == PROBE_ENGINE_TCP_CONNECT else None,
                cadence_scheduled_count=sum(state.scheduled_count for state in target_states.values()),
                cadence_skipped_slot_count=sum(state.skipped_slot_count for state in target_states.values()),
                max_cadence_start_lateness_ms=max(
                    (state.max_start_lateness_seconds * 1000 for state in target_states.values()),
                    default=0.0,
                ),
            )
        )

    def _wait_for_active_target_pings(
        self,
        target_futures: dict[Future[PingResult], TargetProbeState],
        active_target_pings: set[str],
        hop_futures: dict[Future[PingResult], str],
        active_hop_pings: set[str],
        metrics: MetricsSession | None,
        hops: list[HopInfo],
        target_trackers: dict[str, TargetMetricTracker],
        target_states: dict[str, TargetProbeState],
        session_log: _AsyncSessionLogWriter,
        recent_observations: deque[HopObservation],
    ) -> None:
        # 마지막 화면 갱신에는 executor 종료 중 뒤늦게 끝난 결과가 빠지지 않아야 합니다.
        # 일반 라운드 간격이 아니라 probe timeout을 기준으로 한 번만 충분히 기다립니다.
        wait_seconds = max(self.timeout_ms / 1000, WORKER_POLL_SECONDS) + FINAL_TARGET_DRAIN_GRACE_SECONDS
        deadline = time.monotonic() + wait_seconds
        while (
            (active_target_pings or (self.interval_seconds <= 0 and active_hop_pings))
            and not self._stop_event.is_set()
            and time.monotonic() < deadline
        ):
            self._collect_completed_ping_results(
                target_futures,
                active_target_pings,
                hop_futures,
                active_hop_pings,
                metrics,
                hops,
                target_trackers,
                target_states,
                session_log,
                recent_observations,
                timeout=self._next_poll_timeout(deadline),
            )

    def _refresh_trace_result(
        self,
        metrics: MetricsSession | None,
        hops: list[HopInfo],
        trace_future: Future[list[HopInfo]] | None,
        route_log: RouteLogWriter | None = None,
        *,
        first_check: bool = False,
    ) -> tuple[MetricsSession | None, list[HopInfo]]:
        """백그라운드 tracert 결과가 준비되었으면 hop 목록과 경로 변경 기록을 갱신합니다."""

        if trace_future is None:
            return metrics, hops
        if metrics is not None and not trace_future.done():
            return metrics, hops
        try:
            refreshed_hops = trace_future.result(timeout=0.05 if first_check else 0)
        except FutureTimeoutError:
            self._tracert_status = "탐색 중"
            return metrics, hops
        except Exception:
            self._tracert_status = "오류"
            self.status_message.emit("tracert 실행 중 오류가 발생했습니다. 대상 Ping은 계속 측정합니다.")
            refreshed_hops = [
                HopInfo(
                    index=1,
                    address=self.target,
                    hostname="Target",
                    timed_out=False,
                    is_target=True,
                )
            ]
        if self._stop_event.is_set():
            return metrics, hops
        if not refreshed_hops:
            refreshed_hops = ensure_target_hop([], self.target, self.target)
        self._tracert_status = "완료"
        route_change = self._route_history.record(refreshed_hops, datetime.now())
        if route_log is not None and self._route_history.snapshots:
            self._write_route_log_snapshot(route_log, route_change)
        self.trace_completed.emit(refreshed_hops)
        if route_change is not None:
            self.route_changed.emit(route_change)
        self.status_message.emit("측정 중...")
        return MetricsSession(refreshed_hops, recent_observation_limit=RECENT_OBSERVATION_LIMIT), refreshed_hops

    def _write_route_log_snapshot(
        self,
        route_log: RouteLogWriter,
        route_change,
    ) -> None:
        had_failure = self._route_log_write_failed
        try:
            route_log.write_snapshot(self._route_history.snapshots[-1], route_change)
        except OSError as exc:
            if self._route_log_write_failed:
                return
            self._route_log_write_failed = True
            operation_failure(
                ROUTE_LOG_WRITE_FAILED_CODE,
                "measurement.route_log_write",
                exc,
                target=self.target,
                session_path=getattr(route_log, "path", None),
            )
            route_error = _error_code_summary(ROUTE_LOG_WRITE_FAILED_CODE, exc)
            self.error_message.emit(f"경로 로그 저장 중 오류가 발생했습니다. 측정은 계속 진행합니다. ({route_error})")
        else:
            self._route_log_write_failed = False
            if had_failure:
                self.status_message.emit("경로 로그 저장이 정상화되었습니다.")

    def _target_round_wait_seconds(self) -> float:
        if self.interval_seconds <= 0:
            return 0
        ping_timeout_seconds = max(self.timeout_ms / 1000, WORKER_POLL_SECONDS)
        return min(float(self.interval_seconds), ping_timeout_seconds)

    def _target_base_interval_seconds(self, target: str) -> int:
        with self._control_lock:
            return max(int(self._target_interval_overrides.get(target, self.interval_seconds)), 0)

    def _drain_target_change_requests(self) -> tuple[list[str], list[str]]:
        with self._control_lock:
            removals = list(self._pending_remove_targets)
            additions = list(self._pending_add_targets)
            self._pending_remove_targets.clear()
            self._pending_add_targets.clear()
        return removals, additions

    def _apply_pending_target_changes(
        self,
        target_trackers: dict[str, TargetMetricTracker],
        target_states: dict[str, TargetProbeState],
        active_target_pings: set[str],
    ) -> bool:
        removals, additions = self._drain_target_change_requests()
        if not removals and not additions:
            return False

        previous_target = self.target
        for target in removals:
            if target in self.targets:
                self.targets.remove(target)
            target_trackers.pop(target, None)
            target_states.pop(target, None)
            # 실행 중인 Future가 끝날 때까지 reservation은 유지합니다. 같은 IP를
            # 삭제 후 즉시 재추가해도 이전 ping과 새 ping이 겹치지 않아야 합니다.

        for target in additions:
            if len(self.targets) >= MAX_IPV4_TARGETS or target in self.targets:
                continue
            self.targets.append(target)
            target_trackers[target] = TargetMetricTracker(
                target,
                recent_observation_limit=alert_history_observation_limit(
                    self.alert_rule_config,
                    self._target_base_interval_seconds(target),
                ),
            )
            target_states[target] = TargetProbeState(target)

        if self.target not in self.targets:
            self.target = self.targets[0] if self.targets else ""
            self._route_history = RouteHistory()

        if removals:
            self.status_message.emit(f"{len(removals)}개 IP를 측정에서 삭제했습니다.")
        if additions:
            self.status_message.emit(f"{len(additions)}개 IP를 측정에 추가했습니다.")
        return self.target != previous_target

    def _target_round_ready(
        self,
        scheduled_targets: set[str],
        scheduled_hops: set[str],
        active_target_pings: set[str],
        active_hop_pings: set[str],
        deadline: float,
    ) -> bool:
        if self.interval_seconds <= 0:
            target_ready = not any(target in active_target_pings for target in scheduled_targets)
            hop_ready = not any(hop in active_hop_pings for hop in scheduled_hops)
            return target_ready and hop_ready
        if scheduled_targets and not any(target in active_target_pings for target in scheduled_targets):
            return True
        return time.monotonic() >= deadline

    def _next_poll_timeout(self, deadline: float) -> float:
        remaining = max(deadline - time.monotonic(), 0)
        return min(WORKER_POLL_SECONDS, remaining)

    def _sleep_for(self, seconds: float) -> None:
        if seconds <= 0:
            return
        deadline = time.monotonic() + seconds
        while not self._stop_event.is_set() and time.monotonic() < deadline:
            time.sleep(min(WORKER_POLL_SECONDS, max(deadline - time.monotonic(), 0)))

    def _future_result(self, future: Future[PingResult], target: str) -> PingResult:
        try:
            return future.result()
        except Exception:
            return PingResult(target, False, None, STATUS_ERROR, datetime.now())

    def _ping_target(self, target: str) -> PingResult:
        if self._ping_probe_pool is not None:
            return self._ping_probe_pool.ping(target)
        return self._new_ping_probe().ping(target)

    def _ping_hop(self, target: str) -> PingResult:
        if self._hop_ping_probe_pool is not None:
            return self._hop_ping_probe_pool.ping(target)
        return self._new_hop_ping_probe().ping(target)

    def _start_trace_refresh(self, executor: ThreadPoolExecutor) -> Future[list[HopInfo]]:
        """tracert는 느릴 수 있으므로 별도 Future로 시작하고 ping 루프는 계속 돌립니다."""

        self._tracert_status = "탐색 중"
        return executor.submit(self._trace_hops)

    def _uses_full_route(self) -> bool:
        return self.measurement_mode == MEASUREMENT_MODE_FULL_ROUTE

    def _is_target_paused(self, target: str) -> bool:
        with self._control_lock:
            return target in self._paused_targets

    def _paused_snapshot(self, snapshot):
        return replace(snapshot, current_latency_ms=None, status=STATUS_PAUSED)

    def _trace_hops(self) -> list[HopInfo]:
        if self.traceroute_probe:
            hops = self.traceroute_probe.trace(
                self.target,
                timeout_ms=self.timeout_ms,
                stop_event=self._stop_event,
            )
        else:
            hops = run_traceroute(self.target, timeout_ms=self.timeout_ms, stop_event=self._stop_event)
        return ensure_target_hop(hops, self.target, self.target)

    def _collect_trace_result(
        self,
        trace_future: Future[list[HopInfo]],
        *,
        first_check: bool = False,
    ) -> tuple[MetricsSession | None, list[HopInfo]]:
        try:
            hops = trace_future.result(timeout=0.05 if first_check else 0)
        except FutureTimeoutError:
            return None, []
        except Exception:
            self.status_message.emit("tracert 실행 중 오류가 발생했습니다. 대상 Ping은 계속 측정합니다.")
            hops = [
                HopInfo(
                    index=1,
                    address=self.target,
                    hostname="Target",
                    timed_out=False,
                    is_target=True,
                )
            ]
        if self._stop_event.is_set():
            return None, []
        if not hops:
            hops = ensure_target_hop([], self.target, self.target)
        self.trace_completed.emit(hops)
        self.status_message.emit("측정 중...")
        return MetricsSession(hops, recent_observation_limit=RECENT_OBSERVATION_LIMIT), hops

    def _ping_unique_targets(
        self,
        hops: list[HopInfo],
        on_result: Callable[[PingResult, int, int, int], None] | None = None,
    ) -> dict[str, PingResult]:
        registered_targets = set(self.targets or [self.target])
        targets = {hop.ping_target for hop in hops if hop.ping_target}
        targets.update(registered_targets)
        clean_targets = sorted(target for target in targets if target)

        results: dict[str, PingResult] = {}
        max_workers = min(max(len(clean_targets), 1), 16)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(
                    (self._new_ping_probe() if target in registered_targets else self._new_hop_ping_probe()).ping,
                    target,
                ): target
                for target in clean_targets
            }
            for future in as_completed(future_map):
                target = future_map[future]
                try:
                    results[target] = future.result()
                except Exception:
                    results[target] = PingResult(target, False, None, STATUS_ERROR, datetime.now())
                if on_result is not None:
                    completed = len(results)
                    total = len(future_map)
                    on_result(results[target], completed, total, total - completed)
        return results

    def _new_ping_probe(self):
        """선택된 probe engine에 맞춰 새 ping probe를 만듭니다."""

        if self.ping_probe_factory:
            return self.ping_probe_factory(self.timeout_ms)
        if self.probe_engine == PROBE_ENGINE_TCP_CONNECT:
            return TcpConnectRunner(timeout_ms=self.timeout_ms, port=self.tcp_port)
        return CommandPingRunner(timeout_ms=self.timeout_ms)

    def _new_hop_ping_probe(self):
        if self.ping_probe_factory:
            return self.ping_probe_factory(self.timeout_ms)
        return CommandPingRunner(timeout_ms=self.timeout_ms)

    def _session_measurement_mode(self) -> str:
        if self.probe_engine == PROBE_ENGINE_TCP_CONNECT:
            return f"{self.measurement_mode}:{self.probe_engine}:port{self.tcp_port}"
        return f"{self.measurement_mode}:{self.probe_engine}"

    def _target_probe_label(self) -> str:
        if self.probe_engine == PROBE_ENGINE_TCP_CONNECT:
            return f"TCP Connect:{self.tcp_port}"
        return "ICMP"

    def _route_probe_label(self) -> str:
        if not self._uses_full_route():
            return "disabled"
        return "tracert/ICMP"

    def _normalize_targets(self, targets: list[str] | None) -> list[str]:
        """입력 대상 목록에서 빈 값과 중복을 제거하되 입력 순서는 유지합니다."""

        source = targets if targets is not None else [self.target]
        normalized, _invalid = parse_ipv4_targets("\n".join(source))
        return normalized[:MAX_IPV4_TARGETS]

    def _format_ping_progress(self, completed: int, total: int, *, waiting_for_trace: bool) -> str:
        pending = max(total - completed, 0)
        trace_prefix = "경로 탐색 중, " if waiting_for_trace else ""
        if pending:
            return f"측정 중... {trace_prefix}대상 Ping {completed}/{total}, {pending}개 응답 대기"
        return f"측정 중... {trace_prefix}대상 Ping {completed}/{total}"


def _error_code_summary(code: str, exc: Exception) -> str:
    return f"{code}: {type(exc).__name__}"


def _session_log_error_summary(exc: Exception) -> str:
    original = exc.original if isinstance(exc, _SessionLogWriterFailed) else exc
    if isinstance(original, SessionLogBackpressureError):
        return f"{SESSION_LOG_BACKPRESSURE_CODE}: queue_full"
    return _error_code_summary(SESSION_LOG_WRITE_FAILED_CODE, original)


def _merge_error_summaries(current: str, extra: str) -> str:
    if not current:
        return extra
    if not extra or extra in current.split("; "):
        return current
    return f"{current}; {extra}"
