# 네트워크 엔지니어 포트폴리오: 다중 대상 관측의 신뢰성

[README로 돌아가기](../README.md)

이 프로젝트는 **동일 시간대의 여러 대상 비교, ICMP 해석의 한계, timeout 부하 제어, 장기 기록 복구**를 코드와 테스트로 설명하는 사례입니다. 화면에 표시되는 지표뿐 아니라 측정 도구 자체의 지연·누락·복구 상태를 검토할 수 있습니다.

## 문제에서 구현까지

| 운영 상황 | 설계 판단과 비용 | 구현 근거 | 확인할 회귀 테스트 |
|---|---|---|---|
| 응답 없는 대상이 executor를 점유하고 정상 대상도 늦게 측정됨 | 성공 이력이 있는 대상의 capacity를 보존하고 미확인·실패 대상 동시 점유를 제한. 실패 대상은 backoff로 관측 간격이 길어질 수 있음 | [MeasurementWorker](../app/ui/worker.py) | [cadence·backoff·overlap·종료](../tests/test_worker.py) |
| 느린 응답마다 다음 측정 시작이 밀림 | 완료 시각 대신 예정 due-time grid를 기준으로 다음 slot 계산. 놓친 slot을 한꺼번에 따라잡지 않음 | [worker의 _advance_periodic_due](../app/ui/worker.py) | [worker 주기 테스트](../tests/test_worker.py), [soak 판정](../tests/test_soak_test.py) |
| 중간 hop 무응답을 실제 전달 손실로 오인 | 최종 대상과 후속 hop을 함께 비교하고 ICMP 제한 가능성을 안내. 실제 장애 원인은 추가 증거가 필요 | [analyze_path](../app/core/analyzer.py) | [고립된 중간 hop 손실·지연·지연 변동](../tests/test_metrics_analyzer.py) |
| 실시간 그래프가 장기 이력을 모두 메모리에 보관 | 최근 cache와 segmented CSV를 분리. 과거 조회에는 디스크 I/O와 loader 수명 관리가 필요 | [세션 저장](../app/storage/session_log.py), [범위 loader](../app/ui/session_observation_loader.py) | [저장·부분 손상·복구](../tests/test_validators_storage.py), [loader 상한·취소·정리](../tests/test_session_observation_loader.py) |
| 손상된 로그를 일부 읽고 정상 결과처럼 내보냄 | skip 행과 손상 상태를 구분하고 불완전한 export를 거부 | [세션 인덱스](../app/storage/session_index.py), [export worker](../app/ui/export_worker.py) | [부분 로그 export 거부·세션 복구 경고](../tests/test_validators_storage.py) |

## 합성 관측을 설명하는 방법

다음은 실제 장애 결과가 아닌 검토용 시나리오입니다.

| 가정한 관측 | 추가 확인 | 아직 말할 수 없는 것 |
|---|---|---|
| 중간 hop만 무응답이고 최종 대상은 응답 | ICMP 응답 제한, 뒤 hop의 응답과 서비스 증상 | 해당 hop이 실제 사용자 패킷을 같은 비율로 버린다는 결론 |
| 여러 대상이 같은 시각에 지연 증가 | 측정 PC 부하, 공통 경로, VPN/방화벽, 시각 정렬 | 상관관계만으로 특정 uplink를 원인으로 확정 |
| 한 대상만 연속 무응답 | 대상 정책·서비스 상태와 그 대상 경로 | ICMP 차단과 서버 중단을 Ping만으로 구별 |
| 세션 복구 중 일부 행 skip | 복구 경고, 유효 샘플 수, 누락 시각 | 남은 표본의 손실률을 전체 시간의 완전한 기록으로 인용 |

`loss_percent`는 관측 시도 중 성공하지 못한 비율이며 ICMP 정책·endpoint 상태의 영향도 포함합니다. `jitter_ms`는 RTT 표본 표준편차입니다. [지표 계산 코드](../app/core/metrics.py)와 [관측·판정 기준](OBSERVABILITY_LOGIC.md)을 함께 읽으면 지표 이름보다 정확한 해석 범위를 확인할 수 있습니다.

## 장비 없이 핵심 설계 재현

저장소 루트의 Windows PowerShell에서 Python 3.12를 사용합니다. 의존성 다운로드 이후 아래 테스트와 soak는 합성 데이터로 동작합니다. `--live`와 `--target`은 이 재현에 사용하지 않습니다.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements-dev.lock
$env:QT_QPA_PLATFORM = "offscreen"
.\.venv\Scripts\python.exe -m pytest -q tests/test_metrics_analyzer.py tests/test_worker.py tests/test_session_observation_loader.py tests/test_validators_storage.py
.\.venv\Scripts\python.exe scripts/soak_test.py --profile release --output-dir artifacts/portfolio-soak
```

`release` 프로필은 50개의 문서용 IPv4와 simulated probe를 쓰는 짧은 검증입니다. `scripts/soak_test.py`의 `SOAK_PROFILES`에서 시간·timeout 비율을 확인할 수 있습니다. 프로필 이름이 release라고 해서 이 명령이 공개 배포를 하지는 않습니다.

콘솔과 `artifacts/portfolio-soak`의 JSON에서 다음을 확인합니다.

- `failures`가 비어 있고 종료 코드가 0인지
- `stopped_cleanly`, 실제 대상별 실행 수와 `max_same_target_overlap`이 정상인지
- `session_log_rows`와 `session_log_segments`에 저장 증거가 있는지
- cadence, pending ping, queue, thread 지표가 검사 기준을 충족하는지

이 결과는 짧은 simulated workload의 증거입니다. 4/8/24시간 실행이나 Windows EXE 실행 증거로 대신할 수 없습니다. 전체 source verifier는 `.\.venv\Scripts\python.exe scripts/verify_release.py`, Windows EXE 검증은 [개발 절차](../README.md#검증-명령)를 따릅니다.

## 검토자가 확인할 증거

[CI](https://github.com/sebia1993/multi-target-ping-monitor/actions/workflows/ci.yml)에서 검토 중인 commit SHA와 `Quality, tests, and security` 및 `Windows package validation` 결과를 확인합니다. Linux source 테스트와 Windows package job의 역할은 [workflow](../.github/workflows/ci.yml)에 분리되어 있습니다.

장시간 실행은 [Stability Soak workflow](../.github/workflows/stability-soak.yml)와 [soak 절차](stability_soak.md)를 기준으로 실제 완료된 프로필·기간·원시 summary를 확인합니다. [Releases](https://github.com/sebia1993/multi-target-ping-monitor/releases)는 main과 다른 소스 시점일 수 있으므로 tag, ZIP SHA-256, release manifest와 SBOM을 함께 확인합니다.

## 남는 제약과 다음 검증

IPv4 50개까지의 Windows 관측 도구이며 ICMP가 차단된 환경의 서비스 가용성을 단독 판정할 수 없습니다. RTT 변동을 UDP/RTP 서비스 품질로 환산하지 않습니다. 로컬 macOS의 합성 테스트가 통과해도 Windows Ping/Tracert, EXE, EDR와 NIC/VPN 드라이버 호환성은 별도입니다.

다음 검증은 허가된 환경에서 baseline과 서비스 증상을 대조하고, 현재 UI의 시간 범위 전환·종료를 관찰하는 것입니다. 세션 재열기·내보내기는 현재 일반 UI에서 숨겨져 있어 별도의 개발 호출로만 재연하며, [화면 안내](USAGE_SCREENSHOTS_KO.md)에 그 경계를 기록했습니다. 공개할 때는 운영 식별자를 제거하고 [현장 검증 항목](field_verification.md)의 실제 수행 범위만 기록합니다.
