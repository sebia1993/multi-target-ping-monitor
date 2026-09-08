from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication

from app import __version__
from app.core.models import STATUS_OK, STATUS_TIMEOUT, HopObservation
from app.ui.latency_graph import TimelineSeries
from app.ui.main_window import MainWindow
from app.core.observation_stats import build_focus_snapshots
from app.storage.session_index import SessionIndexStore, SESSION_STATE_ARCHIVED
from app.storage.session_log import SessionLogWriter
from app.utils.app_paths import session_logs_directory


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "docs" / "images"


def _history(address: str, base_ms: float, *, timeout_every: int = 0) -> list[HopObservation]:
    start = datetime(2026, 8, 21, 19, 55, 0)
    rows: list[HopObservation] = []
    for index in range(120):
        timeout = bool(timeout_every and (index + 1) % timeout_every == 0)
        wave = ((index % 17) - 8) * 0.32
        spike = 9.0 if index in {36, 73, 98} and not timeout else 0.0
        rows.append(
            HopObservation(
                timestamp=start + timedelta(seconds=index),
                hop_index=0,
                address=address,
                hostname=None,
                success=not timeout,
                latency_ms=None if timeout else max(base_ms + wave + spike, 0.1),
                status=STATUS_TIMEOUT if timeout else STATUS_OK,
                is_target=True,
            )
        )
    return rows


def _save(window: MainWindow, path: Path, app: QApplication) -> None:
    window.resize(1460, 940)
    window.show()
    for _ in range(20):
        app.processEvents()
        time.sleep(0.01)
    window.grab()  # Resolve deferred Qt backing-store sizing before the final render.
    app.processEvents()
    image = window.grab()
    if image.isNull() or image.width() < 1200 or image.height() < 700:
        raise RuntimeError(f"문서 화면 캡처 실패: {path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not image.save(str(path), "PNG"):
        raise RuntimeError(f"PNG 저장 실패: {path}")
    window.hide()
    app.processEvents()


def _render_measurement(app: QApplication) -> None:
    window = MainWindow()
    targets = ["192.0.2.10", "198.51.100.20", "203.0.113.30"]
    histories = {
        targets[0]: _history(targets[0], 4.5),
        targets[1]: _history(targets[1], 24.0, timeout_every=17),
        targets[2]: _history(targets[2], 88.0, timeout_every=4),
    }

    observations = sorted((row for rows in histories.values() for row in rows), key=lambda row: row.timestamp)
    snapshots = build_focus_snapshots(observations, current_target=targets[0]).target_snapshots

    window.current_target = targets[0]
    window.current_targets = targets
    window.target_snapshots = snapshots
    window.target_snapshot = snapshots[0]
    window.target_aliases = {
        targets[0]: "Gateway-A",
        targets[1]: "Core-Service",
        targets[2]: "Remote-Site",
    }
    window.target_input.setPlainText("\n".join(targets))
    window._sync_target_graph_rows(snapshots)
    window._update_target_summary(snapshots[0])
    window._update_all_targets_summary(snapshots)
    window.status_label.setText("합성 관측 · 3개 IP")
    window._set_state_chip("측정", "success")
    window.target_input.hide()
    window.running_target_summary_label.setText("Gateway-A · Core-Service · Remote-Site")
    window.running_target_summary_label.show()
    window.graph_panel.show()
    window.right_panel.hide()

    window.resize(1460, 940)
    window.show()
    app.processEvents()

    colors = {
        targets[0]: "#16a34a",
        targets[1]: "#f59e0b",
        targets[2]: "#dc2626",
    }
    for address in targets:
        graph = window.target_graph_widgets.get(address)
        if graph is None:
            raise RuntimeError(f"대상 그래프 위젯 생성 실패: {address}")
        points = histories[address]
        graph.set_series(
            [
                TimelineSeries(
                    key=address,
                    label=window.target_aliases.get(address, address),
                    points=points,
                    color=colors[address],
                )
            ]
        )
        graph.set_visible_time_range(points[0].timestamp, points[-1].timestamp)

    for _ in range(5):
        app.processEvents()
    _save(window, OUTPUT_DIR / "multiping-main.png", app)
    window.close()
    app.processEvents()


def _wait(app: QApplication, predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        app.processEvents()
        if time.monotonic() >= deadline:
            raise RuntimeError("문서용 로컬 작업이 제한 시간 안에 끝나지 않았습니다.")
        time.sleep(0.01)
    app.processEvents()


def _render_saved_session(app: QApplication) -> None:
    # Exercise the real CSV/index/loader/exporter. The current simplified UI hides
    # the session panel, so documentation explicitly calls this developer replay.
    target = "203.0.113.30"
    rows = _history(target, 88.0, timeout_every=4)
    sample = session_logs_directory() / target / "2026-08" / "documentation.samples.csv"
    with SessionLogWriter(sample) as writer:
        writer.write_many(rows)
    store = SessionIndexStore.create()
    record = store.register_session(target=target, sample_path=sample, route_path=None,
        started_at=rows[0].timestamp, interval_seconds=1, measurement_mode="final_hop_only:icmp", target_count=1)
    store.add_samples(record.session_id, len(rows), rows[-1].timestamp, segments=[sample])
    store.finish_session(record.session_id, state=SESSION_STATE_ARCHIVED, ended_at=rows[-1].timestamp)
    window = MainWindow(worker_factory=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("Live probe disabled")))
    window.session_combo.setCurrentIndex(window.session_combo.findData(record.session_id))
    window.open_selected_session()
    _wait(app, lambda: window.session_open_worker is None)
    assert "세션 불러오기 완료" in window.status_label.text(), window.status_label.text()
    assert window.target_snapshot and window.target_snapshot.sent == 120
    assert window.target_snapshot.loss_percent == 25.0
    window.setWindowTitle("멀티핑체크 · 개발 검증: 합성 보관 세션 재열기")
    _save(window, OUTPUT_DIR / "multiping-session-replay.png", app)
    export_path = OUTPUT_DIR / "session-demo.csv"
    window._select_save_path = lambda *_args, **_kwargs: export_path
    window.export_selected_session()
    _wait(app, lambda: window.export_worker is None)
    assert export_path.is_file() and export_path.stat().st_size > 100
    # Verify the application export, then show only its public artifact name.
    with export_path.open(encoding="utf-8-sig", newline="") as stream:
        exported = list(csv.reader(stream))
    header = ["측정시간", "대상IP", "구분", "Hop", "성공", "지연시간", "상태"]
    exported_rows = exported[exported.index(header) + 1:]
    assert len(exported_rows) == len(rows)
    assert sum(row[4] == "False" for row in exported_rows) == 30
    window.status_label.setText("합성 CSV 저장 완료")
    window.setWindowTitle("멀티핑체크 · 개발 검증: 합성 세션 내보내기")
    _save(window, OUTPUT_DIR / "multiping-export.png", app)
    window.close()
    _wait(app, lambda: window.session_graph_loader is None)


def main() -> None:
    global OUTPUT_DIR
    parser = argparse.ArgumentParser(description="Render actual Qt widgets using isolated synthetic observations; never sends probes.")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "docs" / "images")
    args = parser.parse_args()
    OUTPUT_DIR = args.output_dir.resolve()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    source_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    with tempfile.TemporaryDirectory(prefix="multiping-docs-") as scratch:
        os.environ["MULTIPINGCHECK_DATA_DIR"] = str(Path(scratch) / "data")
        os.environ["MULTIPINGCHECK_EXPORT_DIR"] = str(Path(scratch) / "exports")
        os.environ["MULTIPINGCHECK_LEGACY_SESSION_DIR"] = str(Path(scratch) / "legacy")
        app = QApplication.instance() or QApplication(sys.argv[:1])
        app.setStyle("Fusion")
        window = MainWindow()
        window.target_input.setPlainText("192.0.2.10\n198.51.100.20\n203.0.113.30")
        _save(window, OUTPUT_DIR / "multiping-start.png", app)
        window.close()
        _render_measurement(app)
        _render_saved_session(app)
        app.processEvents()
    artifacts = [OUTPUT_DIR / name for name in ("multiping-start.png", "multiping-main.png", "multiping-session-replay.png", "multiping-export.png", "session-demo.csv")]
    for path in artifacts:
        if not path.is_file() or path.stat().st_size < 100:
            raise RuntimeError(f"Invalid documentation artifact: {path.name}")
    manifest = {"source_sha": source_sha, "product_version": __version__, "tool": "scripts/render_docs_screenshots.py",
        "os": platform.platform(), "synthetic": True, "device_connections": False,
        "limitations": "Session/export replay invokes internal application methods; these controls are hidden in the current simplified UI.",
        "artifacts": [{"file": p.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in artifacts]}
    (OUTPUT_DIR / "capture-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Rendered {len(artifacts)} isolated documentation artifacts.", flush=True)


if __name__ == "__main__":
    main()
