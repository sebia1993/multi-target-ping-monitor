"""Bounded, immutable-input report aggregation. No widgets or network probes."""
from __future__ import annotations

import math
from collections import Counter, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path

from app.core.models import HopObservation, MetricSnapshot
from app.core.observation_stats import FocusSnapshotBuilder
from app.storage.session_log import (
    iter_observations_in_range,
    observation_to_row,
    session_log_bounds,
)

MAX_GRAPH_BINS = 900
MAX_FAILURE_DETAILS = 20
IGNORED_STATUSES = {"PAUSED", "NO_PING_TARGET"}


class ReportCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class ReportTarget:
    address: str
    alias: str = ""
    paused: bool = False
    interval_seconds: int | None = None
    current_state: str = ""


@dataclass(frozen=True)
class ReportRequest:
    targets: tuple[ReportTarget, ...]
    start: datetime | None
    end: datetime
    generated_at: datetime
    session_path: Path | None = None
    live_tail: tuple[HopObservation, ...] = ()
    scope_label: str = "현재 그래프 구간"
    timezone_label: str = "기록 PC의 로컬 시각"


@dataclass
class GraphBin:
    samples: int = 0
    failures: int = 0
    paused: int = 0
    latency_count: int = 0
    latency_sum: float = 0.0
    minimum: float | None = None
    maximum: float | None = None

    def add(self, point: HopObservation) -> None:
        if point.status in IGNORED_STATUSES:
            self.paused += 1
            return
        self.samples += 1
        if not point.success:
            self.failures += 1
        if point.success and point.latency_ms is not None:
            value = point.latency_ms
            if not math.isfinite(value) or value < 0:
                raise ValueError("유효하지 않은 지연시간이 포함되어 있습니다.")
            self.latency_count += 1
            self.latency_sum += value
            self.minimum = value if self.minimum is None else min(self.minimum, value)
            self.maximum = value if self.maximum is None else max(self.maximum, value)

    @property
    def mean(self) -> float | None:
        return self.latency_sum / self.latency_count if self.latency_count else None


@dataclass(frozen=True)
class TargetReport:
    target: ReportTarget
    snapshot: MetricSnapshot | None
    bins: tuple[GraphBin, ...]
    first_sample: datetime | None
    last_sample: datetime | None
    failure_details: tuple[tuple[datetime, str], ...]
    ignored_samples: int
    error_samples: int


@dataclass(frozen=True)
class MonitoringReport:
    request: ReportRequest
    start: datetime
    end: datetime
    targets: tuple[TargetReport, ...]
    notes: tuple[str, ...]


@dataclass
class _TargetAccumulator:
    target: ReportTarget
    bins: list[GraphBin]
    builder: FocusSnapshotBuilder = field(default_factory=FocusSnapshotBuilder)
    first: datetime | None = None
    last: datetime | None = None
    failures: deque = field(default_factory=lambda: deque(maxlen=MAX_FAILURE_DETAILS))
    ignored: int = 0
    errors: int = 0


def closed_second(now: datetime) -> datetime:
    """Legacy CSV rounds timestamps to seconds: never include an open second."""
    return now.replace(microsecond=0) - timedelta(seconds=1)


def _key(point: HopObservation) -> tuple[object, ...]:
    # Compare the persisted representation, not microsecond/full-float live data.
    return tuple(observation_to_row(point))


def _normalized(point: HopObservation) -> HopObservation:
    return replace(
        point,
        timestamp=point.timestamp.replace(microsecond=0),
        latency_ms=float(f"{point.latency_ms:.3f}") if point.latency_ms is not None else None,
    )


def _check_cancel(cancelled: Callable[[], bool]) -> None:
    if cancelled():
        raise ReportCancelled("보고서 저장을 취소했습니다.")


def report_observations(
    request: ReportRequest,
    start: datetime,
    cancelled: Callable[[], bool],
) -> Iterable[HopObservation]:
    selected = {target.address for target in request.targets}

    def selected_point(point: HopObservation) -> bool:
        return (
            point.hop_index == 0
            and point.address in selected
            and start <= point.timestamp.replace(microsecond=0) <= request.end
        )

    tail = tuple(_normalized(point) for point in request.live_tail if selected_point(point))
    pending = Counter(_key(point) for point in tail)
    if request.session_path is not None:
        if not request.session_path.is_file():
            raise OSError("세션 원본을 찾을 수 없어 완전한 보고서를 만들 수 없습니다.")
        for point in iter_observations_in_range(request.session_path, start, request.end, strict=True):
            _check_cancel(cancelled)
            if not selected_point(point):
                continue
            key = _key(point)
            if pending[key] > 0:
                pending[key] -= 1
            yield _normalized(point)
    # Multiset subtraction preserves legitimate identical records in the log.
    for point in tail:
        _check_cancel(cancelled)
        key = _key(point)
        if pending[key] > 0:
            pending[key] -= 1
            yield point


def build_monitoring_report(
    request: ReportRequest,
    *,
    cancelled: Callable[[], bool] = lambda: False,
    progress: Callable[[str], None] = lambda _message: None,
) -> MonitoringReport:
    _check_cancel(cancelled)
    addresses = [target.address for target in request.targets]
    if not addresses or len(addresses) > 50 or len(set(addresses)) != len(addresses):
        raise ValueError("중복 없이 1~50개의 IP를 선택하세요.")
    if request.end.tzinfo is not None or (request.start and request.start.tzinfo is not None):
        raise ValueError("기존 세션과 동일한 로컬 시각을 사용해야 합니다.")
    start = request.start
    if start is None:
        bounds = session_log_bounds(request.session_path) if request.session_path is not None else None
        candidates = [point.timestamp for point in request.live_tail if point.hop_index == 0]
        if bounds:
            candidates.append(bounds[0])
        start = min(candidates).replace(microsecond=0) if candidates else request.end
    start = start.replace(microsecond=0)
    if start > request.end:
        raise ValueError("선택한 구간에 완료된 초 단위 측정 기록이 없습니다.")
    duration = max((request.end - start).total_seconds() + 1, 1)
    bin_count = min(MAX_GRAPH_BINS, max(1, math.ceil(duration)))
    accumulators = {
        target.address: _TargetAccumulator(target, [GraphBin() for _ in range(bin_count)])
        for target in request.targets
    }
    for count, point in enumerate(report_observations(request, start, cancelled), 1):
        accumulator = accumulators[point.address]
        index = min(bin_count - 1, max(0, int((point.timestamp - start).total_seconds() / duration * bin_count)))
        accumulator.bins[index].add(point)
        if point.status in IGNORED_STATUSES:
            accumulator.ignored += 1
            continue
        # A target may have changing hostname metadata; it is still one IP.
        accumulator.builder.add(replace(point, hostname=None, is_target=True))
        accumulator.first = point.timestamp if accumulator.first is None else min(accumulator.first, point.timestamp)
        accumulator.last = point.timestamp if accumulator.last is None else max(accumulator.last, point.timestamp)
        if not point.success:
            accumulator.failures.append((point.timestamp, point.status))
        if point.status == "ERROR":
            accumulator.errors += 1
        if count % 10000 == 0:
            progress(f"보고서 집계 중: {count:,}개 표본")
    rows = []
    for target in request.targets:
        _check_cancel(cancelled)
        accumulator = accumulators[target.address]
        snapshot = accumulator.builder.build(current_target=target.address).target_snapshot
        rows.append(TargetReport(
            target, snapshot, tuple(accumulator.bins), accumulator.first, accumulator.last,
            tuple(sorted(accumulator.failures)), accumulator.ignored, accumulator.errors,
        ))
    notes = [
        "저장 요청 시각 직전의 완료된 초까지 집계합니다. 저장 중 Ping 측정은 계속됩니다.",
        "기존 CSV 정밀도에 맞춰 시각은 초, 지연시간은 0.001 ms 단위로 맞췄습니다.",
        "표의 통계는 선택 구간 전체 표본 기준입니다. 그래프는 최대 900개 구간의 평균과 최소·최대를 표시합니다.",
        "빨간 구간은 응답 실패 관측, 회색은 일시중지 표본, 빈 구간은 표본 없음입니다. 빈 구간을 손실로 계산하지 않습니다.",
        "일시중지·측정 대상 없음 표본은 통계에서 제외합니다. ERROR는 측정 오류이므로 네트워크 손실로 단정하지 마세요.",
        "로그에 시간대 오프셋이 없으므로 과거 기록을 UTC로 자동 변환하지 않습니다.",
        "응답 실패만으로 회선·장비·서비스 장애 원인을 확정할 수 없습니다.",
    ]
    if request.session_path is None:
        notes.insert(0, "세션 원본 로그 없음: 메모리에 남아 있는 표본만 포함합니다. 장시간 전체 이력의 완전성을 보장하지 않습니다.")
    return MonitoringReport(request, start, request.end, tuple(rows), tuple(notes))
