from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from app.core.models import HopObservation
from app.reporting.data import ReportRequest, TargetInfo, build_report

BASE = datetime(2026, 1, 1, 12)
A = "192.0.2.10"


def point(second=0, *, success=True, latency=10.1234):
    return HopObservation(BASE + timedelta(seconds=second), 0, A, None, success,
                          latency if success else None, "OK" if success else "TIMEOUT", True)


def request(*addresses, **changes):
    value = ReportRequest(tuple(TargetInfo(ip) for ip in addresses or (A,)),
                          BASE + timedelta(seconds=120), BASE, BASE + timedelta(seconds=120))
    return replace(value, **changes)


@pytest.mark.parametrize("source", ["disk", "live", "overlap"])
def test_full_route_target_copy_is_not_counted_twice(source):
    # MetricsSession and TargetMetricTracker are the actual worker producers.
    from app.core.metrics import MetricsSession, TargetMetricTracker
    from app.core.models import HopInfo, PingResult

    final_hop = HopInfo(index=4, address=A, hostname="final-router", is_target=True)
    route = MetricsSession([final_hop])
    tracker = TargetMetricTracker(A)
    paired = []
    for second, status in enumerate(("OK", "TIMEOUT", "UNREACHABLE")):
        result = PingResult(A, status == "OK", 10.1234 if status == "OK" else None, status, BASE + timedelta(seconds=second))
        paired.extend((route.add_result(4, result), tracker.add_result(result)))
    disk = paired if source != "live" else ()
    live = tuple(paired) if source != "disk" else ()
    target = build_report(request(A, live=live), disk).targets[0]
    assert target.stats.sent == 3
    assert target.stats.received == 1
    assert target.stats.sent - target.stats.received == 2
    assert target.timeouts == 1
    assert target.loss == pytest.approx(200 / 3)
    assert target.stats.average == pytest.approx(10.123)
    assert sum(bucket.sent for bucket in target.buckets) == 3


def test_full_route_preserves_repeated_target_probes_in_same_second():
    canonical = point(3)
    final_hop = replace(canonical, hop_index=4, hostname="final-router")
    paired = (final_hop, canonical, final_hop, canonical)
    report = build_report(request(A, live=paired), paired)
    assert report.targets[0].stats.sent == 2


def test_full_route_does_not_double_count_unflushed_current_second():
    old = point(9.2)
    tail = point(10.2, success=False)
    future = point(10.8)
    live = (replace(old, hop_index=4), old, replace(tail, hop_index=4), tail)
    disk = [replace(item, timestamp=item.timestamp.replace(microsecond=0)) for item in (*live, future)]
    req = request(A, live=live, running=True, captured_at=BASE + timedelta(seconds=10.5), end=BASE + timedelta(seconds=10.5))
    target = build_report(req, disk).targets[0]
    assert (target.stats.sent, target.stats.received, target.timeouts) == (2, 1, 1)


def test_final_hop_only_rows_are_not_claimed_as_canonical_target_samples():
    # A route response alone is not evidence of a dedicated target probe.
    target = build_report(request(A), [replace(point(), hop_index=4)]).targets[0]
    assert target.stats.sent == 0
    assert target.loss is None
