# K37 Spectrum Measurement

이 폴더는 RTM3004의 K37 Spectrum Analysis, Spectrogram, Peak List 기능을
자동화한다. 기존 C1-C2 지연 통계나 K15 개별 segment 지터 측정을 대체하지
않으며, 동일 지연 조건에서 채널별 주파수 성분과 시간에 따른 스펙트럼 변화를
확인하는 원인 진단용 측정이다.

## 파일 구성

- `config.json`: 펌웨어 sweep, 획득 수, FFT와 Peak List 설정
- `instrument.py`: K37 SCPI 설정, 스펙트럼과 피크 데이터 조회
- `runner.py`: 빌드, DFU, RESET, sweep, 결과 저장
- `launch_smoke.bat`: 3회 획득으로 사전 검증
- `launch_smoke_retest.bat`: 이미 flash된 동일 계획으로 사전 검증을 재실행
- `launch_main.bat`: 조건별 100회 본 측정

## 기본 측정 계획

기존에 성공한 C1-C2 자동화와 동일하게 발생기 burst와 STM32 자율 sweep을
사용한다.

- 발생기: 10 Hz, 3.3 Vpp, offset 1.65 V, duty 50%
- 트리거: C1 상승 에지, 1.65 V
- 채널: OUT3/J4 -> C1, OUT4/J5 -> C2
- FFT window: Hanning
- 크기 단위: dBV
- 범위와 RBW: Full Span, Automatic RBW
- 조건당 본 측정: 100 acquisition
- 저장 trace: Average, Maximum, Minimum, 현재 Spectrum
- Spectrogram과 Peak List: 활성화

K37의 Spectrum source는 한 번에 하나의 채널만 선택할 수 있다. 따라서 각 지연
조건을 같은 값의 CH1 step과 CH2 step으로 연속 배치한다.

```text
Z00_C1 -> Z00_C2
P11_C1 -> P11_C2
N22_C1 -> N22_C2
P22_C1 -> P22_C2
```

두 step은 펌웨어에서 동일한 delay 값을 사용하며, 첫 burst는 CH1 Spectrum,
다음 burst는 CH2 Spectrum을 저장한다.

## 1. 사전 검증

`launch_smoke.bat`을 실행한다. K37용 8-step 펌웨어 계획을 빌드하고 DFU로
플래시하지만, 실제 Smoke는 첫 번째 `Z00_C1`에서 3 acquisition만 수행한다.

동일한 K37 firmware를 이미 flash했고 Python 자동화만 수정한 경우에는
`launch_smoke_retest.bat`을 사용한다. Build/DFU 없이 RESET부터 다시 진행한다.

DFU 요청이 나오면 BOOT을 누른 상태로 RESET을 짧게 누르고 RESET을 놓은 다음
BOOT을 놓는다. 일반 RESET 요청이 나오면 보드 RESET을 한 번 누르고 Enter를
누른다.

Smoke는 다음 항목을 확인한다.

- RTM3004가 `*OPT?`에서 K37을 보고함
- K15 Fast Segmentation이 OFF
- Fast Segmentation이 꺼지고 K37 활성 상태의 메모리 모드가 `SPEC`
- Spectrum, Spectrogram, Peak List readback이 ON
- source와 average count가 설정값과 일치
- Nx Single acquisition 수가 일치
- 평균 계산 완료
- 네 Spectrum trace의 point 수와 주파수축이 일치
- Peak List와 화면 PNG를 저장할 수 있음

성공하면 `Debug/k37_smoke_verified.json`이 생성된다.

## 2. 본 측정

Smoke가 성공한 뒤 `launch_main.bat`을 실행한다. 본 측정에서는 다시 빌드하거나
DFU로 플래시하지 않으며, 보드 RESET만 한 번 수행한다.

기본 acquisition 시간은 약 80초지만 스펙트럼 CSV와 화면 전송 시간이 추가된다.
측정 중에는 케이블, 시간축, 채널 스케일을 변경하지 않는다.

## 저장 결과

결과는 `output/k37_spectrum/<timestamp>_smoke` 또는
`output/k37_spectrum/<timestamp>_main`에 저장된다.

각 step 폴더에는 다음 파일이 생성된다.

- `*_planned_step.json`: delay, source, 펌웨어 step
- `*_preflight.json`: K37 설정 readback
- `*_spectrum.csv`: 주파수별 Average, Maximum, Minimum, 현재 Spectrum
- `*_peaks.csv`: Peak List의 주파수와 크기
- `*_capture.png`: burst 직후의 RTM3004 화면. Spectrum 평균 완료 전에도 저장됨
- `*_spectrogram.png`: 평균 완료 뒤의 RTM3004 Spectrum과 Spectrogram 화면
- `*_spectrum_metadata.json`: point 수, 주파수 간격, span, RBW

상위 폴더에는 전체 설정과 계획을 기록한 `manifest.json`, 조건별 요약
`summary.csv`, 실행 상태 `run_status.json`이 저장된다.

RTM3004 firmware 01.700에서는 유한 burst 뒤 `AVERAGE:COMPLETE?`가 0으로
남을 수 있다. 이 경우에도 읽을 수 있는 raw Spectrum과 캡처는 저장하며,
`summary.csv`의 `quality_notes`에 해당 상태를 경고로 남긴다.
`captured_acquisitions` 값이 계획한 횟수보다 작으면 그 조건은 평균 성능 수치로
해석하지 않고 원인 진단용 단일 캡처로만 사용한다.

K37이 사용하는 `SPEC` 메모리 모드에서는 일반 C1/C2 binary waveform export가
장비 응답 지연을 일으킬 수 있어 기본 측정에서는 저장하지 않는다. 시간 파형은
`*_capture.png`에서 확인하고, 정량 시간 지터 데이터는 K15 또는 A/B 측정 결과를
사용한다.

## 해석 범위

K37 결과는 신호의 고조파, 링잉 주파수, 광대역 잡음, 반복 acquisition 사이에서
변하는 스펙트럼을 관찰하는 자료다. 이 단계에서는 지터 보정식이나 A/B 보정식을
적용하지 않는다. K15에서 얻은 시간 지터와 K37의 특정 spectral peak가 함께
변하는지는 모든 측정이 끝난 뒤 분석 단계에서 비교한다.

현재 기본 발생기 주파수는 기존 성공 조건을 유지하기 위해 10 Hz다. 더 높은
반복 주파수의 위상 잡음이나 sideband를 조사하려면 별도 검증 계획을 세운 뒤
`generator.frequency_hz`, 시간 범위, span과 RBW를 함께 변경해야 한다.

## 다른 모드로 복귀

K37, K15, A/B 측정은 펌웨어 sweep 계획이 서로 다르다. 다른 모드로 이동한 뒤
첫 실행에서는 해당 모드의 Build와 DFU flash를 한 번 수행한다.
