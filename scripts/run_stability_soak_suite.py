from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).absolute().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.storage.atomic_write import atomic_write_path, read_text_with_retries
from scripts.soak_test import SOAK_PROFILES, evaluate_summary
from scripts.soak_test import parse_args as parse_soak_args

DEFAULT_PROFILES = ("long4h", "long8h", "long24h", "ui10", "ui20", "ui50")
FIXED_EVIDENCE_PROFILES = frozenset({"long4h", "long8h", "long24h"})
SUMMARY_MTIME_GRACE_SECONDS = 1.0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run_root = args.output_dir / args.run_id
    run_root.mkdir(parents=True, exist_ok=True)
    manifest_path = run_root / "stability_soak_suite.json"
    previous_payload = load_manifest(manifest_path) if args.resume or args.validate_only else None

    if args.validate_only:
        failures = validate_manifest(manifest_path, args.profiles)
        if args.evidence_report:
            emit_evidence_report(
                build_evidence_report(manifest_path, args.profiles, validation_failures=failures),
                report_path=args.evidence_report_path,
            )
            return 1 if failures else 0
        if failures:
            print(
                json.dumps(
                    {"manifest": str(manifest_path), "validation_failures": failures}, ensure_ascii=False, indent=2
                )
            )
            return 1
        print(manifest_path)
        return 0

    started_at = (
        str(previous_payload.get("suite_started_at"))
        if args.resume and previous_payload and previous_payload.get("suite_started_at")
        else datetime.now().isoformat(timespec="seconds")
    )
    results: list[dict[str, Any]] = (
        list(previous_payload.get("results", [])) if args.resume and previous_payload else []
    )

    for profile in args.profiles:
        if args.resume and latest_passed_result(profile, results, manifest_path=manifest_path) is not None:
            continue
        profile_result = run_profile(profile, args=args, run_root=run_root)
        results.append(profile_result)
        write_manifest(
            manifest_path,
            started_at=started_at,
            profiles=args.profiles,
            results=results,
            finished_at=None,
        )
        if profile_result["status"] == "failed" and not args.continue_on_failure:
            break

    finished_at = datetime.now().isoformat(timespec="seconds")
    write_manifest(
        manifest_path,
        started_at=started_at,
        profiles=args.profiles,
        results=results,
        finished_at=finished_at,
    )
    print(manifest_path)
    if args.dry_run:
        return 0
    validation_failures = validate_manifest(manifest_path, args.profiles)
    if validation_failures:
        print(
            json.dumps(
                {"manifest": str(manifest_path), "validation_failures": validation_failures},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    if args.evidence_report:
        emit_evidence_report(
            build_evidence_report(manifest_path, args.profiles, validation_failures=[]),
            report_path=args.evidence_report_path,
        )
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run long-duration simulated soak profiles and write one suite manifest."
    )
    parser.add_argument(
        "--profiles",
        nargs="+",
        choices=sorted(SOAK_PROFILES),
        default=list(DEFAULT_PROFILES),
        help="Profiles to run in order. Defaults to 4h/8h/24h plus UI 10/20/50 checks.",
    )
    parser.add_argument("--output-dir", type=Path, default=ROOT / "artifacts" / "stability_soak_suite")
    parser.add_argument("--run-id", default=datetime.now().strftime("%Y%m%d_%H%M%S"))
    parser.add_argument("--python", dest="python_executable", default=sys.executable)
    parser.add_argument("--continue-on-failure", action="store_true")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Reuse already passed profile results from the same run-id manifest and continue missing profiles.",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Do not run profiles. Validate the existing run-id manifest and referenced summary JSON files.",
    )
    parser.add_argument(
        "--override-duration-seconds",
        type=float,
        help="Use only for local smoke checks. Omit it for real 4h/8h/24h evidence.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Write the suite plan without starting any long-running profile.",
    )
    parser.add_argument(
        "--evidence-report",
        action="store_true",
        help="Print compact JSON evidence after validation so downloaded long-run artifacts are easy to audit.",
    )
    parser.add_argument(
        "--evidence-report-path",
        type=Path,
        help="Optional file path for the JSON evidence report. Use inside artifact folders for GitHub Actions runs.",
    )
    args = parser.parse_args(argv)
    fixed_overrides = sorted(FIXED_EVIDENCE_PROFILES.intersection(args.profiles))
    if args.override_duration_seconds is not None and fixed_overrides:
        parser.error("fixed-duration evidence profiles cannot be shortened: " + ", ".join(fixed_overrides))
    return args


def run_profile(profile: str, *, args: argparse.Namespace, run_root: Path) -> dict[str, Any]:
    profile_output_dir = run_root / profile
    profile_output_dir.mkdir(parents=True, exist_ok=True)
    command = build_profile_command(
        profile,
        output_dir=profile_output_dir,
        python_executable=args.python_executable,
        override_duration_seconds=args.override_duration_seconds,
    )
    started_at = datetime.now().isoformat(timespec="seconds")
    result: dict[str, Any] = {
        "profile": profile,
        "started_at": started_at,
        "finished_at": None,
        "command": command,
        "output_dir": str(profile_output_dir),
        "summary_json": None,
        "returncode": None,
        "status": "planned" if args.dry_run else "running",
        "thresholds": profile_thresholds(profile),
        "failures": [],
    }
    if args.dry_run:
        result["finished_at"] = datetime.now().isoformat(timespec="seconds")
        return result

    run_started_mtime = time.time()
    completed = subprocess.run(command, cwd=ROOT)
    result["returncode"] = completed.returncode
    result["finished_at"] = datetime.now().isoformat(timespec="seconds")
    summary = load_latest_summary(profile_output_dir, since=run_started_mtime)
    if summary is not None:
        result["summary_json"] = _manifest_path_value(summary["path"], run_root=run_root)
        result["failures"] = summary["data"].get("failures", [])
        result["duration_seconds"] = summary["data"].get("duration_seconds")
        result["max_ui_event_gap_seconds"] = summary["data"].get("max_ui_event_gap_seconds")
        result["max_ui_event_process_seconds"] = summary["data"].get("max_ui_event_process_seconds")
        result["memory_growth_bytes"] = summary["data"].get("memory_growth_bytes")
        result["active_threads_final"] = summary["data"].get("active_threads_final")
        result["max_active_threads"] = summary["data"].get("max_active_threads")
        result["session_log_rows"] = summary["data"].get("session_log_rows")
        result["session_log_min_expected_rows"] = summary["data"].get("session_log_min_expected_rows")
        result["session_log_row_delta"] = summary["data"].get("session_log_row_delta")
    if completed.returncode == 0 and summary is not None and not result["failures"]:
        result["status"] = "passed"
    else:
        result["status"] = "failed"
        if summary is None:
            result["failures"] = ["summary JSON was not created"]
    return result


def build_profile_command(
    profile: str,
    *,
    output_dir: Path,
    python_executable: str,
    override_duration_seconds: float | None = None,
) -> list[str]:
    if override_duration_seconds is not None and profile in FIXED_EVIDENCE_PROFILES:
        raise ValueError(f"{profile} is a fixed-duration evidence profile")
    command = [
        python_executable,
        str(Path("scripts") / "soak_test.py"),
        "--profile",
        profile,
        "--output-dir",
        str(output_dir),
    ]
    if override_duration_seconds is not None:
        command.extend(["--duration-seconds", str(override_duration_seconds)])
    return command


def profile_thresholds(profile: str) -> dict[str, object]:
    defaults = SOAK_PROFILES[profile]
    expected_duration_seconds = float(defaults["duration_seconds"])
    interval_seconds = int(defaults["interval_seconds"])
    targets = int(defaults["targets"])
    return {
        "expected_duration_seconds": expected_duration_seconds,
        "minimum_duration_seconds": expected_duration_seconds * 0.95,
        "targets": targets,
        "with_ui": bool(defaults["with_ui"]),
        "interval_seconds": interval_seconds,
        "max_active_threads": int(defaults["max_active_threads"]),
        "max_pending_ping_count": min(targets, 20) + 8,
        "max_same_target_overlap": 1,
        "max_cadence_grid_drift_seconds": max(float(interval_seconds) * 0.45, 0.05),
        "max_cadence_start_gap_seconds": max(float(interval_seconds) * 1.5, 2.0),
        "max_process_handle_growth": 256,
        "max_memory_growth_mb": float(defaults["max_memory_growth_mb"]),
        "max_cpu_percent": float(defaults["max_cpu_percent"]),
        "max_ui_event_gap_seconds": float(defaults["max_ui_event_gap_seconds"]),
        "max_ui_event_process_seconds": float(defaults["max_ui_event_process_seconds"]),
    }


def load_latest_summary(output_dir: Path, *, since: float | None = None) -> dict[str, Any] | None:
    candidates = []
    for path in output_dir.glob("soak_*_targets_*.json"):
        mtime = path.stat().st_mtime
        if since is not None and mtime + SUMMARY_MTIME_GRACE_SECONDS < since:
            continue
        candidates.append((mtime, path.name, path))
    candidates.sort()
    if not candidates:
        return None
    path = candidates[-1][2]
    return {"path": path, "data": _load_json(path)}


def load_manifest(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return _load_json(path)


def _manifest_path_value(path: Path, *, run_root: Path) -> str:
    try:
        return str(path.relative_to(run_root))
    except ValueError:
        return str(path)


def latest_result(profile: str, results: list[dict[str, Any]]) -> dict[str, Any] | None:
    matches = [result for result in results if result.get("profile") == profile]
    return matches[-1] if matches else None


def latest_passed_result(
    profile: str,
    results: list[dict[str, Any]],
    *,
    manifest_path: Path,
) -> dict[str, Any] | None:
    result = latest_result(profile, results)
    if result is None or result.get("status") != "passed":
        return None
    summary_path = resolve_recorded_path(result.get("summary_json"), manifest_path=manifest_path)
    if summary_path is None or not summary_path.exists():
        return None
    summary = _load_json(summary_path)
    if validate_summary(profile, summary):
        return None
    return result


def validate_manifest(path: Path, profiles: list[str]) -> list[str]:
    payload = load_manifest(path)
    if payload is None:
        return [f"manifest was not found: {path}"]
    return validate_manifest_payload(payload, profiles, manifest_path=path)


def validate_manifest_payload(
    payload: dict[str, Any],
    profiles: list[str],
    *,
    manifest_path: Path,
) -> list[str]:
    failures: list[str] = []
    results = list(payload.get("results", []))
    if payload.get("planned_only"):
        failures.append("manifest is dry-run only")
    if payload.get("profiles_requested") != profiles:
        failures.append(f"profiles mismatch: {payload.get('profiles_requested')} != {profiles}")
    for profile in profiles:
        result = latest_result(profile, results)
        if result is None:
            failures.append(f"{profile}: result missing")
            continue
        if result.get("status") != "passed":
            failures.append(f"{profile}: status is {result.get('status')!r}")
            continue
        summary_path = resolve_recorded_path(result.get("summary_json"), manifest_path=manifest_path)
        if summary_path is None or not summary_path.exists():
            failures.append(f"{profile}: summary JSON missing")
            continue
        summary = _load_json(summary_path)
        failures.extend(validate_summary(profile, summary))
    return failures


def validate_summary(profile: str, summary: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    profile_defaults = SOAK_PROFILES[profile]
    soak_args = parse_soak_args(["--profile", profile])
    expected_duration = float(profile_defaults["duration_seconds"])
    minimum_duration = expected_duration * 0.95
    actual_duration = float(summary.get("duration_seconds", 0.0) or 0.0)
    if summary.get("profile") != profile:
        failures.append(f"{profile}: summary profile mismatch: {summary.get('profile')!r}")
    if int(summary.get("targets", 0) or 0) != int(profile_defaults["targets"]):
        failures.append(f"{profile}: target count mismatch: {summary.get('targets')!r}")
    if bool(summary.get("with_ui")) != bool(profile_defaults["with_ui"]):
        failures.append(f"{profile}: UI mode mismatch: {summary.get('with_ui')!r}")
    if summary.get("failures"):
        failures.append(f"{profile}: summary failures: {summary['failures']}")
    if actual_duration < minimum_duration:
        failures.append(f"{profile}: duration too short: {actual_duration:.1f}s < {minimum_duration:.1f}s")
    try:
        failures.extend(f"{profile}: {failure}" for failure in evaluate_summary(summary, soak_args))
    except (KeyError, TypeError, ValueError) as exc:
        failures.append(f"{profile}: summary is missing or has invalid stability fields: {exc}")
    if int(summary.get("active_threads_final", 0) or 0) > int(profile_defaults["max_active_threads"]):
        failures.append(
            f"{profile}: final active thread count too high: "
            f"{summary.get('active_threads_final')} > {profile_defaults['max_active_threads']}"
        )
    return failures


def build_evidence_report(
    manifest_path: Path,
    profiles: list[str],
    *,
    validation_failures: list[str] | None = None,
) -> dict[str, Any]:
    payload = load_manifest(manifest_path) or {}
    results = list(payload.get("results", []))
    report: dict[str, Any] = {
        "manifest": str(manifest_path),
        "passed": bool(payload.get("passed")) and not validation_failures,
        "profiles_requested": payload.get("profiles_requested"),
        "validation_failures": list(validation_failures or []),
        "profiles": [],
    }
    profile_reports: list[dict[str, Any]] = []
    for profile in profiles:
        result = latest_result(profile, results)
        if result is None:
            profile_reports.append(
                {
                    "profile": profile,
                    "status": "missing",
                    "summary_json": None,
                    "summary_exists": False,
                    "thresholds": profile_thresholds(profile),
                    "measurements": {},
                    "checks": {},
                }
            )
            continue

        summary_path = resolve_recorded_path(result.get("summary_json"), manifest_path=manifest_path)
        summary = _load_summary_if_available(summary_path)
        thresholds = result.get("thresholds") or profile_thresholds(profile)
        measurements = _evidence_measurements(result, summary)
        profile_reports.append(
            {
                "profile": profile,
                "status": result.get("status"),
                "summary_json": str(summary_path) if summary_path is not None else None,
                "summary_exists": bool(summary_path and summary_path.exists()),
                "thresholds": thresholds,
                "measurements": measurements,
                "checks": _evidence_checks(measurements, thresholds),
            }
        )
    report["profiles"] = profile_reports
    return report


def emit_evidence_report(report: dict[str, Any], *, report_path: Path | None = None) -> None:
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if report_path is not None:
        atomic_write_path(
            report_path,
            lambda temp_path: temp_path.write_text(text + "\n", encoding="utf-8"),
        )
    print(text)


def _load_summary_if_available(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    return _load_json(path)


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(read_text_with_retries(path, encoding="utf-8"))


def _evidence_measurements(result: dict[str, Any], summary: dict[str, Any]) -> dict[str, object]:
    return {
        "evidence_schema_version": _first_present_number(result, summary, key="evidence_schema_version"),
        "platform": _first_present(result, summary, key="platform"),
        "duration_seconds": _first_present_number(result, summary, key="duration_seconds"),
        "stopped_cleanly": _first_present(result, summary, key="stopped_cleanly"),
        "max_ui_event_gap_seconds": _first_present_number(result, summary, key="max_ui_event_gap_seconds"),
        "max_ui_event_process_seconds": _first_present_number(result, summary, key="max_ui_event_process_seconds"),
        "active_threads_final": _first_present_number(result, summary, key="active_threads_final"),
        "max_active_threads": _first_present_number(result, summary, key="max_active_threads"),
        "max_active_ping_count": _first_present_number(result, summary, key="max_active_ping_count"),
        "max_pending_ping_count": _first_present_number(result, summary, key="max_pending_ping_count"),
        "memory_growth_bytes": _first_present_number(result, summary, key="memory_growth_bytes"),
        "process_handle_samples": _first_present_number(result, summary, key="process_handle_samples"),
        "process_handle_count_final": _first_present_number(result, summary, key="process_handle_count_final"),
        "max_process_handle_count": _first_present_number(result, summary, key="max_process_handle_count"),
        "process_handle_growth": _first_present_number(result, summary, key="process_handle_growth"),
        "cadence_target_count": _first_present_number(result, summary, key="cadence_target_count"),
        "cadence_probe_starts": _first_present_number(result, summary, key="cadence_probe_starts"),
        "cadence_max_abs_grid_drift_seconds": _first_present_number(
            result,
            summary,
            key="cadence_max_abs_grid_drift_seconds",
        ),
        "cadence_max_start_gap_seconds": _first_present_number(
            result,
            summary,
            key="cadence_max_start_gap_seconds",
        ),
        "max_same_target_overlap": _first_present_number(result, summary, key="max_same_target_overlap"),
        "session_resume_verified": _first_present(result, summary, key="session_resume_verified"),
        "session_loader_wait_completed": _first_present(
            result,
            summary,
            key="session_loader_wait_completed",
        ),
        "session_loader_reserved_before_cleanup": _first_present(
            result,
            summary,
            key="session_loader_reserved_before_cleanup",
        ),
        "session_loader_released_after_cleanup": _first_present(
            result,
            summary,
            key="session_loader_released_after_cleanup",
        ),
        "session_log_rows": _first_present_number(result, summary, key="session_log_rows"),
        "session_log_min_expected_rows": _first_present_number(result, summary, key="session_log_min_expected_rows"),
        "session_log_row_delta": _first_present_number(result, summary, key="session_log_row_delta"),
    }


def _evidence_checks(measurements: dict[str, object], thresholds: dict[str, object]) -> dict[str, bool | None]:
    memory_limit_mb = _number(thresholds.get("max_memory_growth_mb"))
    memory_limit_bytes = None if memory_limit_mb is None else memory_limit_mb * 1024 * 1024
    return {
        "evidence_schema_ok": _equal(measurements.get("evidence_schema_version"), 2),
        "duration_ok": _greater_equal(
            measurements.get("duration_seconds"),
            thresholds.get("minimum_duration_seconds"),
        ),
        "ui_gap_ok": _less_equal(
            measurements.get("max_ui_event_gap_seconds"),
            thresholds.get("max_ui_event_gap_seconds"),
        ),
        "ui_processing_ok": _less_equal(
            measurements.get("max_ui_event_process_seconds"),
            thresholds.get("max_ui_event_process_seconds"),
        ),
        "thread_final_ok": _less_equal(
            measurements.get("active_threads_final"),
            thresholds.get("max_active_threads"),
        ),
        "thread_peak_ok": _less_equal(
            measurements.get("max_active_threads"),
            thresholds.get("max_active_threads"),
        ),
        "active_ping_ok": _less_equal(
            measurements.get("max_active_ping_count"),
            thresholds.get("max_active_threads"),
        ),
        "pending_ping_ok": _less_equal(
            measurements.get("max_pending_ping_count"),
            thresholds.get("max_pending_ping_count"),
        ),
        "memory_growth_ok": _less_equal(measurements.get("memory_growth_bytes"), memory_limit_bytes),
        "cadence_recorded": _greater_equal(measurements.get("cadence_target_count"), 1),
        "cadence_drift_ok": _less_equal(
            measurements.get("cadence_max_abs_grid_drift_seconds"),
            thresholds.get("max_cadence_grid_drift_seconds"),
        ),
        "cadence_gap_ok": _less_equal(
            measurements.get("cadence_max_start_gap_seconds"),
            thresholds.get("max_cadence_start_gap_seconds"),
        ),
        "same_target_overlap_ok": _less_equal(
            measurements.get("max_same_target_overlap"),
            thresholds.get("max_same_target_overlap"),
        ),
        "process_handles_ok": _process_handles_ok(measurements, thresholds),
        "session_resume_ok": _is_true(measurements.get("session_resume_verified")),
        "session_loader_wait_ok": _is_true(measurements.get("session_loader_wait_completed")),
        "session_loader_ownership_ok": _all_true(
            measurements.get("session_loader_reserved_before_cleanup"),
            measurements.get("session_loader_released_after_cleanup"),
        ),
        "clean_shutdown_ok": _is_true(measurements.get("stopped_cleanly")),
        "session_log_ok": _greater_equal(
            measurements.get("session_log_rows"),
            measurements.get("session_log_min_expected_rows"),
        ),
        "session_log_delta_ok": _greater_equal(measurements.get("session_log_row_delta"), 0),
    }


def _first_present_number(*sources: dict[str, Any], key: str) -> int | float | None:
    for source in sources:
        value = source.get(key)
        number = _number(value)
        if number is not None:
            return number
    return None


def _first_present(*sources: dict[str, Any], key: str) -> object:
    for source in sources:
        if key in source:
            return source[key]
    return None


def _number(value: object) -> int | float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _greater_equal(value: object, threshold: object) -> bool | None:
    value_number = _number(value)
    threshold_number = _number(threshold)
    if value_number is None or threshold_number is None:
        return None
    return value_number >= threshold_number


def _less_equal(value: object, threshold: object) -> bool | None:
    value_number = _number(value)
    threshold_number = _number(threshold)
    if value_number is None or threshold_number is None:
        return None
    return value_number <= threshold_number


def _equal(value: object, expected: object) -> bool | None:
    if value is None:
        return None
    return value == expected


def _is_true(value: object) -> bool | None:
    if value is None:
        return None
    return value is True


def _all_true(*values: object) -> bool | None:
    if any(value is None for value in values):
        return None
    return all(value is True for value in values)


def _process_handles_ok(
    measurements: dict[str, object],
    thresholds: dict[str, object],
) -> bool | None:
    if measurements.get("platform") != "nt":
        return None
    samples_ok = _greater_equal(measurements.get("process_handle_samples"), 1)
    growth_ok = _less_equal(
        measurements.get("process_handle_growth"),
        thresholds.get("max_process_handle_growth"),
    )
    if samples_ok is None or growth_ok is None:
        return None
    return samples_ok and growth_ok


def resolve_recorded_path(value: object, *, manifest_path: Path) -> Path | None:
    if not value:
        return None
    path = Path(str(value))
    candidates = [path] if path.is_absolute() else [ROOT / path, manifest_path.parent / path]
    artifact_relative = _resolve_artifact_relative_path(path, manifest_path=manifest_path)
    if artifact_relative is not None:
        candidates.append(artifact_relative)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0] if candidates else None


def _resolve_artifact_relative_path(path: Path, *, manifest_path: Path) -> Path | None:
    run_id = manifest_path.parent.name
    parts = path.parts
    for index, part in enumerate(parts):
        if part == run_id:
            suffix = parts[index + 1 :]
            if suffix:
                return manifest_path.parent.joinpath(*suffix)
    if path.name:
        matches = sorted(manifest_path.parent.rglob(path.name))
        if matches:
            return matches[0]
    return None


def write_manifest(
    path: Path,
    *,
    started_at: str,
    profiles: list[str],
    results: list[dict[str, Any]],
    finished_at: str | None,
) -> None:
    payload = {
        "suite_started_at": started_at,
        "suite_finished_at": finished_at,
        "root": str(ROOT),
        "profiles_requested": profiles,
        "planned_only": bool(results)
        and all((latest_result(profile, results) or {}).get("status") == "planned" for profile in profiles),
        "passed": bool(results)
        and all((latest_result(profile, results) or {}).get("status") == "passed" for profile in profiles),
        "results": results,
    }
    atomic_write_path(
        path,
        lambda temp_path: temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        ),
    )


if __name__ == "__main__":
    raise SystemExit(main())
