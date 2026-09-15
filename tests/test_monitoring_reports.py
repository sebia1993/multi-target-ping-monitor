from __future__ import annotations

import re
import time
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from PySide6.QtWidgets import QToolButton

from app.core.models import HopObservation
from app.report_smoke import demo_request
from app.reporting_data import (
    MAX_GRAPH_BINS, ReportCancelled, ReportRequest, ReportTarget,
    build_monitoring_report, closed_second,
)
from app.reporting_render import graph_svg, write_monitoring_html, write_monitoring_pdf
from app.storage.session_log import SessionLogWriter
from app.ui.report_window import MonitoringExportWorker, ReportDialog, ReportMainWindow

BASE = datetime(2026, 9, 15, 9, 0, 0)


def point(second=0, address="192.0.2.1", latency=1.0, status="OK", hop=0):
    return HopObservation(BASE + timedelta(seconds=second), hop, address, None, status == "OK", latency, status, hop == 0)


def request(points=(), addresses=("192.0.2.1",), **kwargs):
    return ReportRequest(
        tuple(ReportTarget(address) for address in addresses), BASE, BASE + timedelta(seconds=10),
        BASE + timedelta(seconds=11), live_tail=tuple(points), **kwargs,
    )


def test_selected_ip_and_final_target_only():
    report = build_monitoring_report(request([
        point(0, latency=0.0), point(1, latency=None, status="TIMEOUT"),
        point(2, address="192.0.2.2"), point(3, hop=4),
    ]))
    data = report.targets[0].snapshot
    assert len(report.targets) == 1
    assert data.sent == 2 and data.received == 1
    assert data.loss_percent == 50.0
    assert data.avg_latency_ms == 0.0
    assert report.targets[0].last_sample == BASE + timedelta(seconds=1)


def test_no_data_targets_are_not_silently_dropped():
    report = build_monitoring_report(request([point()], addresses=("192.0.2.1", "192.0.2.2")))
    assert len(report.targets) == 2
    assert report.targets[1].snapshot is None
    assert "측정 표본이 없습니다" in graph_svg(report.targets[1])


def test_pause_is_not_packet_loss_and_error_is_separate():
    report = build_monitoring_report(request([
        point(0), point(1, latency=None, status="PAUSED"),
        point(2, latency=None, status="NO_PING_TARGET"),
        point(3, latency=None, status="ERROR"),
    ]))
    row = report.targets[0]
    assert row.snapshot.sent == 2 and row.snapshot.loss_percent == 50.0
    assert row.ignored_samples == 2 and row.error_samples == 1
    assert sum(part.failures for part in row.bins) == 1


def test_disk_live_merge_uses_persisted_precision_and_multiset(tmp_path):
    original = replace(point(), timestamp=BASE.replace(microsecond=987654), latency_ms=1.2345678)
    path = tmp_path / "session.csv"
    with SessionLogWriter(path) as writer:
        writer.write_many([original, original])
    report = build_monitoring_report(request([original, original, point(2)], session_path=path))
    data = report.targets[0].snapshot
    assert data.sent == 3
    assert data.avg_latency_ms == pytest.approx((1.235 * 2 + 1) / 3)


def test_range_filters_disk_and_live_identically(tmp_path):
    path = tmp_path / "session.csv"
    points = [point(0), point(1), point(2), point(3), point(4)]
    with SessionLogWriter(path) as writer:
        writer.write_many(points)
    req = replace(request(points, session_path=path), start=BASE + timedelta(seconds=1), end=BASE + timedelta(seconds=3))
    report = build_monitoring_report(req)
    assert report.targets[0].snapshot.sent == 3
    assert report.start == BASE + timedelta(seconds=1)
    assert report.end == BASE + timedelta(seconds=3)


def test_closed_second_never_includes_open_legacy_csv_second():
    assert closed_second(BASE.replace(microsecond=999999)) == BASE - timedelta(seconds=1)
    assert closed_second(BASE) == BASE - timedelta(seconds=1)


def test_missing_or_corrupt_source_is_not_reported_as_complete(tmp_path):
    with pytest.raises(OSError):
        build_monitoring_report(request([point()], session_path=tmp_path / "missing.csv"))
    path = tmp_path / "bad.csv"
    path.write_text("broken,header\n", encoding="utf-8")
    with pytest.raises((RuntimeError, OSError, ValueError)):
        build_monitoring_report(request([point()], session_path=path))


def test_whole_session_uses_log_history_not_only_live_tail(tmp_path):
    path = tmp_path / "session.csv"
    with SessionLogWriter(path) as writer:
        writer.write_many([point(-7200), point(-3600), point(0)])
    req = replace(request([point(0)], session_path=path), start=None)
    report = build_monitoring_report(req)
    assert report.start == BASE - timedelta(seconds=7200)
    assert report.targets[0].snapshot.sent == 3
    assert len(report.targets[0].bins) == MAX_GRAPH_BINS


def test_fifty_targets_stream_without_retaining_all_samples(tmp_path, monkeypatch):
    import app.reporting_data as module
    path = tmp_path / "stream.csv"
    path.touch()
    addresses = tuple(f"192.0.2.{index}" for index in range(1, 51))
    def stream(*_args, **_kwargs):
        for second in range(2400):
            for address in addresses:
                yield point(second, address)
    monkeypatch.setattr(module, "iter_observations_in_range", stream)
    req = replace(request(addresses=addresses, session_path=path), end=BASE + timedelta(seconds=2399))
    report = build_monitoring_report(req)
    assert len(report.targets) == 50
    assert sum(row.snapshot.sent for row in report.targets) == 120000
    assert all(len(row.bins) <= MAX_GRAPH_BINS for row in report.targets)


def test_html_is_single_file_escaped_and_filtered(tmp_path):
    req = demo_request(2)
    req = replace(req, targets=(replace(req.targets[0], alias='<img src=x onerror="alert(1)">'),))
    path = tmp_path / "report.html"
    write_monitoring_html(path, build_monitoring_report(req))
    html = path.read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>")
    assert "<svg " in html and 'Content-Security-Policy' in html
    assert "&lt;img" in html and "<img" not in html
    assert "192.0.2.1" in html and "192.0.2.2" not in html
    assert "<script" not in html and '<link ' not in html


def test_pdf_has_real_pages_for_all_targets(qt_app, tmp_path):
    report = build_monitoring_report(demo_request(3))
    path = tmp_path / "report.pdf"
    write_monitoring_pdf(path, report)
    content = path.read_bytes()
    assert content.startswith(b"%PDF-")
    assert len(re.findall(rb"/Type\s*/Page\b", content)) == 5
    assert content.rstrip().endswith(b"%%EOF")


def test_cancel_during_atomic_html_save_preserves_previous_file(tmp_path):
    report = build_monitoring_report(request([point()]))
    path = tmp_path / "keep.html"
    path.write_text("previous report", encoding="utf-8")
    calls = 0
    def cancelled():
        nonlocal calls
        calls += 1
        return calls >= 4
    with pytest.raises(ReportCancelled):
        write_monitoring_html(path, report, cancelled=cancelled)
    assert path.read_text(encoding="utf-8") == "previous report"


def test_cancel_before_pdf_save_preserves_previous_file(qt_app, tmp_path):
    path = tmp_path / "keep.pdf"
    path.write_bytes(b"previous report")
    with pytest.raises(ReportCancelled):
        write_monitoring_pdf(path, build_monitoring_report(request([point()])), cancelled=lambda: True)
    assert path.read_bytes() == b"previous report"


def test_visible_main_ui_and_offscreen_target_selection(qt_app):
    window = ReportMainWindow()
    try:
        window.current_targets = [f"192.0.2.{index}" for index in range(1, 51)]
        for index, address in enumerate(window.current_targets[:3]):
            window._create_target_graph_row(address, use_primary_graph=index == 0)
        window._sync_target_graph_layout_order(window.current_targets[:3])
        window.show()
        window.set_advanced_features_visible(False)
        qt_app.processEvents()
        assert window.monitor_report_button.isVisible()
        assert len(window.findChildren(QToolButton, "targetReportButton")) == 3
        assert len(window._report_targets()) == 50
        dialog = ReportDialog(window._report_targets(), ["192.0.2.2"], (BASE, BASE + timedelta(seconds=10)), window)
        assert dialog.selected_addresses() == ["192.0.2.2"]
        dialog.select_all(True)
        assert len(dialog.selected_addresses()) == 50
        dialog.select_all(False)
        assert dialog.selected_addresses() == []
        dialog.close()
    finally:
        window.close()
        qt_app.processEvents()


def test_background_export_finishes_without_capturing_widgets(qt_app, tmp_path):
    path = tmp_path / "thread.html"
    worker = MonitoringExportWorker(demo_request(2), path, "html")
    errors = []
    worker.error_message.connect(errors.append)
    worker.start()
    deadline = time.monotonic() + 10
    while worker.isRunning() and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.005)
    assert worker.wait(1000)
    qt_app.processEvents()
    assert not errors and path.is_file()


def test_production_entrypoint_uses_report_window():
    from app.main import MainWindow
    assert MainWindow is ReportMainWindow
