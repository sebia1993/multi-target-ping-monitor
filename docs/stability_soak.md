# 장시간 안정성 검증 가이드

이 문서는 MultiPingCheck를 오래 켜 두었을 때 멈춤, 누락, 메모리 증가,
thread 잔류, 세션 로그 누락이 생기는지 확인하기 위한 절차입니다.

실제 회사 장비나 인터넷 ping 대상은 사용하지 않습니다. 모든 장시간 검증은
`scripts/soak_test.py`의 simulated probe로 실행합니다.

## 로컬 또는 MacBook에서 실행

짧은 릴리즈 smoke:

```powershell
python scripts\soak_test.py --profile release
```

전체 장시간 suite 계획만 확인:

```powershell
python scripts\run_stability_soak_suite.py --dry-run
```

4시간, 8시간, 24시간, UI 10/20/50 대상 검증:

```powershell
python scripts\run_stability_soak_suite.py
```

중간에 PC가 꺼졌거나 일부 profile만 완료된 경우:

```powershell
python scripts\run_stability_soak_suite.py --resume --run-id <RUN_ID>
```

완료된 결과가 통과 증거인지 다시 확인:

```powershell
python scripts\run_stability_soak_suite.py --validate-only --run-id <RUN_ID>
```

## GitHub Actions에서 수동 실행

GitHub 저장소에서 다음 메뉴를 사용합니다.

1. `Actions`
2. `Manual Stability Soak`
3. `Run workflow`
4. `profiles` 입력
5. runner 선택
6. `Run workflow` 클릭

추천 입력:

- 빠른 확인: `release`
- UI 멈춤 수치 확인: `ui10 ui20 ui50`
- 4시간 확인: `long4h`
- 8시간/24시간 확인: `long8h long24h`

GitHub-hosted Windows runner는 짧은 확인이나 4시간 이하 검증에 적합합니다.
8시간/24시간 검증은 `self-hosted-windows` runner 또는 로컬 PC에서 실행하는
방식을 권장합니다.

참고:

- GitHub-hosted runner job 실행 한도:
  https://docs.github.com/en/actions/reference/limits
- GitHub Actions 과금/무료 사용 기준:
  https://docs.github.com/en/actions/concepts/billing-and-usage

## 통과 기준

결과 JSON의 `failures`가 빈 배열이어야 합니다.

주요 확인 항목:

- `stopped_cleanly`: worker가 정상 종료됐는지
- `session_log_rows`: 완료된 ping 결과가 세션 로그에 누락 없이 저장됐는지
- `max_active_threads`: thread 수가 비정상적으로 늘지 않았는지
- `memory_growth_bytes`: 메모리가 계속 증가하지 않았는지
- `max_ui_event_gap_seconds`: UI 이벤트 사이 간격이 기준을 넘지 않았는지
- `max_ui_event_process_seconds`: UI 이벤트 처리 1회가 오래 걸리지 않았는지
- `max_pending_ping_count`: 대기 중 ping이 계속 쌓이지 않았는지
- `max_log_queue_depth`: 세션 로그 저장 queue가 밀리지 않았는지
- `cadence_max_abs_grid_drift_seconds`: Worker가 선택한 현재 due에서 executor submit까지의 실제 lateness(놓친 slot 지연을 반올림으로 숨기지 않음)
- `cadence_probe_starts` / `cadence_max_start_gap_seconds`: 정상 대상 runner가 실제 시작한 횟수와 시작 간 최대 gap
- `probe_target_count`: 설정한 모든 대상에서 runner 시작이 실제 관측됐는지
- `probe_min_starts_per_target` / `probe_max_starts_per_target`: 대상별 실제 시작 횟수 범위. 최소값은 5초 slow-backoff 기준 기대 횟수의 80% 이상이어야 함
- `cadence_skipped_slot_count`: 지연 중 놓친 slot을 폭주 없이 건너뛴 수
- `max_same_target_overlap`: 동일 대상 probe가 겹치지 않았는지(통과값은 `1` 이하)
- `process_handle_growth`: Windows process handle이 장시간 누적되지 않는지
- `session_resume_verified`: 종료된 세션을 원본으로 새 세션 resume 관계가 기록됐는지
- `session_loader_reserved_before_cleanup` / `session_loader_released_after_cleanup`: loader가 owner cleanup 전후 올바르게 소유되는지

`long4h` 증거는 `override_duration_seconds` 없이 정확히 14,400초를 요청해야 합니다. schema v3 필수 값이 없거나 대상 coverage/cadence/lifecycle/overlap/종료 gate가 실패하면 workflow도 실패합니다. 실패 시에도 원인 분석을 위해 artifact는 업로드하지만, 해당 결과는 Release 증거로 사용할 수 없습니다.

헤드리스 profile도 Qt 신호 queue를 실제로 처리하되 호출당 10ms budget을 적용합니다. 이렇게 하면 한 번의 무제한 event drain이 Python GIL을 오래 점유해 측정 스케줄러의 due-to-submit cadence를 왜곡할 위험을 줄이면서, 이벤트 루프 정지 여부는 계속 독립 측정합니다.

UI 10/20/50 profile은 `max_ui_event_gap_seconds`와
`max_ui_event_process_seconds`가 0.2초 이하인지 확인합니다.

## 커밋하지 말아야 할 것

다음 폴더는 검증 산출물입니다. Git 커밋에 포함하지 않습니다.

- `artifacts/`
- `release/`
- `dist/`
- `build/`
- `exports/`
- `logs/`
