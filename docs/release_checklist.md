# 릴리즈 체크리스트

이 문서는 `v0.2.0` Windows ZIP을 정상 Release로 배포할 때의 fail-closed 절차입니다.

## 1. PR과 main 검증

- PR에서 `Quality, tests, and security`와 `Windows package validation`이 성공해야 합니다.
- squash merge 뒤 같은 두 check가 `main`에서도 성공해야 합니다.
- CodeQL, Dependabot, secret scanning의 high/medium 미해결 alert가 없어야 합니다.

로컬 개발 환경은 hash-locked 개발 의존성을 사용합니다.

```powershell
python -m pip install --require-hashes -r requirements-dev.lock
python scripts\verify_release.py
```

## 2. 4시간 Windows 안정성 증거

`main`에서 `Manual Stability Soak` workflow를 다음 값으로 실행합니다.

- `profiles`: `long4h`
- `runner_mode`: `github-hosted-windows`
- `override_duration_seconds`: 비움

`long4h`는 정확히 14,400초를 요청하며 축약할 수 없습니다. 결과 artifact의 schema v2 JSON에서 다음 항목이 모두 threshold를 통과해야 합니다.

- cadence grid drift와 시작 gap
- 동일 target 최대 overlap 1 이하
- active/pending ping과 thread 수
- memory와 Windows process handle 증가량
- session resume와 loader ownership 해제
- clean shutdown 및 session log 무손실

실패 시 artifact는 진단용으로 남지만 Release 근거로 인정하지 않습니다.

## 3. GitHub Release 생성

`main`에서 `Release Windows ZIP` workflow를 실행합니다. `tag`는 앱의 단일 버전 원본과 일치하는 `v0.2.0`이어야 합니다.

workflow는 다음 asset을 생성하고 검증합니다.

- `MultiPingCheck_v0.2.0.zip`
- `MultiPingCheck_v0.2.0.zip.sha256`
- `MultiPingCheck_v0.2.0_sbom.cdx.json`
- `MultiPingCheck_v0.2.0_release-manifest.json`

또한 annotated tag, Build provenance, SBOM attestation을 생성합니다. Release 생성이 실패하면 이 실행에서 새로 만든 tag만 정리하며, 기존 tag는 삭제하지 않습니다.

## 4. 독립 다운로드 검증

- GitHub Release asset을 새 폴더에 다시 다운로드합니다.
- `.sha256` digest와 ZIP 실제 digest가 같은지 확인합니다.
- ZIP CRC와 필수 파일(`MultiPingCheck.exe`, `README-실행안내.txt`, build info)을 확인합니다.
- SBOM이 CycloneDX 1.6 JSON이고 runtime 구성 요소를 포함하는지 확인합니다.
- annotated tag를 역참조한 commit이 Release manifest의 source commit과 같은지 확인합니다.
- provenance와 SBOM attestation을 GitHub CLI로 검증합니다.

```powershell
python scripts\verify_release_assets.py `
  --zip release\MultiPingCheck_v0.2.0.zip `
  --checksum release\MultiPingCheck_v0.2.0.zip.sha256 `
  --sbom release\MultiPingCheck_v0.2.0_sbom.cdx.json `
  --manifest release\MultiPingCheck_v0.2.0_release-manifest.json `
  --expected-version 0.2.0 `
  --expected-source-commit <TAG_DEREFERENCED_FULL_SHA>
```

## 5. 사용자 안내와 증거 경계

- ZIP은 먼저 완전히 압축 해제한 뒤 실행합니다.
- SmartScreen 또는 Defender 경고가 보이면 Release 출처와 SHA-256을 확인합니다.
- 자동 검증은 synthetic probe, CI, Windows package 동작을 증명합니다.
- 코드서명, 모든 EDR 정책, 실제 회사망 장비와 실제 장애 원인 판정은 증명하지 않습니다.
