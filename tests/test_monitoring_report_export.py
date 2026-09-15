import re
from datetime import datetime, timedelta
from dataclasses import replace

import pytest

from app.core.models import HopObservation
from app.reporting.data import ReportCancelled, ReportRequest, TargetInfo, build_report
from app.reporting.render import write_html, write_pdf
from app.ui.report_window import MonitoringReportWorker, ReportMainWindow, ReportOptionsDialog


def demo_request(count=2):
    start = datetime(2026, 1, 1, 12)
    targets = tuple(TargetInfo(f"192.0.2.{index}", "한글 & <장비>", "대기", 1) for index in range(1, count + 1))
    samples = tuple(HopObservation(start + timedelta(seconds=index), 0, target.address, None, index % 10 != 0, 10.0 if index % 10 else None, "OK" if index % 10 else "TIMEOUT", True) for target in targets for index in range(20))
    return ReportRequest(targets, start + timedelta(seconds=20), start, start + timedelta(seconds=20), live=samples)


def test_html_is_self_contained_and_escaped(qt_app, tmp_path):
    path = tmp_path / "한글 보고서.html"
    write_html(path, build_report(demo_request()))
    text = path.read_text(encoding="utf-8")
    assert text.lower().startswith("<!doctype html>")
    assert text.count('src="data:image/png;base64,') == 2
    assert "한글 &amp; &lt;장비&gt;" in text
    assert "<장비>" not in text
    assert 'src="https:' not in text
    assert 'src="http:' not in text
    assert "Content-Security-Policy" in text
    assert "192.0.2.2" in text


def test_pdf_multiple_pages_and_korean_filename(qt_app, tmp_path):
    path = tmp_path / "한글 보고서.pdf"
    write_pdf(path, build_report(demo_request()))
    data = path.read_bytes()
    assert data.startswith(b"%PDF-")
    assert len(re.findall(rb"/Type\s*/Page\b", data)) >= 4
    # Writer must release the Windows file handle before atomic replacement.
    path.rename(tmp_path / "renamed.pdf")


def test_50_target_pdf_has_all_detail_pages(qt_app, tmp_path):
    path = tmp_path / "50-targets.pdf"
    write_pdf(path, build_report(demo_request(50)))
    assert len(re.findall(rb"/Type\s*/Page\b", path.read_bytes())) >= 52


@pytest.mark.parametrize("writer,extension", [(write_html, "html"), (write_pdf, "pdf")])
def test_cancel_preserves_previous_file(qt_app, tmp_path, writer, extension):
    path = tmp_path / f"previous.{extension}"
    path.write_bytes(b"previous-file")
    calls = []
    def cancel():
        calls.append(1)
        raise ReportCancelled()
    with pytest.raises(ReportCancelled):
        writer(path, build_report(demo_request()), cancel)
    assert path.read_bytes() == b"previous-file"
    assert list(tmp_path.iterdir()) == [path]


def test_missing_log_does_not_publish_partial_report(qt_app, tmp_path):
    req = replace(demo_request(), session_path=tmp_path / "missing.csv")
    path = tmp_path / "report.html"
    worker = MonitoringReportWorker(req, path, "html")
    errors = []
    worker.error_message.connect(errors.append)
    worker.run()
    assert errors
    assert not path.exists()


def test_corrupt_log_does_not_overwrite_report(qt_app, tmp_path):
    source = tmp_path / "samples.csv"
    source.write_text("broken,header\n1,2\n", encoding="utf-8")
    path = tmp_path / "report.html"
    path.write_bytes(b"previous-file")
    worker = MonitoringReportWorker(replace(demo_request(), session_path=source), path, "html")
    errors = []
    worker.error_message.connect(errors.append)
    worker.run()
    assert errors
    assert path.read_bytes() == b"previous-file"


def test_buttons_are_in_normal_ui_and_each_row(qt_app):
    from PySide6.QtWidgets import QToolButton
    window = ReportMainWindow()
    try:
        window.show()
        assert window.monitoring_report_button.isVisible()
        window.current_targets = ["192.0.2.1", "192.0.2.2"]
        for index, address in enumerate(window.current_targets):
            window._create_target_graph_row(address, use_primary_graph=index == 0)
            assert window.target_graph_rows[address].findChild(QToolButton, "monitoringReportButton") is not None
        window._sync_monitoring_report_controls()
        assert window.monitoring_report_button.isEnabled()
        assert len(window._report_target_info()) == 2
    finally:
        window.close()


def test_dialog_selected_ip_and_all_selection(qt_app):
    req = demo_request()
    dialog = ReportOptionsDialog(list(req.targets), (req.start, req.end), "192.0.2.2")
    try:
        assert dialog.selected_addresses() == ("192.0.2.2",)
        dialog._check_all(True)
        assert len(dialog.selected_addresses()) == 2
        dialog.scope.setCurrentIndex(2)
        assert dialog.start.isEnabled() and dialog.end.isEnabled()
    finally:
        dialog.close()
