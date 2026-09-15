"""Exercise the actual normal-UI export path, including QThread cleanup."""
from datetime import datetime, timedelta
from time import monotonic

import pytest
from PySide6.QtWidgets import QFileDialog

from app.core.models import HopObservation
from app.ui.report_window import ReportMainWindow, ReportOptionsDialog


@pytest.mark.parametrize("kind", ["html", "pdf"])
def test_selected_ip_export_runs_in_real_thread(qt_app, tmp_path, monkeypatch, kind):
    window = ReportMainWindow()
    start = datetime.now() - timedelta(minutes=2)
    window.current_targets = ["192.0.2.1", "198.51.100.2"]
    window.current_target = window.current_targets[0]
    samples = [
        HopObservation(start + timedelta(seconds=index), 0, address, None, True, 10.0, "OK", True)
        for address in window.current_targets for index in range(60)
    ]
    window._remember_live_graph_observations(samples)
    destination = tmp_path / f"selected.{kind}"
    errors = []

    def accept_options(dialog):
        assert dialog.selected_addresses() == ("198.51.100.2",)
        dialog.scope.setCurrentIndex(1)
        dialog.kind.setCurrentIndex(dialog.kind.findData(kind))
        return ReportOptionsDialog.DialogCode.Accepted

    monkeypatch.setattr(ReportOptionsDialog, "exec", accept_options)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kwargs: (str(destination), ""))
    monkeypatch.setattr(window, "on_export_error", errors.append)
    try:
        window.open_monitoring_report("198.51.100.2")
        assert window.export_worker is not None
        assert window.worker is None  # Export must not create or restart probes.
        deadline = monotonic() + 10
        while window.export_worker is not None and monotonic() < deadline:
            qt_app.processEvents()
            if window.export_worker is not None:
                window.export_worker.wait(10)
        qt_app.processEvents()
        assert not errors
        assert window.export_worker is None
        assert destination.is_file()
        if kind == "html":
            text = destination.read_text(encoding="utf-8")
            assert "198.51.100.2" in text
            assert "192.0.2.1" not in text
            assert text.count('src="data:image/png;base64,') == 1
        else:
            assert destination.read_bytes().startswith(b"%PDF-")
        assert window.monitoring_report_button.isEnabled()
    finally:
        if window.export_worker is not None:
            window.export_worker.request_cancel()
            window.export_worker.wait(10000)
            qt_app.processEvents()
        window.close()
