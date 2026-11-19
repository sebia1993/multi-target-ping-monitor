"""Verify that performance profiles exercise the actual Fusion UI, not legacy-only MainWindow."""
from __future__ import annotations

import pytest

from app.ui.fusion.window import FusionMainWindow
from scripts import soak_test


def test_ui50_profile_can_select_fusion_window_without_shortening_thresholds():
    args = soak_test.parse_args(["--profile", "ui50", "--ui-implementation", "fusion"])
    assert args.duration_seconds == 600
    assert args.targets == 50
    assert args.with_ui
    assert args.ui_implementation == "fusion"
    assert args.max_ui_event_gap_seconds == 0.2
    assert args.max_ui_event_process_seconds == 0.2


def test_headless_rejects_misleading_fusion_flag():
    with pytest.raises(SystemExit):
        soak_test.parse_args(["--profile", "release", "--ui-implementation", "fusion"])


def test_fusion_soak_uses_actual_dashboard_and_still_allows_classic(qt_app, tmp_path, monkeypatch):
    monkeypatch.setenv("MULTIPINGCHECK_DATA_DIR", str(tmp_path / "userdata"))
    app, window = soak_test.create_application(with_ui=True, ui_implementation="fusion")
    try:
        assert app is qt_app
        assert isinstance(window, FusionMainWindow)
        assert window.fusion_dashboard.isVisible()
        assert window.fusion_mode_combo.currentData() == "fusion"

        # The new screen must not render hidden classic charts on every sample.
        renders: list[str] = []
        monkeypatch.setattr(window, "_render_graph_now", lambda: renders.append("classic"))
        window._request_graph_render(force=True)
        assert not renders
        window.fusion_mode_combo.setCurrentIndex(1)
        assert renders
        window.fusion_mode_combo.setCurrentIndex(0)
        renders.clear()
        window._request_graph_render(force=True)
        assert not renders
    finally:
        window.close()
        qt_app.processEvents()
