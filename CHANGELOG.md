# 변경 기록

이 문서는 사용자에게 보이는 변화와 검증 경계를 기록합니다.

## [0.2.0] - 2026-08-24

### 추가

- 단일 버전 원본(`app.__version__`)과 `v0.2.0` Release 계약
- runtime/dev 의존성의 SHA-256 hash lock
- CycloneDX 1.6 SBOM, ZIP checksum, release manifest, GitHub build/SBOM attestation
- due 대비 submit lateness, 실제 probe 시작 gap, 대상별 coverage, 동일 대상 overlap, Windows handle, 세션 resume/loader ownership 증거
- cadence 지연 상위 10개의 대상·due·submit·경과·wall-clock 원인 분석 증거
- MIT License와 비전문가용 검증·보안 경계

### 변경

- 대상 측정 주기를 ping 완료 시각 기준에서 예정 due-time 기준으로 변경
- 늦어진 측정은 누락 slot을 한 번에 몰아 실행하지 않고 다음 미래 slot으로 전진
- timeout이 많은 경우에도 확인된 정상 대상을 먼저 실행하고, 정상 대상 10개를 한 due wave에 제출할 수 있도록 15개 timeout 처리 용량과 별도의 bounded capacity를 예약
- 측정 스케줄러 QThread에 높은 우선순위를 요청해 Windows의 UI·백그라운드 작업 burst가 1초 cadence를 밀어내는 위험 완화
- 헤드리스 soak의 Qt 이벤트 처리를 호출당 10ms로 제한해 누적 이벤트 drain이 측정 스케줄러의 Python 실행 시간을 독점하지 않도록 개선
- 엄격한 GitHub-hosted cadence soak를 고정 `windows-2022` image로 실행해 `windows-latest` image migration 변수를 제거
- tracert 갱신 주기를 완료 시각이 아니라 고정 due-time 기준으로 전진해 장시간 누적 drift와 종료 경계 누락 방지
- 장시간 증거를 schema v3로 올리고 모든 대상의 실제 probe 시작 횟수를 80% slow-backoff floor로 fail-closed 검증
- PR/main 검증을 `CI` workflow의 Linux quality/security와 Windows package gate로 통합
- GitHub-hosted Windows에서 실제 14,400초 `long4h` soak를 실행할 수 있도록 증거 schema 강화
- Release가 실행 대기 중 바뀐 `main`을 따라가지 않도록 dispatch 시점의 정확한 commit SHA로 checkout 고정

### 보안

- hash-locked 설치, `pip-audit`, GitHub secret scanning/push protection, CodeQL, redacted 고신뢰 secret gate 적용
- Release asset 검증과 attestation을 배포 gate에 포함

### 검증 경계

- 자동 테스트와 장시간 soak는 synthetic probe/RFC 5737 주소를 사용합니다.
- 실제 운영망·Windows EDR·VPN/NIC 조합 및 코드서명은 자동 검증 범위가 아닙니다.
