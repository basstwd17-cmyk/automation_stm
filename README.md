# STM32 PCB Delay Generator

STM32G474CEU6 PCB 펌웨어와 RTM3004의 K15·K37·A/B 측정·분석 코드를 함께 관리한다.

→ 개발보드 검증은 [stm32g474-hrtim-delay](https://github.com/basstwd17-cmyk/stm32g474-hrtim-delay), PCB와 측정 도구는 이 저장소에서 확인한다.

## 코드 구성

| 경로 | 역할 |
| --- | --- |
| `Core/`, `USB_Device/` | PCB 펌웨어, 외부 이벤트, Timer C·D, 자율 sweep |
| `Drivers/`, `Middlewares/` | ST HAL·CMSIS·USB 드라이버 |
| `tools/automation/ab_compare/` | C1−C2 반복 통계와 A/B 케이블·채널 교환 보정 |
| `tools/automation/k15/` | K15 History의 개별 파형·상승 에지·지터 수집 |
| `tools/automation/k37/` | Spectrum·Spectrogram·Peak List 수집과 요약 그림 |
| `tools/automation/common/` | 계측기 제어, sweep 계획, 빌드·DFU 공통 기능 |
| `tools/analysis/` | 입력 기준 지터, 상대 지연, 반복 sweep, A/B 분석 |
| `tools/measurement/` | RTM3004 설정 조회·파형·화면 수집 보조 도구 |

## 시작하기

Windows, Python 3.10 이상, STM32CubeIDE와 STM32CubeProgrammer를 사용한다. K15·K37 측정에는 해당 RTM3004 장비 옵션이 필요하다.

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

STM32CubeIDE에서 **Existing Projects into Workspace**로 이 폴더를 가져와 `Debug`를 한 번 빌드한다. 프로젝트 이름은 `delay_generator_g474ceu6`이다.

→ `Debug/makefile`이 생성된 뒤 자동화의 Build 기능을 사용할 수 있다. 최초 import에서 `.ioc` 재생성은 필요 없다.

```powershell
$env:STM32CUBEIDE_ROOT = 'C:\ST\STM32CubeIDE\STM32CubeIDE'
$env:STM32_PROGRAMMER_CLI = 'C:\Program Files\STMicroelectronics\STM32Cube\STM32CubeProgrammer\bin\STM32_Programmer_CLI.exe'
```

분석 명령은 저장소의 `.venv` Python을 사용한다. 확인한 패키지 버전은 [requirements-tested.txt](requirements-tested.txt)에 기록했다.

위 경로는 본인 PC에 맞춘다. `STM32CUBEIDE_ROOT`는 `plugins` 폴더가 있는 위치다. 측정 전 선택한 모드의 `config.json`과 장비 IP를 확인한다.

| 목적 | 시작 위치 |
| --- | --- |
| A/B 상대 지연 측정 | `tools/automation/launch_ab_compare.bat` |
| K15 C1/C2 사전 검증·본 측정 | `launch_k15_smoke.bat` → `launch_k15_main.bat` |
| K37 사전 검증·본 측정 | `launch_k37_smoke.bat` → `launch_k37_main.bat` |
| 외부 입력 C3를 포함한 K15 | [입력 기준 측정 절차](tools/automation/k15/INPUT_OUTPUT_MEASUREMENT_GUIDE.md) |
| 저장 결과 분석 | [실행·분석 명령](docs/RUNNING.md) |

실행 파일은 `tools/automation/`에 있다. 모드를 바꾸면 펌웨어 계획도 달라지므로 다시 빌드·DFU·RESET한다.

## PCB 신호

| PCB | MCU·HRTIM | 측정 연결 |
| --- | --- | --- |
| IN1 / J6 | PB9 / EEV5 → Timer D | 발생기 입력, 입력 기준 측정 시 C3 |
| OUT3 / J4 | PB14 / TD1 | C1 |
| OUT4 / J5 | PB15 / TD2 | C2 |
| IN2 / J3 | PB7 / EEV3 → Timer C | 두 번째 외부 입력 |
| OUT1 / OUT2 | PB13 / TC2, PB12 / TC1 | 나머지 출력 쌍 |

측정 부호는 `OUT4(C2) − OUT3(C1)`. 양수이면 OUT4가 늦다.

## 펌웨어와 측정 계획

- HSE 8 MHz → PLL 170 MHz, HRTIM MUL32 → 약 **183.8 ps/tick**.
- OUT3·OUT4 펄스 폭: 109 tick, 약 **20.04 ns**.
- 원본 입력 모드: **EEV3 Rising/동기식**, **EEV5 Level/Fast**.
- 기본 헤더: `k15/config_input_reference.json`, 4조건·조건당 1,300 입력 + 30 guard 입력.
- 기존 A/B·K15 C1/C2·K37 버스트 계획: **300 ms 입력 유휴 구간**에서 단계 전환.
- 연속 입력 계획: 헤더에 `AUTONOMOUS_SWEEP_TRANSITION_GUARD_PULSES`가 있을 때 **입력 개수**로 전환.

→ `prepare_plan.py`로 선택한 설정의 헤더를 장비 없이 생성한다. [실행 절차](docs/RUNNING.md)를 따른다.

## 결과와 버전 해석

설정 분해능, 설정 오차, 입력 기준 변동, 출력 간 상대 지터를 구분한다.

→ 과거 측정값을 재현하려면 원시 데이터와 당시 펌웨어·이벤트 모드·배선을 함께 확인한다. 현재 Fast 설정만으로 입력 기준 지터가 제거되었다고 해석하지 않는다.

- [결과와 코드 대응](docs/RESULTS.md)
- [원본과 배포 변경점](docs/PROVENANCE.md)
- [빌드·실행 점검 결과](docs/VALIDATION.md)
- [프로젝트 정리 페이지](https://chatgpt.com/space/page_b490046acce88191bf61d6f8bf3e7bdb)

빌드 결과·장비 상태·원시 파형은 `Debug/`, `output/` 등에 보관하며 Git에서 제외한다.
