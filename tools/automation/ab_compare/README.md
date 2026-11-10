# C1-C2 Delay and A/B Swap Measurement

이 폴더는 RTM3004의 일반 통계 기능으로 C1과 C2의 상승 에지 시간차를 반복
측정하고, 단자 교환 전 A와 교환 후 B 결과를 비교하는 코드만 관리한다.
K15 Fast Segmentation은 사용하지 않는다.

## 파일 구성

- `config.json`: 지연 조건, 반복 횟수, 조건당 파형 수와 장비 설정
- `ui.py`: 측정 화면
- `run.py`: 명령행 측정 실행기
- `compare_runs.py`: 원시 지연 샘플로 A/B의 DUT 지연과 측정 경로 지연 분리
- `launch_ui.bat`: 측정 화면 실행
- `launch_cli.bat`: 명령행 실행

새 결과는 `output/ab_compare/<timestamp>`에 저장된다. 기존
`output/delay_sweep_automation` 결과는 이동하거나 수정하지 않는다.

## C1-C2 측정

기본 배선은 발생기 출력 → IN1/J6, OUT3/J4 → C1, OUT4/J5 → C2다.
`launch_ui.bat`를 실행한다. 측정 부호는 `C2 상승 시점 - C1 상승 시점`이므로
양수이면 OUT4가 늦고 음수이면 OUT3가 늦다.

다른 측정 모드에서 돌아온 직후에는 펌웨어 계획이 다를 수 있다. 첫 실행에서
Build와 DFU flash를 함께 선택하고, 같은 설정으로 반복할 때는 둘 다 끈다.

## A/B 단자 교환 측정

1. A 배선으로 한 번 측정한다.
2. C1/C2에 연결된 케이블과 채널 경로를 서로 교환한다.
3. 설정을 바꾸지 않고 B 측정을 실행한다.
4. 가능하면 원래 배선으로 복귀해 A2를 한 번 더 측정한다.

폴더 이름에는 `_A`, `_B`, `_A2`를 붙여 구분한다. 계산식은 다음과 같다.

```text
A = DUT 지연 + 측정 경로 지연
B = -DUT 지연 + 측정 경로 지연

DUT 지연       = (A - B) / 2
측정 경로 지연 = (A + B) / 2
```

A1과 A2를 평균해 B와 비교하는 실행 예시는 다음과 같다.

```powershell
py -3 tools\automation\ab_compare\compare_runs.py `
  --a output\delay_sweep_automation\20260727_095646_A `
      output\delay_sweep_automation\20260727_104609 `
  --b output\delay_sweep_automation\20260727_102320_B
```

결과는 `output/ab_compare/comparisons/<timestamp>`에 저장된다.

- `ab_deembedded_rows.csv`: 반복별 A, B, DUT 지연, 경로 지연과 Z00 보정 잔차
- `ab_deembedded_conditions.csv`: 지연 조건별 평균, 반복 변동과 Z00 보정 잔차
- `ab_comparison_summary.json`: 사용한 폴더, 계산식, 전체 경로 지연과 Z00 오프셋

비교 계산은 `summary.csv`에 반올림되어 표시된 평균이 아니라 각 조건 폴더의
`*_delay_samples.csv` 원시 샘플을 사용한다. 조건별 최종 왜곡은 같은 repeat의
Z00 DUT 오프셋까지 뺀 `zero_corrected_error`로 확인한다. 완료되지 않은 측정,
`quality_status=ok`가 아닌 행, 원시 샘플 수 불일치, 서로 다른 A/B 조건을
입력하면 비교를 중단한다.

상세 SCPI 동기화와 저장 형식은 `TECHNICAL_REFERENCE.md`에 보관되어 있다.
