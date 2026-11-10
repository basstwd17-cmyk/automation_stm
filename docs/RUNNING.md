# 실행과 분석

저장소 루트에서 실행한다. `RUN_DIR`, `A_DIR`, `B_DIR`는 실제 측정 폴더로 바꾼다.

## 장비 없는 확인

```powershell
.\.venv\Scripts\python.exe tools\automation\ab_compare\run.py --dry-run --repeats 10
.\.venv\Scripts\python.exe tools\automation\k15_runner.py --dry-run
.\.venv\Scripts\python.exe tools\automation\k37_runner.py --dry-run
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

→ dry-run은 계획과 PC 실행 흐름만 확인하며 실제 파형을 검증하지 않는다.

## A/B 반복 통계

`ab_compare/config.json`의 기본값은 7조건 × 10회 × 100파형이다.

```powershell
.\.venv\Scripts\python.exe tools\automation\ab_compare\ui.py
```

첫 실행은 Build·DFU flash를 함께 선택한다. DFU 업로드 후 발생기 출력이 꺼진 상태에서 RESET한다.

→ A 측정 → C1/C2 케이블·채널 경로 교환 후 B 측정 → 원래 배선 A2 측정.

```powershell
.\.venv\Scripts\python.exe tools\automation\ab_compare\compare_runs.py --a A_DIR A2_DIR --b B_DIR
```

완료된 `quality_status=ok` 행과 원시 샘플 수가 일치해야 비교한다. DUT 지연은 `(A−B)/2`, 경로 지연은 `(A+B)/2`다.

## K15 C1/C2 버스트 측정

```powershell
.\.venv\Scripts\python.exe tools\automation\k15_runner.py --smoke --build --flash --host 192.168.50.2
.\.venv\Scripts\python.exe tools\automation\k15_runner.py --host 192.168.50.2
```

기본은 4조건·조건당 1,002 segment다. 시작 2개를 제외한 1,000개를 분석한다.

→ 같은 계획의 smoke가 성공한 뒤 본 측정을 실행한다.

## 외부 입력 C3 기준 측정

[전용 절차](../tools/automation/k15/INPUT_OUTPUT_MEASUREMENT_GUIDE.md)에 따라 `prepare_plan.py`로 연속 입력 계획을 만들고 빌드·DFU한다.

→ `k15/external_input_k15.py`로 수집한다. 기본 C1/C2 runner에 연속 입력 설정을 넘기는 과거 `launch_input_*`와 구분한다.

## K37 스펙트럼

```powershell
.\.venv\Scripts\python.exe tools\automation\k37_runner.py --smoke --build --flash --host 192.168.50.2
.\.venv\Scripts\python.exe tools\automation\k37_runner.py --host 192.168.50.2
.\.venv\Scripts\python.exe tools\automation\k37\_render_k37_summary.py RUN_DIR
```

→ K15 Fast Segmentation을 끈 상태로 K37을 사용한다. 획득 수를 확인하고 부분 획득과 완료된 평균을 구분한다.

## 결과 분석

```powershell
.\.venv\Scripts\python.exe tools\analysis\analyze_delay_sweep.py RUN_DIR
.\.venv\Scripts\python.exe tools\analysis\analyze_external_input.py RUN_DIR
.\.venv\Scripts\python.exe tools\analysis\analyze_input_output_jitter.py LEGACY_RUN_DIR
```

| 코드 | 입력·목적 |
| --- | --- |
| `analyze_delay_sweep.py` | 일반 sweep의 summary·원시 지연 CSV → baseline·설정 오차·INL·DNL |
| `analyze_external_input.py` | 전용 K15의 input_output_segments.csv → 입력 기준·상대 지터 |
| `analyze_input_output_jitter.py` | K15 summary·segments CSV → d1·d2·skew와 RMS 변동 |
| `ab_compare/compare_runs.py` | A/B 원시 샘플 → DUT·측정 경로 지연 분리 |
| `analyze_c1c2.py`, `analyze_repeat_sweep.py`, `analyze_raw_offset_trend.py` | sweep 비교·반복성·offset 추세 |
| `analyze_ab_c1c2.py`, `analyze_c1_c2_four_runs.py`, `render_ab_c1c2_clean.py` | 2026년 7월 원래 폴더명과 상대 경로에 맞춘 보고서 재생성 |

분석만 수행할 때는 장비가 필요 없다. 원시 결과와 `manifest.json`, `summary.csv`, `run_status.json`을 함께 보관한다.

## 빌드 환경

CubeIDE 2.1.1 / GNU Tools for STM32 14.3.rel1에서 빌드한다. 다른 설치 위치는 README의 환경 변수로 지정한다.

→ `.ioc` 재생성 시 수동 HRTIM·USB·EXTI 설정이 바뀔 수 있으므로 `Core/Src/main.c`와 비교한다.
