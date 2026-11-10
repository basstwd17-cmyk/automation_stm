# 입력 기준 K15 측정

IN1의 외부 입력을 C3에 함께 저장해 입력→C1, 입력→C2, C2−C1을 구분한다.

→ 연속 입력 전용 실행기 `external_input_k15.py`를 사용한다. 기본 C1/C2 버스트용 `k15_runner.py`와 구분한다.

## 배선

- IN1/J6/PB9/EEV5 → 발생기 입력과 RTM3004 C3.
- OUT3/J4/PB14/TD1 → C1, OUT4/J5/PB15/TD2 → C2.
- C3 rising, 1.65 V 트리거. 세 경로의 케이블·종단 조건을 기록한다.

## 실행

1. `config_input_reference.json`의 IP·주파수·조건을 확인하고 헤더를 생성한다.

```powershell
.\.venv\Scripts\python.exe tools\automation\prepare_plan.py --config tools\automation\k15\config_input_reference.json
```

2. CubeIDE Debug 빌드 → CubeProgrammer DFU 업로드.
3. 외부 입력을 준비하고 smoke 실행 → 안내에 따라 RESET.

```powershell
.\.venv\Scripts\python.exe tools\automation\k15\external_input_k15.py --segments 3
.\.venv\Scripts\python.exe tools\automation\k15\external_input_k15.py --segments 1000
```

→ 각 실행은 WGEN을 변경하지 않는다. 기본은 조건당 1,300 입력 + 30 guard 입력이다. 계획을 바꾸면 헤더 생성·빌드·업로드를 다시 한다.

## 분석

```powershell
.\.venv\Scripts\python.exe tools\analysis\analyze_external_input.py RUN_DIR
```

`input_output_segments.csv`에서 같은 segment의 상승 50% crossing으로 d1=C1−IN, d2=C2−IN, Δ=C2−C1을 계산한다.

→ 평균은 고정 지연·skew, 표준편차는 반복 RMS 변동이다. 시작 segment의 포함·제외 조건도 함께 확인한다.

기존 `launch_input_*`는 과거 generic runner 진입점이다. 연속 입력 계획은 위 전용 명령과 `launch_external_input_*`를 사용한다. `external_input_statistics_sweep.py`는 K15 원파형 대신 세 지연 통계값을 수집하는 보조 도구다.
