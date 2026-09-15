"""Packaged/source export smoke. Synthetic RFC 5737 data; never sends probes."""
from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtWidgets import QApplication

from app import __version__
from app.core.models import HopObservation
from app.core.observation_stats import build_focus_snapshots
from app.reporting.data import ReportRequest, TargetInfo, build_report
from app.reporting.render import chart_image, write_html, write_pdf
from app.ui.report_window import ReportMainWindow, ReportOptionsDialog


def run_smoke(output: Path) -> None:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    old = {key: os.environ.get(key) for key in ("MULTIPINGCHECK_DATA_DIR", "MULTIPINGCHECK_EXPORT_DIR")}
    with tempfile.TemporaryDirectory(prefix="multiping-report-smoke-") as directory:
        os.environ["MULTIPINGCHECK_DATA_DIR"] = directory
        os.environ["MULTIPINGCHECK_EXPORT_DIR"] = directory
        window = None
        try:
            app = QApplication.instance()
            if app is None:
                raise RuntimeError("QApplication must exist before report smoke.")
            window = ReportMainWindow()
            start = datetime(2026, 1, 1, 12)
            targets = (
                TargetInfo("192.0.2.10", "합성 게이트웨이", "정상 (합성)", 1),
                TargetInfo("198.51.100.20", "합성 서버", "주의 (합성)", 1),
                TargetInfo("203.0.113.30", "합성 원격 장비", "장애 (합성)", 1),
            )
            samples = []
            for index, target in enumerate(targets):
                for second in range(120):
                    failed = (index == 1 and 40 <= second < 47) or (index == 2 and 60 <= second < 90)
                    latency = 4 + index * 8 + 2 * math.sin(second / 8)
                    samples.append(HopObservation(start + timedelta(seconds=second), 0, target.address, None, not failed, None if failed else latency, "TIMEOUT" if failed else "OK", True))
            request = ReportRequest(targets, start + timedelta(seconds=120), start, start + timedelta(seconds=119), scope="합성 데이터 기능 검증 - 실제 측정 아님", live=tuple(samples), font_family=QApplication.font().family())
            report = build_report(request)
            assert [target.stats.sent for target in report.targets] == [120, 120, 120]
            assert [target.stats.sent - target.stats.received for target in report.targets] == [0, 7, 30]
            write_html(output / "sample-all.html", report)
            write_pdf(output / "sample-all.pdf", report)
            single = build_report(replace(request, targets=(targets[1],)))
            write_html(output / "sample-one.html", single)
            write_pdf(output / "sample-one.pdf", single)
            if not chart_image(report, report.targets[1]).save(str(output / "sample-graph.png"), "PNG"):
                raise OSError("Graph smoke PNG failed.")
            snapshots = build_focus_snapshots(samples, current_target=targets[0].address)
            window.current_targets = [target.address for target in targets]
            window.current_target = targets[0].address
            window.target_aliases = {target.address: target.alias for target in targets}
            window.target_snapshots = list(snapshots.target_snapshots)
            window.target_snapshot = snapshots.target_snapshot
            window.observations = samples
            window.target_history = [point for point in samples if point.address == targets[0].address]
            window._remember_live_graph_observations(samples)
            window._render_current_view(force_graph=True)
            window._sync_graph_panel_visibility(running=False)
            window._sync_monitoring_report_controls()
            window.command_title_label.setText("합성 데이터 검증")
            window.show()
            app.processEvents()
            if not window.grab().save(str(output / "main-window.png"), "PNG"):
                raise OSError("Main window smoke PNG failed.")
            dialog = ReportOptionsDialog(list(targets), (request.start, request.end), None, window)
            dialog.show()
            app.processEvents()
            if not dialog.grab().save(str(output / "save-dialog.png"), "PNG"):
                raise OSError("Dialog smoke PNG failed.")
            dialog.close()
            manifest = {
                "synthetic_only": True, "network_probes_sent": 0,
                "version": __version__, "platform": platform.platform(),
                "packaged": bool(getattr(sys, "frozen", False)),
                "expected_samples": [120, 120, 120], "expected_failures": [0, 7, 30],
                "files": {path.name: {"bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in sorted(output.iterdir()) if path.is_file() and path.suffix in {".html", ".pdf", ".png"}},
            }
            (output / "report-smoke-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            if window is not None:
                window.close()
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
