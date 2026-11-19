# MultiPingCheck Fusion — 개발 브랜치 사용자 안내

이 문서는 **미병합 개발 브랜치**의 시범 기능을 설명합니다. 정식 릴리스가 아니며 운영망 검증도 수행하지 않았습니다.

## 개발 의도

기존 1초/최대 50개 IPv4 ICMP Worker와 Segmented Session Log, 기존 그래프, v0.3.0 HTML5/PDF 보고서를 그대로 사용하고, PySide6의 독립 대시보드를 추가합니다.

## 화면 조작

1. 기존과 같이 IPv4를 입력하고 **시작**을 선택합니다.
2. 상단 **모니터링 화면**에서 Fusion 카드 / 목록 또는 클래식 그래프를 선택합니다. 이 전환은 Worker를 재시작하거나 저장된 기록을 변경하지 않습니다.
3. Fusion 상단의 카드/목록 전환과 이름/IP 검색, 상태 필터를 사용합니다. 검색과 그룹 필터는 화면만 바꾸며 백그라운드 측정 대상에는 영향을 주지 않습니다.
4. 카드 **상세**는 기존 그래프 상세 창으로, **보고서**는 v0.3.0 HTML5/PDF 선택 창으로 연결됩니다.
5. 카드 **이름**은 기존 IP 별칭 편집을 사용하며, 변경된 별칭은 별도의 즐겨찾기 JSON에도 저장됩니다.
6. **+ 그룹 생성**으로 그룹을 만들고 **이름 변경 / 그룹 삭제**를 수행할 수 있습니다. 카드 **그룹**은 추가·해제·다른 그룹으로 이동을 지원합니다. IP를 이동하면 기존 그룹 소속에서 제거되며, 그룹/검색/상태 필터는 측정 대상 자체를 변경하지 않습니다. 측정 시작 전에 **입력창에 불러오기**로 저장된 IP를 붙여넣을 수 있습니다.
7. 카드 **일시중지/재개/삭제**는 기존 실행 중인 Worker 기능을 호출합니다. 삭제 전 확인 창을 사용합니다.
8. 기존 상태 분류에 따른 실제 변화가 관측될 때 이벤트를 로컬 SQLite 파일로 비동기 저장합니다. 장애 발생 → 복구/완화의 지속시간은 동일 실행 세션에서 **단조 시계(monotonic)**로 집계하며, 시작시각을 알 수 없는 이벤트에는 지속시간을 추정해 채우지 않습니다. 수동 일시중지/대기/첫 표본은 장애로 기록하지 않습니다.
9. 최근 이벤트 최대 500건에 대해 **IP·별칭·시각 텍스트 검색, 장애/복구/품질 유형 필터, 전체·1시간·24시간·7일 기간 필터**를 적용할 수 있습니다.

## 파일 및 데이터 보존

- 대상/그룹/별칭: 사용자 데이터 경로의 fusion_favorites.json
- 상태 전환 이력: 사용자 데이터 경로의 fusion_status_events.sqlite3
- 기존 세션 파일 및 Session Index: 변경하지 않음
- 설정 및 측정 정보의 기본 외부 전송 없음
- 즐겨찾기 파일이 손상된 경우 쓰기를 잠그고 원본을 보존합니다. 이름·그룹 변경 저장 실패 시 기존 메모리 및 디스크 설정을 보존합니다.
- SQLite는 구형 이력 DB에서 duration_seconds 열을 추가하는 보존형 마이그레이션을 수행합니다.
- SQLite 저장 실패 시 상태 표시줄에 오류를 알림

Windows에서 사용자 데이터 경로는 기본적으로 %LOCALAPPDATA%\MultiPingCheck\ 입니다.

## 개발 검증

- RFC 5737 예약 IPv4 주소만 자동 테스트에 사용
- pytest -q tests/test_fusion_models_favorites.py tests/test_fusion_dashboard.py tests/test_fusion_ux_enhancements.py
- python scripts/verify_release.py
- Windows PyInstaller ZIP 빌드 및 EXE 기동/HTML5/PDF 보고서 smoke
- 50 대상 Qt 카드 생성·전환 테스트 및 기존 50-target deterministic soak; 추가 UI50/4시간 장시간 검증은 별도 수행

CI를 통과하더라도 실제 회사망의 EDR·VPN·방화벽·NIC 환경을 검증한 것은 아닙니다. 현장 검증은 별도입니다.

## 의존성 보안 감사 개선 (개발 브랜치)

- 개발 도구 체인의 공개 취약 버전 5종(msgpack, certifi, filelock, urllib3, setuptools)을 수정 버전으로 핀하고, 기존 uv pip compile 명령으로 SHA-256 hash-locked 개발 의존성을 갱신했습니다.
- 실행 패키지 runtime PySide6/openpyxl 직접 핀과 Worker 코드는 변경하지 않았습니다.
- 잠금파일 생성·설치·개발/런타임 pip-audit 검증은 브랜치 전용 GitHub Actions에서 확인했습니다. 정식 CI 및 Windows 검증은 PR에서 따로 확인해야 합니다.

## 이번 브랜치에서 의도적으로 제외한 것

- DNS/TCP/Traceroute를 새 Fusion 카드 유형으로 공개하는 작업
- 50대 상한 확대, AirWave/AP 2,000대 연동
- 장비 설정·PoE 조작, 자동 클라우드/이메일 전송
- vmPing의 스트레스/flood 도구

## 안전한 롤백

main과 기존 v0.3.0 릴리스는 변경하지 않습니다. 개발 결과가 만족스럽지 않으면 기존 배포 폴더의 실행 파일을 사용하세요. Fusion은 기존 세션 저장소를 자동으로 변환하거나 삭제하지 않습니다.
