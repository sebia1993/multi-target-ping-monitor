"""Offline HTML5 and paginated PDF renderers sharing the same report data."""
from __future__ import annotations

from collections.abc import Callable
from html import escape
from pathlib import Path

from app import __version__
from app.reporting_data import MonitoringReport, TargetReport, _check_cancel
from app.storage.atomic_write import atomic_write_path

BLUE = "#1664aa"
RED = "#fee2e2"
GRAY = "#e5e7eb"


def number(value: float | None) -> str:
    return "-" if value is None else f"{value:.3f}"


def stamp(value) -> str:
    return value.isoformat(sep=" ", timespec="seconds") if value else "-"


def plot_geometry(row: TargetReport, width: float, height: float):
    left, top, right, bottom = 46.0, 20.0, width - 14.0, height - 30.0
    maximum = max((part.maximum for part in row.bins if part.maximum is not None), default=1.0)
    ceiling = max(maximum * 1.15, 1.0)
    step = (right - left) / max(len(row.bins), 1)
    bands, whiskers, means, links = [], [], [], []
    previous = None
    for index, part in enumerate(row.bins):
        x = left + (index + 0.5) * step
        if part.failures or part.paused:
            bands.append((left + index * step, top, max(step, 1), bottom - top, RED if part.failures else GRAY))
        if part.mean is None:
            previous = None
            continue
        y = bottom - part.mean / ceiling * (bottom - top)
        low = bottom - part.minimum / ceiling * (bottom - top)
        high = bottom - part.maximum / ceiling * (bottom - top)
        whiskers.append((x, low, x, high))
        means.append((x, y))
        clean = not part.failures and not part.paused
        if previous is not None and clean:
            links.append((*previous, x, y))
        previous = (x, y) if clean else None
    return (left, top, right, bottom, ceiling), bands, whiskers, means, links


def graph_svg(row: TargetReport) -> str:
    width, height = 1000, 240
    axes, bands, whiskers, means, links = plot_geometry(row, width, height)
    left, top, right, bottom, ceiling = axes
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-label="{escape(row.target.address)} 응답시간과 응답 실패 그래프">']
    parts.append(f'<rect width="{width}" height="{height}" fill="white"/>')
    for x, y, w, h, color in bands:
        parts.append(f'<rect x="{x:.2f}" y="{y:.2f}" width="{w:.2f}" height="{h:.2f}" fill="{color}"/>')
    for fraction in (0, 0.5, 1):
        y = bottom - fraction * (bottom - top)
        parts.append(f'<path d="M {left} {y:.2f} H {right}" stroke="#d1d5db" stroke-width="1"/>')
        parts.append(f'<text x="{left - 5}" y="{y + 4:.2f}" text-anchor="end" font-size="11" fill="#475569">{ceiling * fraction:.1f}</text>')
    for lines, color in ((whiskers, "#93b5d4"), (links, BLUE)):
        path = " ".join(f'M {a:.2f} {b:.2f} L {c:.2f} {d:.2f}' for a, b, c, d in lines)
        parts.append(f'<path d="{path}" stroke="{color}" stroke-width="1.3" fill="none"/>')
    for x, y in means:
        parts.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="1.1" fill="{BLUE}"/>')
    parts.append('<text x="4" y="12" font-size="11" fill="#475569">ms</text>')
    if not any(part.samples for part in row.bins):
        parts.append('<text x="500" y="120" text-anchor="middle" font-size="18" fill="#64748b">선택 구간에 측정 표본이 없습니다</text>')
    parts.append('</svg>')
    return "".join(parts)


def _summary_values(row: TargetReport) -> tuple[str, ...]:
    data = row.snapshot
    if data is None:
        return (row.target.address, "0", "0", "0", "-", "-", "-")
    return (
        row.target.address, str(data.sent), str(data.received), str(data.sent - data.received),
        f"{data.loss_percent:.2f}%", number(data.avg_latency_ms), number(data.max_latency_ms),
    )


def write_monitoring_html(path: Path, report: MonitoringReport, *, cancelled: Callable[[], bool] = lambda: False) -> None:
    _check_cancel(cancelled)
    parts = [
        '<!doctype html><html lang="ko"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; img-src data:; base-uri \'none\'; form-action \'none\'">',
        '<title>멀티핑체크 - Ping 관측 보고서</title><style>',
        'body{font-family:"Malgun Gothic",Arial,sans-serif;background:#f3f6fa;color:#172b42;margin:0;line-height:1.65}',
        'main{max-width:1200px;margin:auto;padding:28px}header,section{background:white;border:1px solid #dbe3ed;border-radius:12px;padding:24px;margin-bottom:20px}',
        'h1{font-size:28px;margin:0 0 10px}h2{font-size:21px;margin:0 0 8px}p{margin:5px 0}.muted{color:#52657a;font-size:13px}',
        'table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:8px;border-bottom:1px solid #dbe3ed;text-align:right}th:first-child,td:first-child{text-align:left}th{background:#edf3f9}a{color:#1664aa}svg{display:block;width:100%;height:auto}',
        '.scroll{overflow-x:auto}.metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:18px 0}.metric{padding:12px;background:#f3f6fa;border-radius:6px}.metric strong{display:block;font-size:20px}.name{overflow-wrap:anywhere}.notes{font-size:13px;color:#52657a}',
        '@media(max-width:600px){main{padding:10px}header,section{padding:14px}.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}}',
        '@media print{@page{size:A4 landscape;margin:12mm}body{background:white}main{padding:0;max-width:none}header,section{border:0;border-radius:0;padding:0}.target{break-before:page}tr{break-inside:avoid}thead{display:table-header-group}.scroll{overflow:visible}svg{max-height:65mm}}',
        '</style></head><body><main><header><p class="muted">OFFLINE OBSERVATION REPORT</p><h1>멀티핑체크 · Ping 관측 보고서</h1>',
        f'<p>대상 <strong>{len(report.targets)}개</strong> · {escape(report.request.scope_label)}</p>',
        f'<p>집계 구간: {stamp(report.start)} ~ {stamp(report.end)}</p>',
        f'<p class="muted">생성 시각: {stamp(report.request.generated_at)} · 버전 {escape(__version__)} · {escape(report.request.timezone_label)}</p>',
        '</header><section><h2>전체 IP 요약</h2><p class="muted">통계는 위 집계 구간 기준입니다. IP를 누르면 상세 그래프로 이동합니다.</p><div class="scroll"><table>',
        '<thead><tr><th>IP / 이름</th><th>송신</th><th>수신</th><th>실패</th><th>손실률</th><th>평균 ms</th><th>최대 ms</th></tr></thead><tbody>',
    ]
    for index, row in enumerate(report.targets):
        _check_cancel(cancelled)
        values = _summary_values(row)
        parts.append(f'<tr><td><a href="#ip-{index}">{escape(values[0])}</a><br><span class="muted name">{escape(row.target.alias)}</span></td>')
        parts.extend(f'<td>{escape(value)}</td>' for value in values[1:])
        parts.append('</tr>')
    parts.append('</tbody></table></div></section>')
    for index, row in enumerate(report.targets):
        _check_cancel(cancelled)
        data = row.snapshot
        parts.append(f'<section class="target" id="ip-{index}"><h2 class="name">{escape(row.target.alias or row.target.address)}</h2>')
        parts.append(f'<p>{escape(row.target.address)} · 저장 시 상태: {escape(row.target.current_state or ("일시중지" if row.target.paused else "-"))}</p>')
        parts.append(f'<p class="muted">실제 표본: {stamp(row.first_sample)} ~ {stamp(row.last_sample)} · 저장 시 설정 주기: {row.target.interval_seconds if row.target.interval_seconds is not None else "-"}초</p>')
        cards = (
            ("송신 / 수신", f"{data.sent} / {data.received}" if data else "0 / 0"),
            ("손실률", f"{data.loss_percent:.2f}%" if data else "-"),
            ("평균 지연 ms", number(data.avg_latency_ms) if data else "-"),
            ("최소 / 최대 ms", f"{number(data.min_latency_ms)} / {number(data.max_latency_ms)}" if data else "- / -"),
        )
        parts.append('<div class="metrics">')
        parts.extend(f'<div class="metric"><span>{title}</span><strong>{escape(value)}</strong></div>' for title, value in cards)
        parts.append('</div>' + graph_svg(row))
        parts.append(f'<p class="muted">{stamp(report.start)} → {stamp(report.end)} · 파랑: 평균/최소·최대 · 빨강: 응답 실패 · 회색/빈 구간: 중지/표본 없음</p>')
        parts.append(f'<p class="muted">제외한 중지·미측정 표본: {row.ignored_samples}개 · 측정 오류(ERROR): {row.error_samples}개</p>')
        if row.failure_details:
            parts.append('<h3>최근 실패 표본 (최대 20개)</h3><div class="scroll"><table><thead><tr><th>기록 시각</th><th>관측 상태</th></tr></thead><tbody>')
            parts.extend(f'<tr><td>{stamp(when)}</td><td>{escape(status)}</td></tr>' for when, status in row.failure_details)
            parts.append('</tbody></table></div>')
        parts.append('</section>')
    parts.append('<section class="notes"><h2>집계·해석 기준</h2>')
    parts.extend(f'<p>{escape(note)}</p>' for note in report.notes)
    parts.append('</section></main></body></html>')
    text = "\n".join(parts)
    def save(temp: Path) -> None:
        temp.write_text(text, encoding="utf-8")
        _check_cancel(cancelled)
    atomic_write_path(path, save)


def write_monitoring_pdf(path: Path, report: MonitoringReport, *, cancelled: Callable[[], bool] = lambda: False) -> None:
    # QPdfWriter belongs to QtGui. No QtPdf, browser, printer driver, or web service.
    from PySide6.QtCore import QMarginsF, QPointF, QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPageLayout, QPageSize, QPainter, QPdfWriter, QPen

    def save(temp: Path) -> None:
        _check_cancel(cancelled)
        writer = QPdfWriter(str(temp))
        writer.setResolution(72)
        writer.setPageLayout(QPageLayout(QPageSize(QPageSize.A4), QPageLayout.Landscape, QMarginsF(12, 12, 12, 12)))
        writer.setTitle("멀티핑체크 - Ping 관측 보고서")
        writer.setCreator(f"MultiPingCheck {__version__}")
        painter = QPainter()
        if not painter.begin(writer):
            raise OSError("PDF 출력 장치를 시작할 수 없습니다.")
        painter.setRenderHint(QPainter.Antialiasing)
        width, height = float(writer.width()), float(writer.height())
        page = 1

        def text(x, y, w, h, value, size=10, bold=False, color="#172b42", elide=False):
            font = QFont("Malgun Gothic", size)
            font.setBold(bold)
            painter.setFont(font)
            painter.setPen(QColor(color))
            value = str(value)
            if elide:
                value = QFontMetricsF(font).elidedText(value, Qt.ElideRight, w)
            painter.drawText(QRectF(x, y, w, h), Qt.AlignLeft | Qt.AlignVCenter | Qt.TextWordWrap, value)

        def footer():
            text(0, height - 22, width - 70, 20, f"MultiPingCheck {__version__} | {stamp(report.request.generated_at)}", 8, color="#52657a")
            text(width - 65, height - 22, 65, 20, f"{page} 쪽", 8)

        def new_page():
            nonlocal page
            footer()
            _check_cancel(cancelled)
            if not writer.newPage():
                raise OSError("PDF 페이지를 추가하지 못했습니다.")
            page += 1

        def heading(title):
            text(0, 0, width, 34, title, 20, True)
            text(0, 36, width, 20, f"집계 구간: {stamp(report.start)} ~ {stamp(report.end)}", 10)
            text(0, 57, width, 18, f"{report.request.timezone_label} | 선택 대상 {len(report.targets)}개 | {report.request.scope_label}", 8, color="#52657a")

        try:
            heading("멀티핑체크 · 전체 IP 요약")
            y, row_height = 88.0, 28.0
            columns = [0.27, 0.11, 0.11, 0.11, 0.12, 0.14, 0.14]
            labels = ("IP / 이름", "송신", "수신", "실패", "손실률", "평균 ms", "최대 ms")

            def table_line(values, top, header=False):
                if header:
                    painter.fillRect(QRectF(0, top, width, row_height), QColor("#edf3f9"))
                x = 0.0
                for weight, value in zip(columns, values):
                    cell_width = width * weight
                    text(x + 4, top, cell_width - 8, row_height, value, 9, header, elide=True)
                    x += cell_width
                painter.setPen(QPen(QColor("#dbe3ed"), 0.5))
                painter.drawLine(QPointF(0, top + row_height), QPointF(width, top + row_height))

            table_line(labels, y, True)
            y += row_height
            for row in report.targets:
                _check_cancel(cancelled)
                if y + row_height > height - 32:
                    new_page()
                    heading("전체 IP 요약 · 계속")
                    y = 88.0
                    table_line(labels, y, True)
                    y += row_height
                values = list(_summary_values(row))
                values[0] += f" / {row.target.alias}" if row.target.alias else ""
                table_line(values, y)
                y += row_height
            for row in report.targets:
                new_page()
                heading(row.target.address)
                text(0, 80, width, 34, row.target.alias or "IP 상세 관측", 13, True)
                data = row.snapshot
                summary = "선택 구간에 측정 표본이 없습니다."
                if data:
                    summary = f"송신 {data.sent:,} / 수신 {data.received:,} / 실패 {data.sent - data.received:,} | 손실 {data.loss_percent:.2f}% | 평균 {number(data.avg_latency_ms)} ms | 최소 {number(data.min_latency_ms)} / 최대 {number(data.max_latency_ms)} ms"
                text(0, 117, width, 30, summary, 10, True)
                text(0, 147, width, 25, f"실제 표본 {stamp(row.first_sample)} ~ {stamp(row.last_sample)} | 저장 시 상태 {row.target.current_state or '-'} | 설정 주기 {row.target.interval_seconds if row.target.interval_seconds is not None else '-'}초", 8, color="#52657a")
                painter.save()
                painter.translate(0, 174)
                axes, bands, whiskers, means, links = plot_geometry(row, width, 218)
                left, top, right, bottom, ceiling = axes
                for x, by, w, h, color in bands:
                    painter.fillRect(QRectF(x, by, w, h), QColor(color))
                for fraction in (0, 0.5, 1):
                    by = bottom - fraction * (bottom - top)
                    painter.setPen(QPen(QColor("#d1d5db"), 0.5))
                    painter.drawLine(QPointF(left, by), QPointF(right, by))
                    text(0, by - 8, 43, 16, f"{ceiling * fraction:.1f}", 8)
                for lines, color in ((whiskers, "#93b5d4"), (links, BLUE)):
                    painter.setPen(QPen(QColor(color), 0.8))
                    for a, b, c, d in lines:
                        painter.drawLine(QPointF(a, b), QPointF(c, d))
                painter.setPen(QPen(QColor(BLUE), 1.6))
                for x, by in means:
                    painter.drawPoint(QPointF(x, by))
                text(0, 0, 40, 15, "ms", 8)
                painter.restore()
                text(0, 390, width, 20, "파랑: 평균 / 최소·최대 | 빨강: 실패 관측 | 회색·빈 구간: 중지 / 표본 없음", 8, color="#52657a")
                text(0, 412, width, 20, f"제외한 중지·미측정 표본 {row.ignored_samples}개 | 측정 오류(ERROR) {row.error_samples}개", 8)
                if row.failure_details:
                    recent = " / ".join(f"{when:%H:%M:%S} {status}" for when, status in row.failure_details[-3:])
                    text(0, 436, width, 28, f"최근 실패 표본: {recent}", 8)
            new_page()
            heading("집계·해석 기준")
            y = 90.0
            for note in report.notes:
                text(0, y, width, 38, note, 10)
                y += 44
            footer()
        finally:
            painter.end()
            del writer
        _check_cancel(cancelled)
        if temp.stat().st_size < 256 or temp.read_bytes()[:5] != b"%PDF-":
            raise OSError("PDF 파일 생성 검증에 실패했습니다.")
    atomic_write_path(path, save)
