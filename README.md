# MultiPingCheck — 다중 대상 네트워크 품질 모니터링

[![CI](https://github.com/sebia1993/multi-target-ping-monitor/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/sebia1993/multi-target-ping-monitor/actions/workflows/ci.yml)
[![CodeQL](https://github.com/sebia1993/multi-target-ping-monitor/actions/workflows/github-code-scanning/codeql/badge.svg)](https://github.com/sebia1993/multi-target-ping-monitor/security/code-scanning)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

**최대 50개의 IPv4 대상을 주기적으로 측정해 지연시간·패킷 손실·상태 변화를 실시간으로 표시하고, 장시간 측정 이력을 세션 단위로 보존·복구하는 Windows 네트워크 관측 도구입니다.**

단발성 `ping` 확인보다 **여러 대상을 오래 측정했을 때 UI가 멈추지 않는지, timeout 대상이 전체 측정을 지연시키지 않는지, 측정 이력이 누락되지 않는지, 저장된 세션을 개발 재연으로 복구·분석할 수 있는지**에 초점을 두고 있습니다.

현재 배포 UI는 **IPv4 입력 → 시작/중지 → 대상별 그래프 → 이름·일시중지·삭제**에 집중합니다. 세션 CSV 저장은 내부에서 작동하지만, 저장 세션 재열기·CSV/XLSX/보고서 내보내기 컨트롤은 UI 단순화 과정에서 숨겼으며 일반 사용자 메뉴가 없습니다. 아래의 저장·복구·export 설명은 구현 및 개발 검증 범위입니다. [사용 화면](docs/USAGE_SCREENSHOTS_KO.md)에서 현재 사용 가능한 동선과 개발 재연을 구분합니다.

> 문서와 테스트에는 RFC 5737 문서용 주소와 simulated probe를 사용합니다. 실제 운영망 IP, 고객/사이트 정보, 사내 로그 원문은 공개 저장소에 포함하지 않습니다.

## 채용 검토를 위한 읽기 순서

1. 아래 운영 문제와 설계 판단에서 **다중 대상 관측·측정 주기·기록 복구**라는 범위를 확인합니다.
2. [설계 사례와 코드·테스트 근거](docs/PORTFOLIO_CASE_STUDY_KO.md)에서 timeout 격리, 지표 계산, 부분 손상 처리를 검토합니다.
3. 같은 문서의 장비 없는 재현 명령과 [CI 실행](https://github.com/sebia1993/multi-target-ping-monitor/actions/workflows/ci.yml)을 확인합니다. 자동 검증과 실제 네트워크 증거는 구분합니다.

## 한눈에 보기

| 항목 | 내용 |
|---|---|
| 대상 | IPv4 최대 50개 |
| 기본 측정 | ICMP, 최종 대상 중심 |
| 기본 주기 | 1초 |
| 주요 지표 | 현재/평균 지연, 손실률, 지연 변동, 샘플 수 |
| 상태 | 정상 / 주의 / 장애 / 대기 / 일시중지 |
| 실시간 그래프 | 대상별 독립 그래프 행 |
| 그래프 범위 | 최근 1/5/10/30분, 1시간, 측정 전체 |
| 장시간 저장 | 대상·월 단위 segmented CSV + Session Index |
| 세션 복구 구현 | 비정상 종료·부분 손상·누락 파일 복구/격리. 일반 UI 재열기 진입점은 현재 숨김 |
| 내보내기 구현 | CSV / XLSX / TXT·HTML / PNG / 통계. 일반 UI 진입점은 현재 숨김 |
| 실행 환경 | Windows 일반 사용자 권한, PySide6 |
| 배포 | `MultiPingCheck.exe` 포함 Windows ZIP |
| 현재 버전 | `0.2.0` |
| 안정성 검증 | deterministic 50-target soak + 4/8/24시간 및 UI 10/20/50대 프로필 |

## 해결하려 한 운영 문제

네트워크 장애가 간헐적으로 발생하면 장애 순간에 단발성 Ping을 실행하는 것만으로는 원인을 확인하기 어렵습니다.

- 여러 스위치·게이트웨이·서버를 동시에 관찰하려면 터미널을 여러 개 띄워야 함
- 순간 지연 또는 손실은 장애 신고 시점과 실제 발생 시점이 다를 수 있음
- 응답 없는 대상이 많을 때 timeout 처리 때문에 전체 측정 주기가 밀릴 수 있음
- 장시간 실행 시 Worker/Thread가 남거나 UI event queue가 밀리면 모니터 자체를 신뢰하기 어려움
- 메모리에 모든 샘플을 계속 쌓으면 장시간 운용 시 사용량이 증가함
- 프로그램을 재시작하면 과거 측정 기록을 다시 찾고 분석하기 어려움
- 저장 중 오류나 일부 손상된 CSV를 정상 세션처럼 취급하면 분석 결과를 신뢰하기 어려움
- 긴 측정 후 `최근 1분`과 `측정 전체`를 반복 전환할 때 background loader의 수명 주기를 안전하게 관리해야 함

MultiPingCheck는 이 문제를 **측정 → bounded live cache → segmented session storage → 시간 범위별 로딩 → 과거 세션 복구** 흐름으로 분리합니다.

## 핵심 설계 판단

| 운영 문제 | 설계 판단 |
|---|---|
| 여러 IPv4 동시 측정 | 대상별 상태를 분리하고 최대 50개까지 관리 |
| timeout 대상이 전체 UI를 지연 | 측정 Worker와 GUI thread를 분리하고 pending ping 수를 soak에서 검증 |
| timeout 작업이 executor를 독점 | 성공 응답이 확인된 대상을 우선하고, 미확인·실패 대상의 동시 점유를 제한해 정상 대상용 capacity를 유지 |
| 느린 ping 때문에 주기가 계속 밀림 | 완료 시각이 아닌 예정 due-time grid에서 다음 slot을 계산하고, 놓친 slot은 폭주 없이 건너뜀 |
| 장시간 메모리 증가 | 실시간 그래프용 메모리 보존 범위와 전체 세션 저장소를 분리 |
| 긴 세션 전체 데이터 조회 | segmented CSV에서 필요한 범위를 별도 Loader thread로 읽음 |
| 이전 Loader 결과가 늦게 도착 | request generation과 QThread lifecycle로 오래된 결과가 현재 화면을 덮지 못하게 함 |
| 측정 종료 직전 샘플 유실 | Worker 종료와 Session Log flush/정리를 회귀 테스트로 검증 |
| 저장 파일 일부 손상 | 부분 복구 사실과 오류 코드를 남기고 불완전한 결과를 정상 파일로 위장하지 않음 |
| 장시간 UI 멈춤 판단 | UI event gap/process time을 수치화한 10/20/50-target soak 프로필 사용 |
| 중간 Hop 손실 과대 해석 | 최종 대상 상태를 우선하고 ICMP rate-limit/방화벽 가능성을 별도로 안내 |
| 공개 저장소 정보 노출 | 문서·테스트·화면은 문서용 주소와 simulated data만 사용 |

상세 구조는 [프로그램 구조](docs/ARCHITECTURE.md), 상태 해석 기준은 [관측·판정 기준](docs/OBSERVABILITY_LOGIC.md)을 참고하십시오.

## 동작 구조

```mermaid
flowchart LR
    IP["IPv4 대상 최대 50개"] --> W["Measurement Worker"]
    W --> P["Probe Engine"]
    P --> O["Observation"]

    O --> M["Metrics / Alert 판단"]
    O --> LIVE["Bounded Live Cache"]
    O --> LOG["Segmented Session CSV"]

    LIVE --> UI["대상별 실시간 그래프"]
    M --> UI

    LOG --> IDX["Session Index"]
    IDX --> LOADER["Background Range Loader"]
    LOADER --> UI

    UI --> EXP["CSV / XLSX / Report / PNG"]
```

```text
IPv4 목록 입력
      ↓
측정 시작
      ↓
대상별 주기 관측
      ↓
실시간 상태 + 그래프
      ↓
segmented CSV 지속 저장
      ↓
최근 구간 / 측정 전체 분석
      ↓
세션 보관 / 재열기 / 내보내기 (내부 구현·개발 재연)
```

## 상태 해석 원칙

| 상태 | 의미 |
|---|---|
| 정상 | 최종 대상 응답이 안정적인 상태 |
| 주의 | 손실 또는 지연 변동이 관찰되어 확인이 필요한 상태 |
| 장애 | 높은 손실 또는 연속 응답 없음 등 심각한 상태 |
| 일시중지 | 사용자가 해당 대상 측정을 중지한 상태 |
| 대기 | 아직 유효한 관측이 없는 상태 |

기본 화면은 **초록=정상, 주황=주의, 빨강=장애, 회색=대기/일시중지**로 구분합니다.

중요한 해석 경계:

- 중간 Hop의 높은 packet loss만으로 그 Hop을 장애 원인으로 확정하지 않습니다.
- 중간 장비는 ICMP 응답을 rate-limit하거나 낮은 우선순위로 처리할 수 있습니다.
- 최종 대상의 응답, 손실·지연 추이, 문제가 시작된 시점과 다른 관측 증거를 함께 확인합니다.
- 이 프로그램은 장애 가능성을 좁히기 위한 관측 도구이며 장애 원인을 자동 확정하지 않습니다.
- `jitter_ms`는 성공한 RTT 표본의 표준편차입니다. 단방향 지연 변동이나 RTP jitter를 직접 측정한 값은 아닙니다. 계산식은 [관측·판정 기준](docs/OBSERVABILITY_LOGIC.md)에 있습니다.

## 실행 화면

아래 이미지는 **실제 PySide6 `MainWindow`**를 RFC 5737 문서용 IPv4와 synthetic latency/loss 데이터로 렌더링한 예시입니다. 실제 운영 데이터는 포함하지 않습니다.

![MultiPingCheck 다중 대상 측정 화면](docs/images/multiping-main.png)

화면에서는 대상별로 현재 상태, 손실률, 샘플 수와 독립적인 실시간 지연 그래프를 함께 확인할 수 있습니다.

## 시간 범위와 장시간 세션

공통 그래프 범위는 다음 구간을 지원합니다.

```text
최근 1분
최근 5분
최근 10분
최근 30분
최근 1시간
측정 전체
```

최근 구간은 bounded live cache를 우선 사용하고, 메모리에 없는 과거 구간이나 `측정 전체`는 저장된 세션 로그를 background loader가 읽습니다.

이 구조의 목적은 **장시간 측정 데이터 전체를 UI thread에서 동기적으로 읽지 않는 것**입니다. Loader 교체·취소와 늦게 도착한 결과도 generation 기준으로 구분해 이전 요청이 현재 화면을 덮지 못하도록 관리합니다.

## 세션 저장과 복구

기본 측정 데이터:

```text
%LOCALAPPDATA%\MultiPingCheck\session_logs
```

진단 로그:

```text
%LOCALAPPDATA%\MultiPingCheck\logs\multipingcheck.log
```

사용자 내보내기 기본 위치:

```text
%USERPROFILE%\Documents\MultiPingCheck
```

장시간 저장은 하나의 거대한 CSV 대신 **대상·월 단위 segmented CSV + Session Index** 구조를 사용합니다.

- 오래 실행된 세션을 여러 segment로 관리
- 재시작 후 기존 세션 탐색
- stale active session 복구
- 실제 파일과 index의 샘플 수/마지막 시각 재조정
- 누락 파일을 정상 세션으로 표시하지 않음
- 부분 손상 행은 건너뛴 수를 기록해 복구 사실을 남김
- 보관 기간 기준 정리 지원

## 안정성 검증

기능 수보다 **오래 켜 두었을 때 계속 신뢰할 수 있는지**를 중요하게 봅니다.

기본 Release verifier:

```text
pytest
compileall
Qt offscreen smoke
CSV / XLSX / TXT export smoke
50-target deterministic soak
```

추가 soak 프로필:

| 프로필 | 목적 |
|---|---|
| `release` | 빠른 50-target release smoke |
| `long` | 30분 50-target 안정성 |
| `long4h` | 4시간 장시간 검증 |
| `long8h` | 8시간 장시간 검증 |
| `long24h` | 24시간 장시간 검증 |
| `ui10` | 10대 UI event gap 검증 |
| `ui20` | 20대 UI event gap 검증 |
| `ui50` | 50대 UI event gap 검증 |

주요 검증 지표:

- Worker 정상 종료 여부
- Session Log row/segment 생성 여부
- 최대 active thread 수
- memory growth
- CPU 사용률
- pending ping 수
- log queue depth
- UI event gap / event process time
- 정상 대상의 예정 due 대비 submit lateness와 실제 runner 시작 gap
- 모든 대상의 실제 probe 시작 coverage와 대상별 최소/최대 횟수
- 동일 대상 probe 최대 동시 실행 수(1 이하여야 함)
- Windows process handle 증가량
- 세션 resume 기록과 background loader 소유권 정리

UI 10/20/50대 프로필은 기본적으로 event gap과 event 처리 시간이 **0.2초 이하인지** 검증합니다. 상세 기준은 [안정성 soak](docs/stability_soak.md)와 [검증 보고서](docs/VALIDATION_REPORT.md)에 있습니다.

## 실제 회귀를 테스트로 남기는 방식

장시간 50대 측정 후 시간 범위를 반복 변경하고 `측정 전체`로 전환하는 과정에서 background Session Log Loader의 QThread 수명 주기 경합이 발생할 수 있었습니다.

현재는:

- 이전 loader 정리 중 새 loader 상태를 잘못 삭제하지 않음
- request generation이 다른 늦은 결과를 폐기
- 이전 `finished` callback이 현재 QThread 참조를 훼손하지 않음
- 동일 경로를 회귀 테스트로 유지

하도록 관리합니다.

이 사례는 단순 Ping 기능보다 **장시간 네트워크 관측 프로그램의 lifecycle을 어떻게 검증하는지**를 보여주는 핵심 회귀 시나리오입니다.

## 일반 사용자 빠른 시작

1. GitHub **Releases**에서 최신 `MultiPingCheck_<버전>.zip`을 받습니다.
2. ZIP을 별도 폴더에 완전히 압축 해제합니다.
3. `MultiPingCheck.exe`를 실행합니다.
4. IPv4 주소를 한 줄에 하나씩 입력하거나 Excel의 IP 열을 붙여넣습니다.
5. `시작`을 눌러 대상별 상태와 그래프를 확인합니다.
6. 필요하면 그래프 범위를 바꿔 장애 시간대를 비교합니다.
7. 측정을 끝낼 때 `중지`를 누릅니다.

프로그램은 일반 사용자 권한으로 동작하도록 배포 검증합니다. 처음 실행할 때 Windows SmartScreen이 표시되면 Release 출처와 SHA-256을 먼저 확인하십시오.

## 증거의 범위

| 증거 | 확인하는 것 | 확인하지 못하는 것 |
|---|---|---|
| Unit/통합 테스트 | cadence, overlap 방지, 저장·복구, UI lifecycle의 결정 가능한 동작 | 실제 장비·방화벽·VPN 조합 |
| Simulated soak | timeout이 많은 50-target 부하, 메모리/thread/handle, 세션·loader 수명 | 실제 패킷이 통과하는지 여부 |
| Windows package CI | Windows runner에서 EXE 빌드·기동·종료, ZIP 구조 | 코드서명, 모든 EDR/보안 정책 |
| Field verification | 허가된 실제 네트워크에서 ICMP/Tracert와 사용자 흐름 | CI로 자동 재현되지 않음 |

공개 CI와 4시간 soak는 synthetic RFC 5737 데이터를 사용합니다. 따라서 자동 검증 통과를 실제 운영망 호환성이나 장애 원인 규명의 증명으로 표현하지 않습니다.

## 운영 안전·민감정보 경계

- 기본 기능은 네트워크 관측이며 장비 설정을 변경하지 않습니다.
- 자동 테스트는 실제 사내 장비·고객망 의존성을 요구하지 않습니다.
- 저장소에 실제 IP 목록, Hostname, 사용자명, 고객/사이트명, 실제 로그를 커밋하지 않습니다.
- 외부 공유용 화면과 진단자료는 식별자를 먼저 비식별화합니다.
- 이메일/REST/외부 실행 Alert action은 사용자가 명시적으로 구성해야 하는 고급 기능입니다.
- 비밀번호와 Token을 소스·로그·문서에 직접 기록하지 않습니다.

공개 저장소 기준은 [보안 정책](.github/SECURITY.md), 실제 네트워크 검증 기준은 [현장 검증](docs/field_verification.md)을 참고하십시오.

## 검증 명령

기본 전체 검증:

```powershell
python scripts\verify_release.py
```

Windows EXE 포함 검증:

```powershell
.\build_windows_exe.ps1
python scripts\verify_release.py --exe
```

허가된 실제 대상에 대한 읽기 전용 smoke:

```powershell
python scripts\verify_release.py --target <FIELD_TARGET>
```

실제 대상 값을 fixture나 문서에 커밋하지 않습니다.

## 개발

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --require-hashes -r requirements-dev.lock
python -m app.main
```

수정 원칙과 모듈 책임은 [`DEVELOPMENT.md`](DEVELOPMENT.md)에 정리되어 있습니다. F12 개발자 모드는 로컬 UI 개발 보조 기능이며 상세 사용법은 [개발자 모드](docs/DEVELOPER_MODE.md)에 분리되어 있습니다.

## Release

Windows Release는 `Release Windows ZIP` GitHub Actions를 **main 브랜치에서 수동 실행**합니다.

```text
소스 검증
→ Windows EXE 빌드
→ 패키지 검증
→ ZIP / SHA-256 / CycloneDX SBOM 생성·검증
→ Build provenance·SBOM attestation
→ Git tag
→ GitHub Release
```

EXE/ZIP/checksum은 소스 저장소에 커밋하지 않고 Release asset으로 게시합니다. 절차는 [Release 체크리스트](docs/release_checklist.md)를 참고하십시오.

## License

소스 코드는 [MIT License](LICENSE)로 공개합니다. Windows 배포 ZIP에 포함되는 제3자 구성요소는 함께 게시되는 CycloneDX SBOM에서 확인할 수 있습니다.

## 문서

| 문서 | 내용 |
|---|---|
| [프로그램 구조](docs/ARCHITECTURE.md) | Probe → Worker → Live Cache → Session Storage → UI 구조 |
| [관측·판정 기준](docs/OBSERVABILITY_LOGIC.md) | 지연·손실·중간 Hop 해석과 상태 판단 원칙 |
| [검증 보고서](docs/VALIDATION_REPORT.md) | 자동 테스트·soak·Windows/현장 검증의 증거 경계 |
| [안정성 soak](docs/stability_soak.md) | 4/8/24시간 및 UI 10/20/50대 검증 절차 |
| [현장 검증](docs/field_verification.md) | 허가된 실제 네트워크 확인 항목 |
| [오류 코드](docs/error_codes.md) | 안정적인 오류 코드와 1차 조치 |
| [프로젝트 상태](docs/PROJECT_STATUS.md) | 현재 범위와 향후 개선 방향 |
| [개발자 모드](docs/DEVELOPER_MODE.md) | 로컬 UI 개발 보조 기능 |

화면별 조작과 읽을 값은 [현재 사용 화면 안내](docs/USAGE_SCREENSHOTS_KO.md)를 참고하세요.
