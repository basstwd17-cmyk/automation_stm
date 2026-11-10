# 배포 확인

2026년 11월 10일 코드 통합 후 장비 연결 없이 확인했다.

| 확인 | 결과 |
| --- | --- |
| ARM GCC 컴파일·링크 | A/B, K15 버스트, K37, 기본 연속 입력 4계획 통과·경고 0 |
| 기본 연속 입력 ELF | text 34,740 B / data 12 B / bss 2,588 B |
| Python 구문 | 40파일 통과 |
| 회귀 점검 | 5개 통과: 설정·기본 헤더·잘못된 모드 차단·공통 변동 상쇄·A/B 분리와 불일치 거부 |
| dry-run | A/B 70단계, K15 4단계, K37 8단계 완료 |
| CLI 도움말 | 주요 12개 진입점 통과 |
| 합성 데이터 분석 | 전용 K15 CSV, 일반 delay sweep, K37 matplotlib·Pillow 그림 생성 통과 |

→ 합성 데이터는 실행 경로 확인용이며 실제 측정 결과로 보관·게시하지 않는다.

이번 배포에서 보드 DFU나 RTM3004 측정은 실행하지 않았다. 기존 페이지의 성능 수치는 과거 측정 기록으로 유지한다.

→ 버스트/연속 입력 전환 호환 조정의 실제 장비 검증은 각 모드의 smoke 절차에서 다시 확인한다.

Python 패키지 버전은 [requirements-tested.txt](../requirements-tested.txt)에 기록한다. 검사 코드는 `python -m unittest discover -s tests -v`로 실행한다.
