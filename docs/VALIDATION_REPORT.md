# MultiPingCheck 검증 보고서

이 문서는 MultiPingCheck의 **자동 검증, 장시간 soak, Windows 패키지 검증, 실제 네트워크 검증**을 구분합니다.

자동 테스트 통과를 실제 모든 네트워크 환경의 호환성 증명으로 표현하지 않는 것이 원칙입니다.

## 1. 증거 수준

| 수준 | 의미 |
|---|---|
| 자동 단위/통합 테스트 | 코드의 결정 가능한 동작과 회귀 여부 확인 |
| simulated soak | 다중 timeout/장시간 부하를 실제 네트워크 없이 재현 |
| Offscreen UI soak | 대상 수 증가에 따른 Qt event gap과 UI 처리시간 측정 |
| Windows package smoke | 실제 PyInstaller EXE가 Windows runner에서 시작 가능한지 확인 |
| Field verification | 허가된 실제 네트워크에서 Ping/Tracert와 사용자 흐름 확인 |

각 수준은 서로 대체하지 않습니다.

## 2. 기본 Release Gate

`scripts/verify_release.py`는 기본적으로 외부 네트워크 없이 다음을 순서대로 검증합니다.

```text
unit tests
compileall
release policy
Qt smoke
export smoke
50-target deterministic soak
```

명령:

```powershell
python scripts\verify_release.py
```

### Unit Tests

다음 영역의 회귀를 포함합니다.

- Ping 결과 parsing
- Metric 계산
- Alert 조건·복구·중복 억제
- 다중 대상 Worker lifecycle
- timeout/backoff
- 실시간 그래프와 target row
- 시간 범위 전환
- Session Index
- segmented Session Log
- stale session recovery
- 부분 손상 세션 복구
- CSV/XLSX/Report export
- 한글 경로와 encoding
- Developer Mode의 민감정보 masking 경계

정확한 테스트 수는 코드 변경에 따라 증가하므로 고정 숫자를 README에 박아 두지 않고 최신 CI 결과를 기준으로 봅니다.

## 3. Qt Smoke

Release verifier는 `QT_QPA_PLATFORM=offscreen` 환경에서 실제 `MainWindow`를 생성합니다.

확인 항목의 예:

- Window title
- 주요 table column 계약
- 초기 Session 상태
- Developer Mode 기본 비활성
- 기본 UI 생성 단계에서 예외가 발생하지 않는지

이는 실제 모니터의 DPI/디스플레이 드라이버까지 검증하는 테스트는 아닙니다.

## 4. Export Smoke

임시 synthetic Observation을 사용해 다음 파일이 비어 있지 않게 생성되는지 확인합니다.

- CSV
- XLSX
- TXT Report

실제 운영 데이터는 사용하지 않습니다.

## 5. 50-target Release Soak

기본 Release verifier에 포함된 `release` 프로필은 simulated probe로 최대 대상 규모의 측정 동작을 빠르게 확인합니다.

목적:

- 다수의 timeout 대상이 있어도 loop가 정지하지 않는지
- pending ping이 계속 증가하지 않는지
- Worker가 종료 요청 후 정상 정리되는지
- Session Log가 실제로 생성되는지
- timeout backoff 동작이 유지되는지

## 6. 장시간 Soak Suite

`scripts/run_stability_soak_suite.py`는 다음 프로필을 관리합니다.

| 프로필 | 기본 목적 |
|---|---|
| `long4h` | 4시간 안정성 |
| `long8h` | 8시간 안정성 |
| `long24h` | 24시간 안정성 |
| `ui10` | 10-target Qt UI event gap |
| `ui20` | 20-target Qt UI event gap |
| `ui50` | 50-target Qt UI event gap |

실행 전 계획만 확인:

```powershell
python scripts\run_stability_soak_suite.py --dry-run
```

실행:

```powershell
python scripts\run_stability_soak_suite.py
```

중단된 동일 Run ID 이어서 실행:

```powershell
python scripts\run_stability_soak_suite.py --resume --run-id <RUN_ID>
```

기존 결과 재검증:

```powershell
python scripts\run_stability_soak_suite.py --validate-only --run-id <RUN_ID>
```

`validate-only`도 단순 manifest 존재 여부만 보는 것이 아니라 각 summary JSON의 실제 측정값을 threshold와 다시 비교합니다.

## 7. 주요 Soak 지표

검증 결과에는 다음 값이 사용됩니다.

| 지표 | 확인 이유 |
|---|---|
| `stopped_cleanly` | Worker/thread 종료가 정상인지 |
| `session_log_rows` | 측정 결과가 저장소에 실제 기록됐는지 |
| `session_log_segments` | segment 저장 경로가 생성됐는지 |
| `max_active_threads` | thread leak 가능성 |
| `memory_growth_bytes` | 장시간 메모리 증가 |
| `cpu_percent` | 측정 부하 |
| `max_pending_ping_count` | timeout 작업 backlog |
| `max_log_queue_depth` | Session Log writer backlog |
| `max_ui_event_gap_seconds` | UI freeze 체감 가능성 |
| `max_ui_event_process_seconds` | 한 번의 UI 작업이 event loop를 오래 점유하는지 |
| `cadence_max_abs_grid_drift_seconds` | 선택된 due 대비 executor submit 지연이 누적되는지 |
| `cadence_probe_starts`, `cadence_max_start_gap_seconds` | 정상 대상 runner의 실제 시작 횟수와 최대 gap |
| `probe_target_count`, `probe_min_starts_per_target`, `probe_max_starts_per_target` | 모든 대상이 실제 실행됐고 timeout/backoff 대상도 충분히 반복됐는지 |
| `max_same_target_overlap` | 같은 대상 ping이 중복 실행되지 않는지 |
| `process_handle_growth` | Windows handle leak 징후 |
| 세션 resume/loader lifecycle | 재개 관계와 QThread owner cleanup 경계 |

UI 10/20/50 프로필은 기본적으로 `max_ui_event_gap_seconds`와 `max_ui_event_process_seconds`가 0.2초 이하인지 확인합니다.

## 8. 실제 장애에서 발견된 회귀를 테스트로 전환

장시간 다중 대상 측정에서는 최근 범위와 `측정 전체`를 반복 전환할 때 background Session Log Loader의 QThread 수명 주기 경합이 발생할 수 있었습니다.

현재 구현은 다음 원칙으로 이를 방지합니다.

- 이전 loader 정리가 끝나기 전에 새 loader 참조를 잘못 제거하지 않음
- request generation이 다른 오래된 결과 폐기
- 늦게 도착한 `finished` callback이 현재 loader 상태를 훼손하지 않음
- 동일 lifecycle을 회귀 테스트로 유지

이 유형의 문제는 단순 unit function보다 **장시간 실행 + UI 상태 전환 + background I/O**를 함께 검증해야 발견할 수 있으므로 soak와 lifecycle test를 별도로 유지합니다.

## 9. Windows 패키지 검증

Windows EXE 빌드:

```powershell
.\build_windows_exe.ps1
```

패키지 실행 검증:

```powershell
python scripts\verify_release.py --exe
```

GitHub Actions의 통합 `CI` workflow는 Windows runner에서:

1. Source verifier
2. PyInstaller build
3. Packaged EXE smoke
4. CycloneDX runtime SBOM 생성
5. 검증된 package artifact 보관

순서로 확인할 수 있습니다.

## 10. Field Verification

실제 허가된 IPv4에 대해서만 다음 명령을 사용할 수 있습니다.

```powershell
python scripts\verify_release.py --target <FIELD_TARGET>
```

현장에서는 다음을 확인합니다.

- 다중 대상 실제 응답
- 응답 없는 대상 timeout 누적
- 30분 이상 측정 시 UI 안정성
- 대상 추가/일시중지/삭제
- 최근 범위 ↔ 측정 전체 반복 전환
- 세션 파일 생성·재열기
- EXE 종료 후 프로세스 잔류 여부

실제 IP, Hostname, 사내 장비명, 로그 원문은 이 저장소의 검증 증거로 커밋하지 않습니다.

## 11. 자동 테스트가 증명하지 않는 것

자동 검증이 모두 통과해도 다음을 자동으로 보장하지 않습니다.

- 모든 Windows 보안 정책/EDR 환경
- 모든 ICMP 차단·rate-limit 정책
- 모든 NIC/VPN driver 조합
- 모든 고지연 WAN에서 동일한 성능
- 실제 서비스 장애의 원인 판정 정확도
- 50대 초과 확장성

이 도구는 관측과 증거 수집을 보조하며, 실제 장애 결론은 네트워크 장비 로그·서비스 증상·토폴로지와 함께 판단해야 합니다.
