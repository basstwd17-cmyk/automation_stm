# K15 Segmented Measurement

이 모드는 `../ab_compare/config.json` 기반 일반 통계 sweep과 완전히 분리되어 있다.
기존 측정 코드는 Fast Segmentation을 계속 끈 상태로 사용하며, K15 측정만
이 폴더의 `config.json`과 `runner.py`를 사용한다.

## 측정 구성

- 배선: `OUT3/J4 -> C1`, `OUT4/J5 -> C2`
- 트리거: C1 상승 에지, 1.65 V
- 파형: Sample, Full bandwidth, 2 ns/div, 5000 points
- 발생기: 10 Hz, 3.3 Vpp, offset 1.65 V, duty 50%
- K15: Fast Segmentation ON
- 본 측정 조건:
  - Z00
  - P11
  - N22
  - P22
- 조건별 segment: 1002개
- segment 1~2: 시작 상태 확인용 원시 파형 저장, 에지 통계에서는 제외
- segment 3~1002: 지터 통계용 1000개

## 1. Smoke test

`launch_smoke.bat`을 실행한다.

이 실행은 K15 본 측정용 펌웨어 계획을 빌드하고 DFU로 한 번 플래시한다.
DFU 진입 요청이 나오면 BOOT/RESET으로 DFU 모드에 들어간다. 이후 일반
RESET 요청이 나오면 보드의 RESET을 한 번 누르고 Enter를 누른다.

Smoke test는 Z00에서 segment 3개만 저장한다. 다음 조건을 모두 만족해야
`Debug/k15_smoke_verified.json`이 생성된다.

- RTM3004가 `*OPT?`에서 K15를 보고함
- Fast Segmentation readback이 ON
- 설정한 history segment 수가 3
- C1/C2 timestamp 개수와 값이 일치함
- 각 history segment가 acquisition 1개를 포함함
- C1/C2 파형 축과 point 수가 일치함
- 모든 파형에서 상승 에지 crossing을 계산할 수 있음

실패하면 발생기 출력, burst, acquisition을 정지하고 Fast Segmentation을
끄며 본 측정을 허용하지 않는다.

## 2. Main measurement

Smoke test가 성공한 뒤 `launch_main.bat`을 실행한다.

본 측정에서는 다시 빌드하거나 플래시하지 않는다. 일반 RESET 요청이
나오면 RESET을 한 번 누르고 Enter를 누른다.

순수 acquisition 시간은 약 6분 41초다. 1002개 원파형을 채널별로
전송하고 ZIP으로 저장하므로 전체 실행 시간은 더 길 수 있다.

## 저장 결과

결과는 `output/k15_segmented/<timestamp>_smoke` 또는
`output/k15_segmented/<timestamp>_main`에 저장된다.

각 조건 폴더에는 다음 파일이 생성된다.

- `*_preflight.json`: segment 수와 메모리 설정 readback
- `*_segments.csv`: segment별 timestamp와 측정 지표
- `*_waveforms.zip`: 모든 segment의 C1/C2 원파형 CSV
- `*_waveform_metadata.json`: 시간축, point 수, threshold 설정
- `*_planned_step.json`: 펌웨어 step과 측정 계획

`*_segments.csv`에는 다음 값이 포함된다.

- C1/C2의 low, high, amplitude
- 상대 50% crossing 시간
- 고정 1.65 V crossing 시간
- 10~90% 상승시간
- overshoot
- 두 방식으로 계산한 C2-C1 delay

High 레벨은 전체 기록의 분위수가 아니라 연속된 좁은 펄스 구간에서 계산한다.
따라서 약 20 ns 펄스가 긴 History 기록의 일부만 차지해도 상승 에지와 지연을
분석할 수 있다.

## 기존 통계 sweep으로 복귀

K15 smoke 과정에서 보드에는 K15용 펌웨어 계획이 플래시된다. 기존
`../ab_compare/config.json` sweep으로 복귀할 때는 A/B 측정 UI에서 Build와 DFU flash를 한 번
켜고 실행해야 한다. 그러면 기존 70-step 통계 계획이 다시 빌드되고
플래시된다.
