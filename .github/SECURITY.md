# 보안 및 민감정보 정책

MultiPingCheck는 네트워크 측정 결과와 장시간 세션 데이터를 다루므로 공개 저장소에는 실제 운영환경 식별정보를 남기지 않습니다.

## 공개하지 않는 정보

Issue, Pull Request, 테스트 fixture, 문서, 스크린샷, Release notes에 다음 정보를 올리지 않습니다.

- 실제 사내/고객 IPv4 목록
- Hostname, DNS 이름, 사이트/건물명
- 사용자명, 이메일 계정 정보
- 비밀번호, API Key, Token, SMTP 인증정보
- VPN/방화벽 정책을 유추할 수 있는 실제 내부 경로
- 실제 운영 로그 원문
- 실제 고객 또는 조직 식별정보

예시는 RFC 5737 문서용 주소를 사용합니다.

```text
192.0.2.0/24
198.51.100.0/24
203.0.113.0/24
```

## Session Log와 진단자료

Session Log와 진단 로그는 로컬 운영 증거입니다.

공유 전에 다음을 확인합니다.

1. IP/Hostname 비식별화
2. 사용자·사이트명 제거
3. 실제 파일 경로의 사용자명 제거
4. Alert 외부 endpoint/SMTP 정보 제거
5. 로그에 Token/비밀번호가 포함되지 않았는지 확인

원본 운영 로그는 공개 Issue에 첨부하지 않습니다.

## Developer Mode

F12 개발자 모드는 로컬 개발 보조 기능입니다.

- 새 실행 시 기본 비활성입니다.
- 외부 서버로 요청문을 자동 전송하지 않습니다.
- 회사 정보 masking은 기본 활성 상태를 유지합니다.
- 비밀번호, Token, API Key와 인증정보는 masking 설정과 무관하게 제거해야 합니다.

## Alert 외부 동작

이메일, REST, 외부 실행 파일 같은 Alert action은 고급 기능이며 사용자가 명시적으로 구성해야 합니다.

- 인증정보를 코드 또는 preset에 평문으로 추가하지 않습니다.
- 실제 외부 endpoint를 테스트 fixture에 넣지 않습니다.
- 자동 테스트는 fake/local boundary를 사용합니다.
- 실행 파일 경로와 외부 endpoint는 공개 진단자료에서 제거합니다.

## Release

공개 Release 전 다음을 확인합니다.

- source verifier 통과
- Windows package verifier 통과
- 실제 session/log/output이 package에 없음
- 개발용 산출물(`build/`, `artifacts/`, `logs/`, `exports/`)이 package에 없음
- Release notes에 실제 운영망 정보가 없음
- SHA-256이 게시된 배포 파일과 일치
- runtime/dev 설치가 SHA-256 hash lock을 사용
- `pip-audit`, CodeQL, secret scanning 결과에 처리되지 않은 high/medium 문제가 없음
- CycloneDX 1.6 SBOM과 release manifest가 생성·파싱됨
- ZIP build provenance와 SBOM attestation이 생성된 뒤 annotated tag/Release를 게시

코드서명은 현재 제공하지 않습니다. 사용자는 Release 출처, SHA-256, GitHub attestation을 확인해야 하며 SmartScreen 경고 자체를 안전성 증명으로 해석하지 않습니다.

## 보안 문제 제보

공개 Issue에 민감정보를 올리지 말고 GitHub의 **Report a vulnerability** 기능으로 비공개 제보하십시오. 보안 연락을 위한 별도 이메일 주소는 공개하지 않습니다.

자격 증명, Token, 실제 운영망 정보가 공개된 경우 공개 Issue에 값을 반복해서 복사하지 마십시오.

이미 GitHub에 민감정보가 커밋되었다면 해당 자격 증명을 우선 폐기/교체하고 Git history 정리가 필요한지 검토합니다. 단순히 최신 커밋에서 파일을 삭제하는 것만으로 기존 history의 비밀정보가 제거된 것으로 간주하지 않습니다.
