from __future__ import annotations

import json
from datetime import datetime

import pytest

from app.core.models import HopObservation
from app.storage.fusion_favorites import FavoriteState, FavoritesStore
from app.ui.fusion.models import summarize, to_fusion_target, transition_event


def test_fusion_model_keeps_existing_state_and_bounded_points():
    now = datetime(2026, 9, 15, 12, 0)
    points = [HopObservation(now, 0, "192.0.2.10", None, True, float(index), "OK", True) for index in range(90)]
    target = to_fusion_target("192.0.2.10", None, "warning", "백본", points)
    assert target.state == "warning"
    assert target.title == "백본"
    assert len(target.points) == 48
    assert target.points[0] == 42.0
    assert summarize([target])["warning"] == 1


@pytest.mark.parametrize("previous,current,expected", [
    (None, "critical", None),
    ("waiting", "critical", None),
    ("normal", "critical", "장애 발생"),
    ("critical", "normal", "응답 복구"),
    ("critical", "warning", "장애 완화"),
    ("normal", "warning", "품질 주의"),
    ("warning", "normal", "품질 정상화"),
    ("normal", "normal", None),
    ("paused", "critical", None),
])
def test_transition_discriminates_real_changes(previous, current, expected):
    assert transition_event(previous, current) == expected


def test_transition_never_mistakes_operator_pause_for_outage():
    assert transition_event("normal", "critical", manually_paused=True) is None
    assert transition_event("normal", "paused") is None


def test_favorites_atomic_round_trip_and_validation(tmp_path):
    path = tmp_path / "favorites.json"
    store = FavoritesStore(path)
    assert store.load().groups == {}
    state = FavoriteState(groups={"서울R&D": ["192.0.2.1", "192.0.2.2"]}, aliases={"192.0.2.1": "백본"})
    store.save(state)
    assert store.load() == state
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    with pytest.raises(ValueError):
        store.save(FavoriteState(groups={"위험": ["not-an-ip"]}))
    assert store.load() == state


def test_corrupted_favorites_are_not_silently_replaced(tmp_path):
    path = tmp_path / "favorites.json"
    path.write_text('{"groups":', encoding="utf-8")
    with pytest.raises(ValueError, match="원본 보존"):
        FavoritesStore(path).load()
    assert path.read_text(encoding="utf-8") == '{"groups":'
