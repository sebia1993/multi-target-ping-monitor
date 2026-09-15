from __future__ import annotations

import logging
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox

from app.ui.report_window import ReportMainWindow as MainWindow
from app.utils.instance_lock import (
    APP_ALREADY_RUNNING_CODE,
    InstanceLockError,
    acquire_instance_lock,
)
from app.utils.logger import configure_logging


STARTUP_ERROR_CODE = "APP_STARTUP_FAILED"
UNEXPECTED_ERROR_CODE = "APP_UNEXPECTED_ERROR"


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("MultiPingCheck")
    if "--report-smoke-dir" in sys.argv:
        index = sys.argv.index("--report-smoke-dir")
        if index + 1 >= len(sys.argv):
            return 2
        output = Path(sys.argv[index + 1])
        try:
            from app.reporting.smoke import run_smoke

            run_smoke(output)
        except Exception:
            import traceback

            output.mkdir(parents=True, exist_ok=True)
            (output / "report-smoke-error.txt").write_text(traceback.format_exc(), encoding="utf-8")
            return 1
        return 0
    try:
        instance_lock = acquire_instance_lock()
    except InstanceLockError as exc:
        title = "멀티핑체크 실행 중" if exc.code == APP_ALREADY_RUNNING_CODE else "멀티핑체크 시작 오류"
        QMessageBox.critical(
            None,
            title,
            f"{exc}\n\n오류 코드: {exc.code}",
        )
        return 2

    try:
        log_path = configure_logging()
        _install_exception_hook(log_path)
        try:
            window = MainWindow()
        except Exception:
            logging.getLogger(__name__).exception("Application startup failed")
            QMessageBox.critical(
                None,
                "멀티핑체크 시작 오류",
                "프로그램을 시작할 수 없습니다.\n"
                "프로그램을 다시 실행해도 문제가 계속되면 진단 로그를 확인하세요.\n\n"
                f"오류 코드: {STARTUP_ERROR_CODE}",
            )
            return 1
        window.show()
        return app.exec()
    finally:
        instance_lock.unlock()


def _install_exception_hook(log_path: Path | None) -> None:
    handling_error = False

    def handle_exception(exception_type, exception, traceback) -> None:
        nonlocal handling_error
        if exception_type is KeyboardInterrupt:
            sys.__excepthook__(exception_type, exception, traceback)
            return
        if handling_error:
            sys.__excepthook__(exception_type, exception, traceback)
            return
        handling_error = True
        try:
            logging.getLogger(__name__).critical(
                "Unhandled application error",
                exc_info=(exception_type, exception, traceback),
            )
            active_window = getattr(QApplication, "activeWindow", lambda: None)()
            developer_mode = getattr(active_window, "developer_mode", None)
            if developer_mode is not None:
                developer_mode.record_error(
                    str(exception),
                    error_type=exception_type.__name__,
                    application_terminated=True,
                )
            log_location = str(log_path) if log_path is not None else "로그 파일을 만들 수 없음"
            QMessageBox.critical(
                None,
                "멀티핑체크 오류",
                "프로그램 처리 중 예상하지 못한 오류가 발생했습니다.\n"
                "문제가 반복되면 프로그램을 다시 실행하고 진단 로그를 확인하세요.\n\n"
                f"오류 코드: {UNEXPECTED_ERROR_CODE}\n"
                f"오류 종류: {exception_type.__name__}\n"
                f"진단 로그: {log_location}",
            )
        except Exception:
            sys.__excepthook__(exception_type, exception, traceback)
        finally:
            handling_error = False

    sys.excepthook = handle_exception


if __name__ == "__main__":
    raise SystemExit(main())
