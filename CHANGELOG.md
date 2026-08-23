# 변경 기록

이 문서는 사용자에게 보이는 변화와 검증 경계를 기록합니다.

## [0.2.0] - 2026-08-24

### 추가

- 단일 버전 원본(`app.__version__`)과 `v0.2.0` Release 계약
- runtime/dev 의존성의 SHA-256 hash lock
- CycloneDX 1.6 SBOM, ZIP checksum, release manifest, GitHub build/SBOM attestation
- cadence grid drift, missed slot, 동일 대상 overlap, Windows handle, 세션 resume/loader ownership 증거
- MIT License와 비전문가용 검증·보안 경계

### 변경

- 대상 측정 주기를 ping 완료 시각 기준에서 예정 due-time 기준으로 변경
- 늦어진 측정은 누락 slot을 한 번에 몰아 실행하지 않고 다음 미래 slot으로 전진
- PR/main 검증을 `CI` workflow의 Linux quality/security와 Windows package gate로 통합
- GitHub-hosted Windows에서 실제 14,400초 `long4h` soak를 실행할 수 있도록 증거 schema 강화
- Release가 실행 대기 중 바뀐 `main`을 따라가지 않도록 dispatch 시점의 정확한 commit SHA로 checkout 고정

### 보안

- hash-locked 설치, `pip-audit`, GitHub secret scanning/push protection, CodeQL, redacted 고신뢰 secret gate 적용
- Release asset 검증과 attestation을 배포 gate에 포함

### 검증 경계

- 자동 테스트와 장시간 soak는 synthetic probe/RFC 5737 주소를 사용합니다.
- 실제 운영망·Windows EDR·VPN/NIC 조합 및 코드서명은 자동 검증 범위가 아닙니다.
