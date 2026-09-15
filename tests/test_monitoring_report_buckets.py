"""Regular samples must not acquire artificial graph gaps from float rounding."""
from datetime import datetime, timedelta

import pytest

from app.core.models import HopObservation
from app.reporting.data import ReportRequest, TargetInfo, build_report


@pytest.mark.parametrize("duration", [59, 119, 239, 599, 799])
def test_one_second_samples_map_to_exact_graph_bins(duration):
    start = datetime(2026, 1, 1, 12)
    end = start + timedelta(seconds=duration)
    request = ReportRequest((TargetInfo("192.0.2.1"),), end, start, end)
    samples = (
        HopObservation(start + timedelta(seconds=second), 0, "192.0.2.1", None, True, 5.0, "OK", True)
        for second in range(duration + 1)
    )
    target = build_report(request, samples).targets[0]
    assert target.stats.sent == duration + 1
    assert all(bucket.sent == 1 for bucket in target.buckets)
    assert all(bucket.average == 5.0 for bucket in target.buckets)
