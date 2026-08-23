# MultiPingCheck 개발 및 유지관리 기준

이 문서는 MultiPingCheck를 수정할 때 유지해야 할 구조·안전·검증 기준을 정리합니다.

## 1. 프로젝트 목적

MultiPingCheck는 Windows에서 여러 IPv4 대상의 지연·손실을 장시간 관측하고, 실시간 그래프와 세션 이력을 통해 간헐적인 네트워크 품질 문제를 분석하는 도구입니다.

새 기능 수보다 다음 항목을 우선합니다.

1. 장시간 안정성
2. 측정 결과 저장 무결성
3. UI 응답성
4. Worker/Thread lifecycle
5. 과거 세션 복구
6. 비식별 테스트와 재현 가능한 검증

## 2. 변경 원칙

- 정상 동작하는 기본 UI 흐름을 불필요하게 확대하지 않습니다.
- 네트워크 I/O를 UI thread에 직접 추가하지 않습니다.
- 장시간 데이터를 메모리에 무제한 누적하지 않습니다.
- 실제 사내 네트워크나 고객 데이터를 자동 테스트 의존성으로 만들지 않습니다.
- 오류를 숨이거나 정상 결과로 치환하지 않습니다.
- 세션 손상·누락·부분 복구 사실은 안정적인 상태/오류 코드로 남깁니다.
- 변경 범위를 작게 유지하고 관련 테스트부터 실행한 뒤 전체 Release verifier를 통과시킵니다.

## 3. 모듈 책임

| 영역 | 책임 |
|---|---|
| `app/core/` | Probe, Metric, 분석, Alert 등 도메인 로직 |
| `app/ui/worker.py` | 다중 대상 측정 loop와 lifecycle |
| `app/ui/main_window.py` | UI 상태와 Worker/Loader/Export 조정 |
| `app/ui/latency_graph.py` | Timeline 렌더링 |
| `app/ui/session_*` | 과거 세션의 background 로딩·열기·정리 |
| `app/storage/` | Session Log/Index, export 및 원자적 저장 |
| `app/developer/` | 로컬 UI 개발 보조와 민감정보 masking |
| `scripts/verify_release.py` | Release 전 통합 검증 gate |
| `scripts/soak_test.py` | deterministic soak profile |
| `scripts/run_stability_soak_suite.py` | 장시간 soak orchestration/evidence |

도메인 판단을 새로 추가할 때 가능하면 `app/core/`에 두고 UI가 계산 결과를 표시하도록 유지합니다.

## 4. 네트워크 테스트 경계

기본 자동 테스트는 외부 네트워크 연결 없이 실행 가능해야 합니다.

사용 권장:

- fake/simulated probe
- RFC 5737 문서용 주소
- 임시 Session Log/Index
- Offscreen Qt
- synthetic timeout/loss/latency pattern

사용 금지:

- 실제 회사 게이트웨이/IP 목록
- 고객/사이트 정보
- 사내 DNS 이름
- 실제 운영 로그
- 실제 계정/Token/비밀번호

허가된 실제 네트워크 smoke는 `--target <FIELD_TARGET>` 경로로 별도 수행하고 그 값을 fixture나 문서에 커밋하지 않습니다.

## 5. 장시간 안정성 우선순위

기능 변경 시 특히 다음을 회귀 확인합니다.

- Worker start/stop/cancel
- timeout 대상 backlog
- active thread 수
- Session Log flush
- segment/index 동기화
- Session Log 손상 복구
- 시간 범위 loader 교체·취소
- UI event gap
- 메모리 증가
- 한글 경로/파일 잠금/권한 오류

`최근 범위 ↔ 측정 전체`처럼 background I/O와 UI 상태가 동시에 바뀌는 경로는 단순 함수 테스트뿐 아니라 lifecycle/soak 테스트를 유지합니다.

## 6. 기본 검증

개발 환경:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --require-hashes -r requirements-dev.lock
```

`requirements-dev.in`의 직접 pin은 `pyproject.toml`의 `dev` 목록과 같은 버전을 유지하고,
CI가 두 목록을 hash lock과 대조합니다. `lxml`은 애플리케이션 runtime 의존성이 아니라
SBOM·의존성 감사 도구가 가져오는 transitive 개발 의존성입니다. `PYSEC-2026-87`의 취약한
5.x 해석을 막기 위해 patched `6.1.0`을 명시적 개발 constraint로 고정했습니다. 이 버전을
허용하는 최소 SBOM library인 `cyclonedx-python-lib 10.3.0`과 호환되는 `pip-audit 2.10.1`도
함께 고정하며, CI는 runtime과 development lock을 각각 `pip-audit`합니다.

빠른 테스트:

```powershell
python -m pytest -q
```

CI의 전체 저장소 Ruff gate는 기존 코드 전체를 한 번에 재작성하지 않도록 실제 실행 오류에 해당하는 `E9`, `F63`, `F7`, `F82`를 차단합니다. 이번 버전에서 새로 만들거나 변경한 Python 파일은 여기에 더해 `ruff format --check`와 import ordering(`I`)도 통과해야 합니다. 이후 변경 범위가 넓어질 때 이 quality boundary를 함께 확장합니다.

Release 기준 전체 검증:

```powershell
python scripts\verify_release.py
```

이 검증은 unit test, compileall, Release policy, Qt smoke, export smoke, deterministic 50-target soak를 포함합니다.

Windows 실행 파일 영향을 주는 변경:

```powershell
.\build_windows_exe.ps1
python scripts\verify_release.py --exe
```

## 7. Soak 검증

빠른 50-target:

```powershell
python scripts\soak_test.py --profile release
```

장시간 suite 계획:

```powershell
python scripts\run_stability_soak_suite.py --dry-run
```

전체 suite:

```powershell
python scripts\run_stability_soak_suite.py
```

실제 8/24시간 증거는 로컬 또는 적절한 self-hosted Windows runner에서 수행합니다.

## 8. UI 원칙

기본 사용자 흐름은 다음 순서를 우선합니다.

```text
IPv4 입력
→ 시작
→ 대상별 상태/그래프
→ 시간 범위 확인
→ 중지
```

고급 경로·Alert·Export·개발 보조 기능이 기본 측정 흐름을 가리지 않도록 유지합니다.

F12 개발자 모드는 일반 운영 기능과 분리된 로컬 개발 보조 기능입니다. 새 실행 시 기본 비활성이며, 민감정보 masking과 외부 자동 전송 금지 경계를 유지합니다.

## 9. 저장 원칙

- Session Log와 Index는 실제 관측 증거이므로 쓰기 실패를 무시하지 않습니다.
- 임시 파일 → 원자적 교체 방식을 사용하는 저장 경계는 유지합니다.
- 부분 손상 복구 시 skip 행 수와 오류 코드를 보존합니다.
- 원본 손상 파일을 자동으로 정상 파일처럼 덮어쓰지 않습니다.
- `build/`, `dist/`, `release/`, `artifacts/`, `exports/`, `logs/`는 소스 저장소에 커밋하지 않습니다.

## 10. Release 원칙

Windows Release는 main에서 수동 workflow로 수행합니다.

Release 전에 다음을 확인합니다.

- `Quality, tests, and security` 성공
- `Windows package validation` 성공
- `long4h` 14,400초 evidence gate 성공
- ZIP/SHA-256/CycloneDX SBOM 생성과 독립 verifier 성공
- Build provenance와 SBOM attestation 생성
- 기준 commit/tag 추적 가능
- 실제 운영정보가 package/notes에 없음

문서-only 또는 내부 정리 변경을 이유로 불필요한 Release를 만들지 않습니다.

## 11. 문서 원칙

README에는 운영 목적과 핵심 설계 판단을 우선합니다.

상세 내용은 다음 문서로 분리합니다.

- 구조: `docs/ARCHITECTURE.md`
- 관측 판단: `docs/OBSERVABILITY_LOGIC.md`
- 검증: `docs/VALIDATION_REPORT.md`
- 안정성: `docs/stability_soak.md`
- 현장 검증: `docs/field_verification.md`
- 오류 코드: `docs/error_codes.md`
- 프로젝트 상태: `docs/PROJECT_STATUS.md`

자동 테스트로 증명하지 않은 실제 환경 호환성을 문서에서 확정적으로 주장하지 않습니다.
