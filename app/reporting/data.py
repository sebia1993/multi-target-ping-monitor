"""Bounded report aggregation. Statistics use every sample, not graph buckets.

The existing CSV rounds timestamps to seconds and latency to 3 decimals. The
same representation is used to reconcile the immutable live tail with CSV.
No measurement worker, widget, or mutable live list is accessed here.
"""
from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from app.core.models import HopObservation

GRAPH_BUCKETS = 800
NON_PROBES = {"PAUSED", "NO_PING_TARGET"}


class ReportCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class TargetInfo:
    address: str
    alias: str = ""
    screen_status: str = ""
    interval_seconds: int | None = None


@dataclass(frozen=True)
class ReportRequest:
    targets: tuple[TargetInfo, ...]
    captured_at: datetime
    start: datetime | None
    end: datetime
    scope: str = "현재 그래프 구간"
    session_path: Path | None = None
    live: tuple[HopObservation, ...] = ()
    measurement_start: datetime | None = None
    running: bool = False
    font_family: str = "Malgun Gothic"


@dataclass
class Bucket:
    sent: int = 0
    received: int = 0
    latency_count: int = 0
    total: float = 0.0
    minimum: float | None = None
    maximum: float | None = None
    skipped: int = 0

    def add(self, point: HopObservation) -> None:
        if point.status in NON_PROBES:
            self.skipped += 1
            return
        self.sent += 1
        if not point.success:
            return
        self.received += 1
        value = point.latency_ms
        if value is None or not math.isfinite(value) or value < 0:
            return
        self.latency_count += 1
        self.total += value
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)

    @property
    def average(self) -> float | None:
        return self.total / self.latency_count if self.latency_count else None


@dataclass
class TargetReport:
    target: TargetInfo
    stats: Bucket = field(default_factory=Bucket)
    buckets: list[Bucket] = field(default_factory=lambda: [Bucket() for _ in range(GRAPH_BUCKETS)])
    timeouts: int = 0
    first: datetime | None = None
    last: datetime | None = None
    last_latency: float | None = None
    first_failure: datetime | None = None
    last_failure: datetime | None = None

    @property
    def loss(self) -> float | None:
        return (self.stats.sent - self.stats.received) * 100.0 / self.stats.sent if self.stats.sent else None


@dataclass(frozen=True)
class MonitoringReport:
    request: ReportRequest
    start: datetime
    end: datetime
    targets: tuple[TargetReport, ...]
    notes: tuple[str, ...] = ()


def _canonical(point: HopObservation) -> HopObservation:
    latency = point.latency_ms
    return replace(
        point,
        timestamp=point.timestamp.replace(microsecond=0),
        latency_ms=round(latency, 3) if latency is not None else None,
        hostname=point.hostname or None,
    )


def merged_observations(
    request: ReportRequest,
    disk: Iterable[HopObservation],
    check_cancel: Callable[[], None],
) -> Iterator[HopObservation]:
    """Use hop-0 TargetMetricTracker samples and reconcile CSV/live copies.

    Full-route mode also records the same probe as a final-hop observation.
    That representation must not be counted again, even when is_target is True.
    Repeats within the canonical stream remain legitimate separate probes.

    During measurement the current incomplete second comes only from the frozen
    live tail: a later CSV flush cannot add post-click samples in that second.
    """
    addresses = {target.address for target in request.targets}
    tail = tuple(
        _canonical(point) for point in request.live
        if point.address in addresses and point.hop_index == 0
        and point.timestamp <= request.captured_at
    )
    remaining = Counter(tail)
    current_second = request.captured_at.replace(microsecond=0)
    for index, point in enumerate(disk):
        if index % 256 == 0:
            check_cancel()
        if point.address not in addresses or point.hop_index != 0:
            continue
        if request.running and point.timestamp >= current_second:
            continue
        if point.timestamp > request.captured_at:
            continue
        normalized = _canonical(point)
        if remaining[normalized]:
            remaining[normalized] -= 1
        yield normalized
    # Typically only the unflushed tail remains. Sorting is bounded by live cache.
    for point in sorted(tail, key=lambda item: item.timestamp):
        if remaining[point]:
            check_cancel()
            remaining[point] -= 1
            yield point


def build_report(
    request: ReportRequest,
    disk: Iterable[HopObservation] = (),
    *,
    source_start: datetime | None = None,
    check_cancel: Callable[[], None] = lambda: None,
) -> MonitoringReport:
    if not request.targets or len(request.targets) > 50:
        raise ValueError("저장할 IP를 1~50개 선택하세요.")
    addresses = [target.address for target in request.targets]
    if len(set(addresses)) != len(addresses):
        raise ValueError("저장 대상 IP가 중복되었습니다.")
    starts = [value for value in (source_start, request.measurement_start) if value is not None]
    starts.extend(point.timestamp for point in request.live if point.address in addresses)
    start = request.start if request.start is not None else min(starts, default=request.end)
    end = min(request.end, request.captured_at)
    # Disk precision is one second. Use the same inclusive interval for both sources.
    start, end = start.replace(microsecond=0), end.replace(microsecond=0)
    if end < start:
        raise ValueError("종료 시각은 시작 시각 이후여야 합니다.")
    duration = max(int((end - start).total_seconds()), 1)
    bucket_count = min(GRAPH_BUCKETS, max(2, int(duration) + 1))
    reports = {target.address: TargetReport(target, buckets=[Bucket() for _ in range(bucket_count)]) for target in request.targets}
    for point in merged_observations(request, disk, check_cancel):
        if not start <= point.timestamp <= end:
            continue
        report = reports[point.address]
        # Multiply integer seconds before division to avoid false gaps from float rounding.
        elapsed = int((point.timestamp - start).total_seconds())
        index = min(elapsed * (bucket_count - 1) // duration, bucket_count - 1)
        report.buckets[index].add(point)
        report.stats.add(point)
        if point.status in NON_PROBES:
            continue
        if report.first is None or point.timestamp < report.first:
            report.first = point.timestamp
        if report.last is None or point.timestamp >= report.last:
            report.last = point.timestamp
            report.last_latency = point.latency_ms if point.success else None
        report.timeouts += int(point.status == "TIMEOUT")
        if not point.success:
            if report.first_failure is None or point.timestamp < report.first_failure:
                report.first_failure = point.timestamp
            if report.last_failure is None or point.timestamp > report.last_failure:
                report.last_failure = point.timestamp
    check_cancel()
    notes = [
        "통계는 선택 구간의 전체 표본으로 계산합니다. 그래프만 최대 800개 시간 칸으로 요약합니다.",
        "그래프의 선은 시간 칸별 평균, 세로선은 최소~최대입니다. 빨간 표시는 해당 칸의 실패 표본이며 연속 장애 시간을 뜻하지 않습니다.",
        "일시중지·미측정 표본은 송수신·손실률에서 제외합니다. 표본이 없으면 정상 또는 손실 0%로 표시하지 않습니다.",
        "CSV 저장 정밀도에 맞춰 시간은 초, 지연은 소수점 셋째 자리 기준으로 집계합니다. 시각은 측정 PC의 로컬 시간입니다.",
        "화면 상태와 측정 주기는 저장 시점의 값이며, 과거 선택 구간의 상태·주기를 의미하지 않습니다.",
        "이 파일은 저장 시점의 기록입니다. 이후 측정 결과로 자동 갱신되지 않으며 장애 원인을 확정하지 않습니다.",
    ]
    if request.session_path is None:
        notes.append("원본 세션 로그가 없어 메모리에 남아 있는 표본만 포함했습니다. 전체 이력을 보장하지 않습니다.")
    return MonitoringReport(request, start, end, tuple(reports.values()), tuple(notes))
