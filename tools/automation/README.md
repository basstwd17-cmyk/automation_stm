# Delay Measurement Automation

자동화 코드는 측정 목적별로 분리되어 있다.

- `ab_compare`: RTM3004 통계 기능으로 C1–C2 지연을 측정하고, 단자 교환 전 A와
  교환 후 B 결과를 비교한다.
- `k15`: K15 Fast Segmentation과 History를 이용해 개별 파형과 지터를 저장한다.
- `k37`: K37 Spectrum, Spectrogram, Peak List로 채널별 주파수 성분을 저장한다.
- `common`: 세 측정 모드가 함께 사용하는 펌웨어 계획, 빌드·DFU, RTM3004
  제어 코드다. 일반 실행 시 이 폴더의 파일을 직접 실행하지 않는다.

기존 C1–C2 측정은 `launch_ab_compare.bat`, K15 사전 검증은
`launch_k15_smoke.bat`, K37 사전 검증은 `launch_k37_smoke.bat`로 실행한다.
각 사전 검증이 성공한 뒤 해당 `launch_*_main.bat`로 본 측정을 실행한다.

각 모드의 설정과 자세한 순서는 해당 폴더의 `README.md`를 따른다. 세 모드가
사용하는 펌웨어 sweep 계획이 다르므로 모드를 바꾼 뒤 처음 실행할 때는 Build와
DFU flash가 필요하다.
