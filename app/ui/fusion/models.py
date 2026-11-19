"""Immutable presentation models for the Fusion dashboard.

The measurement scheduler and domain status rules remain in MainWindow/Worker.
This module must not probe the network or read session logs.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from app.core.models import HopObservation, MetricSnapshot

STATES = frozenset({"normal", "warning", "critical", "paused", "waiting"})
STATE_TEXT = {
    "normal": "정상",
    "warning": "주의",
    "critical": "장애",
    "paused": "일시중지",
    "waiting": "대기",
}


@dataclass(frozen=True)
class FusionTarget:
    address: str
    alias: str
    state: str
    current_ms: float | None
    average_ms: float | None
    loss_percent: float | None
    samples: int
    points: tuple[float | None, ...] = ()
    last_observed_at: datetime | None = None

    @property
    def title(self) -> str:
        return self.alias or self.address

    @property
    def label(self) -> str:
        return STATE_TEXT.get(self.state, "대기")


def to_fusion_target(
    address: str,
    snapshot: MetricSnapshot | None,
    state: str,
    alias: str = "",
    history: Sequence[HopObservation] = (),
) -> FusionTarget:
    """Accept the existing graph's authoritative health state, never invent thresholds."""
    if state not in STATES:
        state = "waiting"
    recent = history[-48:]
    points = tuple(float(p.latency_ms) if p.success and p.latency_ms is not None else None for p in recent)
    last = history[-1].timestamp if history else None
    return FusionTarget(
        address=address,
        alias=alias,
        state=state,
        current_ms=snapshot.current_latency_ms if snapshot else None,
        average_ms=snapshot.avg_latency_ms if snapshot else None,
        loss_percent=snapshot.recent_loss_percent if snapshot and snapshot.samples else None,
        samples=snapshot.samples if snapshot else 0,
        points=points,
        last_observed_at=last,
    )


def summarize(targets: Sequence[FusionTarget]) -> dict[str, int]:
    result = {"total": len(targets), "normal": 0, "warning": 0, "critical": 0, "paused": 0, "waiting": 0}
    for target in targets:
        result[target.state if target.state in STATES else "waiting"] += 1
    return result


def transition_event(previous: str | None, current: str, *, manually_paused: bool = False) -> str | None:
    """Emit only verified observations; manual pause/stop and the first sample are not outages."""
    if manually_paused or current in {"waiting", "paused"} or previous is None:
        return None
    if previous in {"waiting", "paused"} or previous == current:
        return None
    if previous in {"normal", "warning", "critical"}:
        if current == "critical" and previous != "critical":
            return "장애 발생"
        if previous == "critical" and current != "critical":
            return "응답 복구" if current == "normal" else "장애 완화"
        if current == "warning" and previous == "normal":
            return "품질 주의"
        if current == "normal" and previous == "warning":
            return "품질 정상화"
    return None
