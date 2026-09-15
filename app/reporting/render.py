"""Self-contained HTML5 and native Qt PDF; no browser engine or network calls."""
from __future__ import annotations

import base64
from collections.abc import Callable
from datetime import datetime
from html import escape
from pathlib import Path

from PySide6.QtCore import QBuffer, QFile, QIODevice, QMarginsF, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPageLayout, QPageSize, QPdfWriter, QPen

from app.reporting.data import MonitoringReport, TargetReport
from app.storage.atomic_write import atomic_write_path

INK = "#172b4d"
MUTED = "#526579"
LINE = "#dce4ed"
BLUE = "#2563eb"
RED = "#dc2626"


def number(value: float | None, suffix: str = "") -> str:
    return "-" if value is None else f"{value:,.2f}{suffix}"


def stamp(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S") if value is not None else "-"


def metric_rows(target: TargetReport) -> list[tuple[str, str]]:
    stats = target.stats
    return [
        ("측정 표본 / 응답 / 실패", f"{stats.sent:,} / {stats.received:,} / {stats.sent - stats.received:,}"),
        ("손실률", number(target.loss, "%")),
        ("평균 / 최소 / 최대 지연 (ms)", " / ".join(number(value) for value in (stats.average, stats.minimum, stats.maximum))),
        ("선택 구간 마지막 측정 지연 (ms)", number(target.last_latency)),
        ("Timeout 표본 / 제외한 미측정 표본", f"{target.timeouts:,} / {stats.skipped:,}"),
        ("첫 측정 / 마지막 측정", f"{stamp(target.first)} / {stamp(target.last)}"),
        ("첫 실패 / 마지막 실패", f"{stamp(target.first_failure)} / {stamp(target.last_failure)}"),
    ]


def chart_image(report: MonitoringReport, target: TargetReport) -> QImage:
    image = QImage(1440, 380, QImage.Format.Format_RGB32)
    image.fill(QColor("white"))
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setFont(QFont(report.request.font_family, 13))
        left, top, width, height = 86.0, 36.0, 1300.0, 265.0
        maximum = max((bucket.maximum or 0 for bucket in target.buckets), default=0)
        ceiling = max(maximum * 1.12, 1.0)
        for tick in range(5):
            y = top + height * tick / 4
            painter.setPen(QPen(QColor(LINE), 1))
            painter.drawLine(QPointF(left, y), QPointF(left + width, y))
            painter.setPen(QColor(MUTED))
            painter.drawText(QRectF(0, y - 13, 75, 28), Qt.AlignmentFlag.AlignRight, f"{ceiling * (1 - tick / 4):.1f}")
        painter.drawText(QRectF(0, 0, 75, 26), Qt.AlignmentFlag.AlignRight, "ms")
        previous: QPointF | None = None
        count = len(target.buckets)
        for index, bucket in enumerate(target.buckets):
            x = left + (index + 0.5) * width / count
            if bucket.sent > bucket.received:
                painter.fillRect(QRectF(x - width / count / 2, top, max(width / count, 1.0), height), QColor(220, 38, 38, 28))
                painter.fillRect(QRectF(x - width / count / 2, top + height + 4, max(width / count, 1.0), 8), QColor(RED))
            average = bucket.average
            if average is None:
                previous = None
                continue
            point = QPointF(x, top + height * (1 - average / ceiling))
            painter.setPen(QPen(QColor(BLUE), 1.5))
            painter.drawLine(
                QPointF(x, top + height * (1 - (bucket.minimum or 0) / ceiling)),
                QPointF(x, top + height * (1 - (bucket.maximum or 0) / ceiling)),
            )
            painter.setPen(QPen(QColor(BLUE), 2.0))
            if previous is not None:
                painter.drawLine(previous, point)
            painter.drawEllipse(point, 1.5, 1.5)
            # Do not connect a success line through a bucket with loss/pause.
            previous = point if bucket.sent == bucket.received and not bucket.skipped else None
        painter.setPen(QColor(MUTED))
        painter.drawText(QRectF(left, 326, width / 2, 30), Qt.AlignmentFlag.AlignLeft, stamp(report.start))
        painter.drawText(QRectF(left + width / 2, 326, width / 2, 30), Qt.AlignmentFlag.AlignRight, stamp(report.end))
        if not target.stats.sent:
            painter.setPen(QColor(INK))
            painter.drawText(QRectF(left, top, width, height), Qt.AlignmentFlag.AlignCenter, "선택 구간에 측정 표본이 없습니다")
    finally:
        painter.end()
    return image


def png_bytes(image: QImage) -> bytes:
    buffer = QBuffer()
    if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
        raise OSError("이미지 버퍼를 열 수 없습니다.")
    try:
        if not image.save(buffer, "PNG"):
            raise OSError("보고서 그래프 이미지를 만들 수 없습니다.")
        return bytes(buffer.data())
    finally:
        buffer.close()


def write_html(path: Path, report: MonitoringReport, check_cancel: Callable[[], None] = lambda: None) -> None:
    def write_temp(temp: Path) -> None:
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write('''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>멀티핑체크 모니터링 보고서</title><style>
:root{color-scheme:light}*{box-sizing:border-box}body{margin:0;background:#f1f5f9;color:#172b4d;font:15px/1.6 'Malgun Gothic',sans-serif}
main{max-width:1180px;margin:32px auto;padding:0 20px}header,section{background:white;border:1px solid #dce4ed;border-radius:12px;padding:24px;margin-bottom:20px}
h1{font-size:28px;margin:0 0 8px}h2{font-size:20px;margin:0 0 12px;overflow-wrap:anywhere}p{margin:6px 0}.meta{color:#526579;font-size:13px;overflow-wrap:anywhere}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px}th,td{border-bottom:1px solid #dce4ed;padding:10px;text-align:left;vertical-align:top}th{background:#f1f5f9}td{overflow-wrap:anywhere}.summary{min-width:750px}.chart{width:100%;height:auto}.metrics th{width:38%}.empty{color:#92400e}a{color:#1d4ed8}.note{font-size:12px;color:#526579}.badge{display:inline-block;padding:3px 9px;background:#eef2ff;border-radius:5px}
@page{size:A4;margin:12mm}@media print{body{background:white;font-size:10pt}main{margin:0;padding:0;max-width:none}header,section{border:0;border-radius:0;padding:0;margin:0 0 8mm}.target{break-before:page}.scroll{overflow:visible}.summary{min-width:0}table{font-size:8pt}tr,img{break-inside:avoid}thead{display:table-header-group}}
</style></head><body><main>''')
            handle.write(f'<header><h1>멀티핑체크 모니터링 보고서</h1><p class="meta">저장 시점: {escape(report.request.captured_at.astimezone().isoformat(timespec="seconds"))}</p>')
            handle.write(f'<p class="meta">선택 구간: {stamp(report.start)} ~ {stamp(report.end)} | {escape(report.request.scope)} | 대상 {len(report.targets)}개</p>')
            handle.write('<p class="meta">인터넷 연결 없이 열 수 있는 단일 파일입니다. PDF 인쇄는 브라우저의 인쇄 메뉴를 사용하세요.</p></header>')
            handle.write('<section><h2>전체 요약</h2><div class="scroll"><table class="summary"><thead><tr><th>IP / 이름</th><th>화면 상태*</th><th>표본</th><th>실패</th><th>손실</th><th>평균 ms</th><th>최대 ms</th></tr></thead><tbody>')
            for index, target in enumerate(report.targets):
                check_cancel()
                info, stats = target.target, target.stats
                handle.write(f'<tr><td><a href="#ip-{index}">{escape(info.address)}</a><br>{escape(info.alias)}</td><td>{escape(info.screen_status or "-")}</td><td>{stats.sent:,}</td><td>{stats.sent - stats.received:,}</td><td>{number(target.loss, "%")}</td><td>{number(stats.average)}</td><td>{number(stats.maximum)}</td></tr>')
            handle.write('</tbody></table></div><p class="note">* 화면 상태는 저장 시점 값이며, 표의 통계는 선택 구간 기준입니다.</p></section>')
            for index, target in enumerate(report.targets):
                check_cancel()
                info = target.target
                handle.write(f'<section class="target" id="ip-{index}"><h2>{escape(info.address)} {escape(info.alias)}</h2>')
                handle.write(f'<p class="meta">저장 시점 화면 상태: <span class="badge">{escape(info.screen_status or "-")}</span> | 저장 시점 주기: {info.interval_seconds if info.interval_seconds is not None else "-"}초</p>')
                if not target.stats.sent:
                    handle.write('<p class="empty">선택한 구간에 측정 표본이 없습니다. 정상 또는 손실 0%로 해석하지 마세요.</p>')
                image = base64.b64encode(png_bytes(chart_image(report, target))).decode("ascii")
                handle.write(f'<img class="chart" alt="{escape(info.address, quote=True)} 응답시간 그래프" src="data:image/png;base64,{image}">')
                handle.write('<table class="metrics"><tbody>')
                for label, value in metric_rows(target):
                    handle.write(f'<tr><th>{escape(label)}</th><td>{escape(value)}</td></tr>')
                handle.write('</tbody></table></section>')
            handle.write('<section><h2>해석 및 저장 범위 안내</h2>')
            for note in report.notes:
                handle.write(f'<p class="note">{escape(note)}</p>')
            handle.write('</section></main></body></html>\n')
        check_cancel()
    atomic_write_path(path, write_temp)


def write_pdf(path: Path, report: MonitoringReport, check_cancel: Callable[[], None] = lambda: None) -> None:
    """Use an explicit QFile so Windows can atomically replace a closed file."""
    def write_temp(temp: Path) -> None:
        device = QFile(str(temp))
        if not device.open(QIODevice.OpenModeFlag.WriteOnly):
            raise OSError(device.errorString())
        writer = QPdfWriter(device)
        writer.setResolution(144)
        writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
        writer.setPageMargins(QMarginsF(12, 12, 12, 12), QPageLayout.Unit.Millimeter)
        writer.setTitle("멀티핑체크 모니터링 보고서")
        writer.setCreator("MultiPingCheck - offline monitoring report")
        painter = QPainter()
        if not painter.begin(writer):
            writer = None
            device.close()
            raise OSError("PDF 작성기를 시작할 수 없습니다.")
        page = 1
        width, height = float(writer.width()), float(writer.height())
        y = 0.0
        family = report.request.font_family

        def text(value: str, size: int = 10, bold: bool = False, gap: int = 10) -> None:
            nonlocal y
            font = QFont(family, size)
            font.setBold(bold)
            painter.setFont(font)
            painter.setPen(QColor(INK))
            flags = int(Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap)
            rect = painter.boundingRect(QRectF(0, y, width, height), flags, value)
            if y + rect.height() > height - 65:
                new_page()
            rect = QRectF(0, y, width, rect.height() + 2)
            painter.setFont(font)
            painter.setPen(QColor(INK))
            painter.drawText(rect, flags, value)
            y += rect.height() + gap

        def footer() -> None:
            painter.setFont(QFont(family, 8))
            painter.setPen(QColor(MUTED))
            painter.drawText(QRectF(0, height - 28, width, 24), Qt.AlignmentFlag.AlignRight, f"MultiPingCheck | {page}")

        def new_page() -> None:
            nonlocal page, y
            check_cancel()
            footer()
            if not writer.newPage():
                raise OSError("PDF 페이지를 추가할 수 없습니다.")
            page += 1
            y = 0

        try:
            text("멀티핑체크 모니터링 보고서", 19, True, 20)
            text(f"저장 시점: {report.request.captured_at.astimezone().isoformat(timespec='seconds')}", 9)
            text(f"선택 구간: {stamp(report.start)} ~ {stamp(report.end)}", 10)
            text(f"{report.request.scope} | 저장 대상 {len(report.targets)}개", 10)
            text("전체 요약", 14, True, 16)
            for index, target in enumerate(report.targets, 1):
                check_cancel()
                info, stats = target.target, target.stats
                # Fixed-width numerical summary; alias is not allowed to crowd numbers.
                text(f"{index:02d}. {info.address}  |  {info.screen_status or '-'}", 11, True, 4)
                text(f"표본 {stats.sent:,} / 실패 {stats.sent - stats.received:,} / 손실 {number(target.loss, '%')} / 평균 {number(stats.average)} ms / 최대 {number(stats.maximum)} ms", 9, gap=15)
            text("화면 상태는 저장 시점 값입니다. 아래 수치는 모두 선택 구간 기준입니다.", 9)
            for target in report.targets:
                new_page()
                info = target.target
                text(info.address, 19, True)
                if info.alias:
                    text(info.alias if len(info.alias) <= 512 else info.alias[:512] + " (긴 이름 생략)", 13, True)
                text(f"선택 구간: {stamp(report.start)} ~ {stamp(report.end)}", 9)
                text(f"화면 상태: {info.screen_status or '-'} | 저장 시점 주기: {info.interval_seconds if info.interval_seconds is not None else '-'}초", 9)
                image = chart_image(report, target)
                image_height = width * image.height() / image.width()
                if y + image_height > height - 65:
                    new_page()
                painter.drawImage(QRectF(0, y, width, image_height), image)
                y += image_height + 16
                for label, value in metric_rows(target):
                    text(label, 9, True, 2)
                    text(value, 11, gap=15)
                if not target.stats.sent:
                    text("선택 구간에 측정 표본이 없습니다. 정상 또는 손실 0%로 해석하지 마세요.", 10, True)
            new_page()
            text("해석 및 저장 범위 안내", 16, True, 18)
            for note in report.notes:
                text(note, 10, gap=18)
            footer()
            check_cancel()
        finally:
            painter.end()
            writer = None
            flushed = device.flush()
            error = device.errorString()
            device.close()
        if not flushed:
            raise OSError(f"PDF 저장 실패: {error}")
        check_cancel()
    atomic_write_path(path, write_temp)
