from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from app.core.models import HopObservation
from app.reporting.data import GRAPH_BUCKETS, ReportCancelled, ReportRequest, TargetInfo, build_report

BASE = datetime(2026, 1, 1, 12, 0, 0)
A, B = "192.0.2.10", "198.51.100.20"


def point(second=0, address=A, success=True, status=None, latency=10.1234):
    return HopObservation(BASE + timedelta(seconds=second), 0, address, None, success, latency if success else None, status or ("OK" if success else "TIMEOUT"), True)


def request(*addresses, **changes):
    original = ReportRequest(tuple(TargetInfo(address) for address in addresses or (A,)), BASE + timedelta(seconds=120), BASE, BASE + timedelta(seconds=120))
    return replace(original, **changes)


def test_single_ip_does_not_include_other_targets():
    report = build_report(request(A), [point(), point(address=B, success=False)])
    assert len(report.targets) == 1
    assert report.targets[0].stats.sent == 1
    assert report.targets[0].loss == 0


def test_multiple_ips_have_independent_statistics():
    report = build_report(request(A, B), [point(), point(1, success=False), point(address=B)])
    assert [item.stats.sent for item in report.targets] == [2, 1]
    assert [item.loss for item in report.targets] == [50, 0]


def test_empty_and_paused_are_not_zero_loss():
    report = build_report(request(A, B), [point(status="PAUSED", success=False)])
    assert all(item.loss is None for item in report.targets)
    assert report.targets[0].stats.skipped == 1
    assert report.targets[0].stats.sent == 0


@pytest.mark.parametrize("status", ["TIMEOUT", "UNREACHABLE", "ERROR"])
def test_failure_types_count_as_loss(status):
    target = build_report(request(A), [point(success=False, status=status)]).targets[0]
    assert target.loss == 100
    assert target.timeouts == int(status == "TIMEOUT")


def test_inclusive_custom_range_and_future_exclusion():
    report = build_report(request(A, start=BASE + timedelta(seconds=10), end=BASE + timedelta(seconds=20)), [point(second) for second in (9, 10, 20, 21, 150)])
    assert report.targets[0].stats.sent == 2
    assert report.targets[0].first == BASE + timedelta(seconds=10)


def test_disk_live_rounding_does_not_double_count():
    disk_point = point(3, latency=10.123)
    live_point = point(3.6)
    report = build_report(request(A, live=(live_point,)), [disk_point])
    assert report.targets[0].stats.sent == 1
    assert report.targets[0].stats.average == pytest.approx(10.123)


def test_repeated_real_observations_are_not_erased():
    sample = point(3, latency=10.123)
    report = build_report(request(A, live=(sample, sample)), [sample, sample])
    assert report.targets[0].stats.sent == 2


def test_unflushed_tail_is_added_once():
    samples = [point(1), point(2), point(3)]
    report = build_report(request(A, live=tuple(samples)), samples[:2])
    assert report.targets[0].stats.sent == 3


def test_live_snapshot_does_not_gain_late_current_second_rows():
    req = request(A, captured_at=BASE + timedelta(seconds=10.5), end=BASE + timedelta(seconds=10.5), running=True, live=(point(9.2), point(10.2)))
    report = build_report(req, [point(9), point(10), point(10, latency=999), point(11)])
    assert report.targets[0].stats.sent == 2
    assert report.targets[0].stats.maximum < 999


def test_all_range_uses_original_measurement_start():
    report = build_report(request(A, start=None, measurement_start=BASE, live=(point(110),)), [point(0), point(100)])
    assert report.start == BASE
    assert report.targets[0].stats.sent == 3


def test_stats_do_not_use_downsampled_graph():
    count = 10_000
    req = request(A, captured_at=BASE + timedelta(seconds=count), end=BASE + timedelta(seconds=count))
    report = build_report(req, (point(index, success=index % 10 != 0, latency=5000 if index == 5555 else 1) for index in range(count)))
    target = report.targets[0]
    assert target.stats.sent == count
    assert target.stats.received == 9000
    assert target.loss == 10
    assert len(target.buckets) <= GRAPH_BUCKETS
    assert target.stats.maximum == 5000
    assert max(bucket.maximum or 0 for bucket in target.buckets) == 5000
    assert sum(bucket.sent for bucket in target.buckets) == count


def test_every_selected_target_including_fiftieth_is_present():
    ips = tuple(f"192.0.2.{index}" for index in range(1, 51))
    report = build_report(request(*ips), [point(address=ip) for ip in ips])
    assert len(report.targets) == 50
    assert report.targets[-1].target.address == "192.0.2.50"


def test_non_target_hops_do_not_leak_into_ip_statistics():
    sample = replace(point(), hop_index=3, is_target=False)
    assert build_report(request(A), [sample]).targets[0].stats.sent == 0


def test_invalid_range_rejected():
    with pytest.raises(ValueError):
        build_report(request(A, start=BASE + timedelta(seconds=130)))


def test_cancel_is_observed_during_stream():
    calls = []
    def cancel():
        calls.append(1)
        if len(calls) == 2:
            raise ReportCancelled()
    with pytest.raises(ReportCancelled):
        build_report(request(A), (point() for _ in range(1000)), check_cancel=cancel)


def test_out_of_order_tail_preserves_first_and_last_times():
    report = build_report(request(A, live=(point(1),)), [point(100, success=False)])
    assert report.targets[0].first == BASE + timedelta(seconds=1)
    assert report.targets[0].last == BASE + timedelta(seconds=100)
    assert report.targets[0].last_latency is None
