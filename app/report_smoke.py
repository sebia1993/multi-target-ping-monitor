"""Explicit, isolated synthetic report smoke test; never sends a network probe."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from app import __version__
from app.core.models import HopObservation
from app.reporting_data import ReportRequest, ReportTarget, build_monitoring_report
from app.reporting_render import write_monitoring_html, write_monitoring_pdf


def demo_request(count: int = 50) -> ReportRequest:
    start = datetime(2026, 9, 15, 9, 0, 0)
    targets = tuple(ReportTarget(
        f"192.0.2.{index}", f"합성 테스트 장비 {index} (실측 아님)",
        index == 50, 1, "일시중지" if index == 50 else "정상",
    ) for index in range(1, count + 1))
    points = []
    for index, target in enumerate(targets, 1):
        for second in range(120):
            paused = index == 50 and second >= 90
            failed = index % 3 == 0 and 40 <= second < 50
            success = not paused and not failed
            points.append(HopObservation(
                start + timedelta(seconds=second), 0, target.address, None, success,
                2 + index % 8 + second % 7 / 10 if success else None,
                "PAUSED" if paused else "TIMEOUT" if failed else "OK", True,
            ))
    return ReportRequest(
        targets, start, start + timedelta(seconds=119), start + timedelta(seconds=120),
        live_tail=tuple(points), scope_label="합성 데이터 검증용 · 실제 네트워크 측정 아님",
        timezone_label="합성 로컬 시각",
    )


def run_report_smoke(output: Path) -> int:
    from dataclasses import replace
    from PySide6.QtWidgets import QApplication
    from app.ui.report_window import ReportMainWindow

    app = QApplication.instance() or QApplication([])
    output.mkdir(parents=True, exist_ok=True)
    request = demo_request()
    results = {}
    for label, selected in (("single", request.targets[:1]), ("all_50", request.targets)):
        report = build_monitoring_report(replace(request, targets=selected))
        write_monitoring_html(output / f"synthetic_{label}.html", report)
        write_monitoring_pdf(output / f"synthetic_{label}.pdf", report)
        results[label] = {
            "targets": len(report.targets),
            "samples": sum(row.snapshot.sent for row in report.targets if row.snapshot),
            "failed": sum(row.snapshot.sent - row.snapshot.received for row in report.targets if row.snapshot),
        }
    previous = {key: os.environ.get(key) for key in ("MULTIPINGCHECK_DATA_DIR", "MULTIPINGCHECK_EXPORT_DIR")}
    with tempfile.TemporaryDirectory(prefix="multiping-report-smoke-") as isolated:
        os.environ["MULTIPINGCHECK_DATA_DIR"] = str(Path(isolated) / "data")
        os.environ["MULTIPINGCHECK_EXPORT_DIR"] = str(Path(isolated) / "exports")
        window = None
        try:
            window = ReportMainWindow()
            window.current_target = request.targets[0].address
            window.current_targets = [target.address for target in request.targets]
            window.target_aliases = {target.address: target.alias for target in request.targets}
            window.observations = list(request.live_tail)
            window._remember_live_graph_observations(window.observations)
            report = build_monitoring_report(request)
            window.target_snapshots = [row.snapshot for row in report.targets if row.snapshot]
            window.target_snapshot = window.target_snapshots[0]
            window.paused_target_addresses.add("192.0.2.50")
            window.resize(1400, 850)
            window.show()
            window._render_current_view(force_graph=True)
            window._set_export_enabled(True)
            window.status_label.setText("합성 데이터 · 실측 아님")
            app.processEvents()
            if not window.grab().save(str(output / "synthetic_report_ui.png"), "PNG"):
                raise OSError("UI 검증 이미지 저장 실패")
            results["ui_save_button_visible"] = window.monitor_report_button.isVisible()
            if not results["ui_save_button_visible"]:
                raise RuntimeError("보고서 저장 버튼이 일반 UI에 표시되지 않습니다.")
        finally:
            if window is not None:
                window.close()
                app.processEvents()
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    if results["single"]["targets"] != 1 or results["all_50"]["targets"] != 50:
        raise RuntimeError("보고서 대상 수 검증 실패")
    results["version"] = __version__
    results["synthetic_only"] = True
    (output / "report_validation.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(run_report_smoke(Path(sys.argv[1])))
