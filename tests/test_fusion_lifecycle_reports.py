"""Regression coverage for Fusion-only reporting and clean event-writer shutdown."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from PySide6.QtGui import QCloseEvent

from app.storage.fusion_status_events import StatusChange, recent_changes
from app.ui.fusion.window import FusionMainWindow


def test_fusion_report_uses_fresh_live_range_not_stale_hidden_graph(qt_app, tmp_path, monkeypatch):
    monkeypatch.setenv("MULTIPINGCHECK_DATA_DIR", str(tmp_path))
    window = FusionMainWindow()
    try:
        origin = datetime(2026, 11, 20, 8, 0, 0)
        latest = origin + timedelta(hours=1)
        outdated = origin - timedelta(days=1)
        monkeypatch.setattr(window, "_main_graph_data_bounds", lambda: (origin, latest))
        monkeypatch.setattr(window.graph, "visible_datetime_range", lambda: (outdated, outdated))
        window._fusion_mode = "fusion"
        window.main_graph_range_seconds = 600
        window.main_graph_range_mode = "recent"
        assert window._report_visible_range("192.0.2.15") == (latest - timedelta(minutes=10), latest)
        window.main_graph_range_mode = "all"
        assert window._report_visible_range("192.0.2.15") == (origin, latest)
    finally:
        window.close()
        qt_app.processEvents()


def test_rejected_worker_close_keeps_fusion_event_writer_alive(qt_app, tmp_path, monkeypatch):
    monkeypatch.setenv("MULTIPINGCHECK_DATA_DIR", str(tmp_path))
    window = FusionMainWindow()
    writer = window._fusion_writer
    event = StatusChange(
        datetime(2026, 11, 20, 1, tzinfo=timezone.utc).isoformat(),
        "192.0.2.17", "테스트", "normal", "critical", "장애 발생"
    )
    try:
        monkeypatch.setattr(window, "_wait_for_measurement_shutdown_on_close", lambda: False)
        rejected = QCloseEvent()
        window.closeEvent(rejected)
        assert rejected.isAccepted() is False
        assert writer.isRunning(), "A rejected close must not tear down the event writer"
        assert writer.submit(event), "Events must still be accepted if the close is deferred"
        monkeypatch.setattr(window, "_wait_for_measurement_shutdown_on_close", lambda: True)
        accepted = QCloseEvent()
        window.closeEvent(accepted)
        assert accepted.isAccepted() is True
        assert not writer.isRunning()
        assert event in recent_changes(tmp_path / "fusion_status_events.sqlite3")
    finally:
        if writer.isRunning():
            assert writer.shutdown()
        qt_app.processEvents()
