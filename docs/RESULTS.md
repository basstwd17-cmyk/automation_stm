# 결과와 코드 대응

기존 프로젝트 결과를 수집·분석 코드에 연결한다.

| 결과 | 코드 |
| --- | --- |
| 개발보드 조이스틱 지연 이동 | 별도 `stm32g474-hrtim-delay` 저장소 |
| A/B 보정·설정 지연 오차 | `tools/automation/ab_compare/`, `tools/analysis/analyze_ab_c1c2.py` |
| C1/C2 개별 펄스 상대 지터 | `tools/automation/k15/runner.py` |
| C3 입력 기준 d1·d2·상대 지터 | `k15/external_input_k15.py`, `analysis/analyze_external_input.py` |
| Spectrum·Spectrogram·Peak List | `tools/automation/k37/`, `_render_k37_summary.py` |

기존 Z00 기록은 C1 평균 지연=62.954 ns, C2=61.456 ns, 각 입력 기준 변동=1.670 ns rms, 출력 간 상대 지터=32.9 ps rms다. A/B 보정 설정 오차의 최대 절대값은 197.9 ps다.

→ 모두 과거 측정 기록이며 이 배포에서 새로 측정한 수치는 아니다.

설정 분해능 약 183.8 ps와 기본 주기 약 5.88 ns를 구분한다. 동기식 외부 이벤트 경로의 한 클럭 변동을 현재 Fast 설정의 측정 성능으로 대체하지 않는다.

→ 입력 기준 변동, 상대 지터, 고정 skew, 평균값의 반복 재현성은 서로 다른 지표다.

원시 파형·CSV·그림·장비 상태는 Git에서 제외한다. 결과 공유 시 측정 폴더와 코드 커밋을 함께 기록한다.
