from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from PySide6.QtWidgets import QInputDialog, QMessageBox

from app.storage.fusion_favorites import FavoriteState, FavoritesStore, renamed_group, updated_membership
from app.storage.fusion_status_events import StatusChange, StatusEventWriter, recent_changes
from app.ui.fusion.dashboard import FusionDashboard
from app.ui.fusion.models import FusionTarget
from app.ui.fusion.window import FusionMainWindow


def _target(i: int) -> FusionTarget:
    return FusionTarget(
        address=f"192.0.2.{i}", alias=f"스위치-{i}", state="normal",
        current_ms=1.0, average_ms=2.0, loss_percent=0.0, samples=120,
    )


def test_group_rename_and_membership_are_copy_on_write():
    initial = FavoriteState(groups={"연구소": ["192.0.2.1"], "태평로": []}, aliases={"192.0.2.1": "백본"})
    renamed = renamed_group(initial, "연구소", "서울R&D")
    assert list(renamed.groups) == ["서울R&D", "태평로"]
    assert initial.groups == {"연구소": ["192.0.2.1"], "태평로": []}
    added = updated_membership(renamed, "192.0.2.2", "서울R&D", "add")
    assert added.groups["서울R&D"] == ["192.0.2.1", "192.0.2.2"]
    assert renamed.groups["서울R&D"] == ["192.0.2.1"]
    moved = updated_membership(added, "192.0.2.1", "태평로", "move")
    assert moved.groups["서울R&D"] == ["192.0.2.2"]
    assert moved.groups["태평로"] == ["192.0.2.1"]
    removed = updated_membership(moved, "192.0.2.1", "태평로", "remove")
    assert removed.groups["태평로"] == []
    assert moved.groups["태평로"] == ["192.0.2.1"]
    assert moved.aliases["192.0.2.1"] == "백본"


def test_group_rename_rejects_collision_and_group_limit():
    initial = FavoriteState(groups={"A": ["192.0.2.1"], "B": []})
    with pytest.raises(ValueError):
        renamed_group(initial, "A", "B")
    with pytest.raises(ValueError):
        renamed_group(initial, "A", " ")
    with pytest.raises(ValueError):
        renamed_group(initial, "missing", "C")
    with pytest.raises(ValueError):
        updated_membership(initial, "bad-ip", "A", "add")
    filled = FavoriteState(groups={"A": [f"192.0.2.{i}" for i in range(1, 51)]})
    with pytest.raises(ValueError, match="50"):
        updated_membership(filled, "192.0.2.60", "A", "add")
    assert len(filled.groups["A"]) == 50


def test_favorites_file_round_trip_after_move(tmp_path):
    store = FavoritesStore(tmp_path / "groups.json")
    initial = FavoriteState(groups={"A": ["192.0.2.1"], "B": []})
    store.save(updated_membership(initial, "192.0.2.1", "B", "move"))
    assert store.load().groups == {"A": [], "B": ["192.0.2.1"]}


def test_legacy_event_database_migrates_and_keeps_rows(qt_app, tmp_path):
    path = tmp_path / "old.sqlite3"
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE fusion_status_changes (
      id INTEGER PRIMARY KEY AUTOINCREMENT, observed_at TEXT NOT NULL,
      address TEXT NOT NULL, alias TEXT NOT NULL, previous TEXT NOT NULL,
      current TEXT NOT NULL, description TEXT NOT NULL
    )""")
    con.execute(
        "INSERT INTO fusion_status_changes "
        "(observed_at,address,alias,previous,current,description) VALUES (?,?,?,?,?,?)",
        ("2026-11-20T00:00:00+00:00", "192.0.2.5", "기존", "normal", "critical", "장애 발생"),
    )
    con.commit()
    con.close()
    before = recent_changes(path)
    assert len(before) == 1 and before[0].duration_seconds is None
    writer = StatusEventWriter(path)
    writer.start()
    new = StatusChange("2026-11-20T00:05:00+00:00", "192.0.2.5", "기존", "critical", "normal", "응답 복구", 300)
    try:
        assert writer.submit(new)
    finally:
        assert writer.shutdown()
    assert recent_changes(path) == [new, before[0]]


def test_timeline_filters_type_search_and_time(qt_app):
    dashboard = FusionDashboard()
    try:
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=10)).isoformat()
        current = now.isoformat()
        dashboard.add_event(StatusChange(old, "192.0.2.8", "구형스위치", "normal", "critical", "장애 발생"))
        dashboard.add_event(StatusChange(current, "192.0.2.9", "신형스위치", "normal", "critical", "장애 발생"))
        dashboard.add_event(StatusChange(current, "192.0.2.9", "신형스위치", "critical", "normal", "응답 복구", 120))
        assert dashboard.events.count() == 3
        assert "지속 2분" in dashboard.events.item(0).text()
        dashboard.event_type_filter.setCurrentIndex(1)
        assert dashboard.events.count() == 2
        dashboard.event_period_filter.setCurrentIndex(1)
        assert dashboard.events.count() == 1
        dashboard.event_type_filter.setCurrentIndex(0)
        dashboard.event_search.setText("구형")
        assert dashboard.events.count() == 0
        dashboard.event_period_filter.setCurrentIndex(0)
        assert dashboard.events.count() == 1
    finally:
        dashboard.close()


def test_dashboard_fifty_targets_stays_reusable(qt_app):
    dash = FusionDashboard()
    try:
        dash.resize(1200, 850)
        dash.show()
        data = [_target(i) for i in range(1, 51)]
        dash.set_targets(data)
        qt_app.processEvents()
        first = dash.cards["192.0.2.1"]
        assert len(dash.cards) == 50
        dash.set_mode("list")
        assert dash.table.rowCount() == 50
        dash.state_filter.setCurrentIndex(1)
        assert dash.table.rowCount() == 50
        dash.set_mode("cards")
        dash.search.setText("스위치-1")
        qt_app.processEvents()
        assert len(dash.targets) == 50
        assert dash.cards["192.0.2.1"] is first
    finally:
        dash.close()


def test_window_group_rename_move_and_remove(qt_app, tmp_path, monkeypatch):
    monkeypatch.setenv("MULTIPINGCHECK_DATA_DIR", str(tmp_path))
    window = FusionMainWindow()
    try:
        window._save_fusion_favorites(FavoriteState(
            groups={"A": ["192.0.2.1"], "B": []}, aliases={}
        ))
        monkeypatch.setattr(QInputDialog, "getText", lambda *_args: ("C", True))
        window._fusion_rename_group("A")
        assert window.favorites.groups == {"C": ["192.0.2.1"], "B": []}
        assert window.fusion_dashboard._selected_group() == "C"
        monkeypatch.setattr(QInputDialog, "getItem", lambda *_args: ("이동: B (기존 그룹 모두 해제)", True))
        window._fusion_assign_to_group("192.0.2.1")
        assert window.favorites.groups == {"C": [], "B": ["192.0.2.1"]}
        monkeypatch.setattr(QInputDialog, "getItem", lambda *_args: ("해제: B", True))
        window._fusion_assign_to_group("192.0.2.1")
        assert window.favorites.groups["B"] == []
        assert FavoritesStore(tmp_path / "fusion_favorites.json").load() == window.favorites
    finally:
        window.close()


def test_failed_favorites_save_leaves_last_valid_in_memory_and_disk(qt_app, tmp_path, monkeypatch):
    monkeypatch.setenv("MULTIPINGCHECK_DATA_DIR", str(tmp_path))
    window = FusionMainWindow()
    try:
        original = FavoriteState(groups={"A": ["192.0.2.1"]})
        assert window._save_fusion_favorites(original)
        monkeypatch.setattr(window.favorites_store, "save", lambda _state: (_ for _ in ()).throw(OSError("locked")))
        monkeypatch.setattr(QMessageBox, "warning", lambda *_args: QMessageBox.StandardButton.Ok)
        assert not window._save_fusion_favorites(FavoriteState(groups={"B": ["192.0.2.2"]}))
        assert window.favorites.groups == {"A": ["192.0.2.1"]}
        assert FavoritesStore(tmp_path / "fusion_favorites.json").load().groups == {"A": ["192.0.2.1"]}
    finally:
        window.close()
