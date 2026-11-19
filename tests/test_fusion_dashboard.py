from __future__ import annotations

from datetime import datetime

from PySide6.QtWidgets import QPushButton

from app.storage.fusion_status_events import StatusChange, StatusEventWriter, recent_changes
from app.ui.fusion.dashboard import FusionDashboard
from app.ui.fusion.models import FusionTarget
from app.ui.fusion.window import FusionMainWindow
from app.ui.report_window import ReportMainWindow


def _target(index: int, state: str = "normal") -> FusionTarget:
    return FusionTarget(
        address=f"192.0.2.{index}", alias=f"테스트 {index}", state=state,
        current_ms=2.0, average_ms=2.5, loss_percent=0, samples=20,
        points=(1.0, 3.0, 2.0), last_observed_at=datetime(2026, 9, 15),
    )


def test_dashboard_reuses_cards_and_filters_without_mutating_target_set(qt_app):
    dashboard = FusionDashboard()
    try:
        dashboard.resize(900, 650)
        dashboard.show()
        dashboard.set_running(True)
        dashboard.set_targets([_target(1), _target(2, "critical"), _target(3, "warning")])
        qt_app.processEvents()
        card = dashboard.cards["192.0.2.1"]
        assert dashboard.kpis["total"].text() == "3"
        assert dashboard.kpis["critical"].text() == "1"
        dashboard.search.setText("테스트 1")
        assert dashboard.count_label.text() == "1 / 3개 표시"
        dashboard.search.clear()
        dashboard.set_targets([_target(1), _target(2, "normal"), _target(3, "warning")])
        assert dashboard.cards["192.0.2.1"] is card
        assert dashboard.kpis["normal"].text() == "2"
        dashboard.set_mode("list")
        assert dashboard.table.rowCount() == 3
        dashboard.set_groups({"백본": ["192.0.2.2"]})
        dashboard.group_list.setCurrentRow(1)
        assert dashboard.count_label.text() == "1 / 3개 표시"
        assert dashboard.table.rowCount() == 1
        dashboard.toggle_theme()
        assert dashboard.theme == "light"
    finally:
        dashboard.close()


def test_dashboard_card_actions_are_wired(qt_app):
    dashboard = FusionDashboard()
    try:
        dashboard.set_running(True)
        dashboard.set_targets([_target(5)])
        clicked: list[str] = []
        dashboard.target_selected.connect(clicked.append)
        card = dashboard.cards["192.0.2.5"]
        next(button for button in card.findChildren(QPushButton) if button.text() == "상세").click()
        assert clicked == ["192.0.2.5"]
        assert card.pause_button.isEnabled()
        dashboard.set_running(False)
        assert not card.pause_button.isEnabled()
    finally:
        dashboard.close()


def test_async_events_are_persisted_and_reopened(qt_app, tmp_path):
    path = tmp_path / "events.sqlite3"
    writer = StatusEventWriter(path)
    writer.start()
    record = StatusChange("2026-09-15T02:00:00+00:00", "192.0.2.8", "테스트", "normal", "critical", "장애 발생")
    try:
        assert writer.submit(record) is True
    finally:
        assert writer.shutdown() is True
    assert recent_changes(path) == [record]
    assert recent_changes(path, limit=0) == []


def test_fusion_entrypoint_keeps_report_window_and_classic_toggle(qt_app, tmp_path, monkeypatch):
    monkeypatch.setenv("MULTIPINGCHECK_DATA_DIR", str(tmp_path))
    window = FusionMainWindow()
    try:
        assert isinstance(window, ReportMainWindow)
        assert window.fusion_mode_combo.currentData() == "fusion"
        assert window.fusion_dashboard.isHidden() is False
        assert hasattr(window, "monitoring_report_button")
        window.fusion_mode_combo.setCurrentIndex(1)
        assert window._fusion_mode == "classic"
        window.fusion_mode_combo.setCurrentIndex(0)
        assert window._fusion_mode == "fusion"
        assert window.worker is None
    finally:
        window.close()
        qt_app.processEvents()
