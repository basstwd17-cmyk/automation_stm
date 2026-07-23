/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file           : main.c
  * @brief          : Main program body
  ******************************************************************************
  * @attention
  *
  * Copyright (c) 2026 STMicroelectronics.
  * All rights reserved.
  *
  * This software is licensed under terms that can be found in the LICENSE file
  * in the root directory of this software component.
  * If no LICENSE file comes with this software, it is provided AS-IS.
  *
  ******************************************************************************
  */
/* USER CODE END Header */
/* Includes ------------------------------------------------------------------*/
#include "main.h"
#include "usb_device.h"

/* Private includes ----------------------------------------------------------*/
/* USER CODE BEGIN Includes */

#include "autonomous_sweep.h"

/* USER CODE END Includes */

/* Private typedef -----------------------------------------------------------*/
/* USER CODE BEGIN PTD */

typedef struct
{
  uint32_t channel1_delay_ticks;
  uint32_t channel1_width_ticks;
  uint32_t channel2_delay_ticks;
  uint32_t channel2_width_ticks;
} PulseTimerTimingTicks;

typedef enum
{
  PULSE_AUTONOMOUS_IDLE = 0,
  PULSE_AUTONOMOUS_RUNNING,
  PULSE_AUTONOMOUS_TRANSITION,
  PULSE_AUTONOMOUS_DONE
} PulseAutonomousState;

/* USER CODE END PTD */

/* Private define ------------------------------------------------------------*/
/* USER CODE BEGIN PD */

#define HRTIM_MIN_COMPARE_TICKS       96U
#define HRTIM_MIN_PULSE_TICKS         96U
#define HRTIM_MAX_TICKS               0xFFDFU
#define PULSE_PERIOD_GUARD_TICKS      96U
#define PULSE_TEST_BASE_DELAY_TICKS   128U
#define PULSE_IN1_PIN                 GPIO_PIN_9
#define PULSE_IN2_PIN                 GPIO_PIN_7
#define PULSE_OUT1_PIN                GPIO_PIN_13
#define PULSE_OUT2_PIN                GPIO_PIN_12
#define PULSE_OUT3_PIN                GPIO_PIN_14
#define PULSE_OUT4_PIN                GPIO_PIN_15
#define PULSE_OUTPUT_PINS             (PULSE_OUT1_PIN | PULSE_OUT2_PIN | \
                                       PULSE_OUT3_PIN | PULSE_OUT4_PIN)
#define PULSE_TEST_WIDTH_TICKS        109U
#define PULSE_INPUT_PATH_DIAGNOSTIC   0U
#define PULSE_BURST_IDLE_GAP_MS       300U

_Static_assert(AUTONOMOUS_SWEEP_STEP_COUNT > 0U,
               "Autonomous sweep must contain at least one step");
_Static_assert(AUTONOMOUS_SWEEP_PULSES_PER_STEP > 0U,
               "Autonomous sweep pulse count must be positive");

/* USER CODE END PD */

/* Private macro -------------------------------------------------------------*/
/* USER CODE BEGIN PM */

/* USER CODE END PM */

/* Private variables ---------------------------------------------------------*/
HRTIM_HandleTypeDef hhrtim1;

/* USER CODE BEGIN PV */

/* J3 IN2 (PB7/EEV3) -> Timer C: TC1=OUT2, TC2=OUT1. */
static const PulseTimerTimingTicks timer_c_timing_ticks =
{
  .channel1_delay_ticks = 5440U,
  .channel1_width_ticks = 2720U,
  .channel2_delay_ticks = 13600U,
  .channel2_width_ticks = 2720U
};

/* J6 IN1 (PB9/EEV5) -> Timer D: TD1=OUT3, TD2=OUT4. */
static PulseTimerTimingTicks timer_d_timing_ticks;
static volatile PulseAutonomousState pulse_autonomous_state =
    PULSE_AUTONOMOUS_IDLE;
static volatile uint32_t pulse_autonomous_step_index = 0U;
static volatile uint32_t pulse_autonomous_last_input_tick = 0U;
static volatile uint8_t pulse_autonomous_burst_active = 0U;

/* USER CODE END PV */

/* Private function prototypes -----------------------------------------------*/
void SystemClock_Config(void);
static void MX_GPIO_Init(void);
static void MX_HRTIM1_Init(void);
/* USER CODE BEGIN PFP */

#if PULSE_INPUT_PATH_DIAGNOSTIC
static void PulseGenerator_RunInputPathDiagnostic(void);
#endif
static HAL_StatusTypeDef PulseGenerator_ConfigureTimer(
    uint32_t timer_index,
    const PulseTimerTimingTicks *timing_ticks);
static HAL_StatusTypeDef PulseGenerator_PsToTicks(
    int32_t delay_ps,
    int32_t *signed_ticks);
static HAL_StatusTypeDef PulseGenerator_BuildSignedTiming(
    int32_t signed_ticks,
    PulseTimerTimingTicks *timing_ticks);
static HAL_StatusTypeDef PulseGenerator_SetIn1DelayPs(
    int32_t signed_delay_ps);
static HAL_StatusTypeDef PulseGenerator_UpdateIn1DelayPs(
    int32_t signed_delay_ps);
static HAL_StatusTypeDef PulseGenerator_ApplyTiming(void);
static HAL_StatusTypeDef PulseGenerator_StartTimerD(void);
static HAL_StatusTypeDef PulseGenerator_StopTimerD(void);
static void PulseAutonomous_Reset(void);
static void PulseAutonomous_EnableInputInterrupt(void);
static void PulseAutonomous_Process(void);

/* USER CODE END PFP */

/* Private user code ---------------------------------------------------------*/
/* USER CODE BEGIN 0 */

#if PULSE_INPUT_PATH_DIAGNOSTIC
static void PulseGenerator_RunInputPathDiagnostic(void)
{
  GPIO_InitTypeDef gpio_init = {0};
  const uint32_t output_mask = PULSE_OUTPUT_PINS;

  __HAL_RCC_GPIOB_CLK_ENABLE();

  gpio_init.Pin = PULSE_IN1_PIN | PULSE_IN2_PIN;
  gpio_init.Mode = GPIO_MODE_INPUT;
  gpio_init.Pull = GPIO_PULLDOWN;
  HAL_GPIO_Init(GPIOB, &gpio_init);

  gpio_init.Pin = output_mask;
  gpio_init.Mode = GPIO_MODE_OUTPUT_PP;
  gpio_init.Pull = GPIO_NOPULL;
  gpio_init.Speed = GPIO_SPEED_FREQ_LOW;
  HAL_GPIO_Init(GPIOB, &gpio_init);
  HAL_GPIO_WritePin(GPIOB, output_mask, GPIO_PIN_RESET);

  while (1)
  {
    uint32_t input_state = GPIOB->IDR;
    uint32_t high_mask = 0U;

    if ((input_state & PULSE_IN1_PIN) != 0U)
    {
      high_mask |= PULSE_OUT3_PIN | PULSE_OUT4_PIN;
    }
    if ((input_state & PULSE_IN2_PIN) != 0U)
    {
      high_mask |= PULSE_OUT1_PIN | PULSE_OUT2_PIN;
    }

    GPIOB->BSRR = high_mask |
                  ((output_mask & ~high_mask) << GPIO_BSRR_BR0_Pos);
  }
}
#endif

static HAL_StatusTypeDef PulseGenerator_PsToTicks(
    int32_t delay_ps,
    int32_t *signed_ticks)
{
  int64_t delay_ps_64 = delay_ps;
  uint64_t magnitude_ps;
  uint64_t magnitude_ticks;

  if (signed_ticks == NULL)
  {
    return HAL_ERROR;
  }

  magnitude_ps = (delay_ps_64 < 0)
                 ? (uint64_t)(-delay_ps_64)
                 : (uint64_t)delay_ps_64;
  magnitude_ticks = ((magnitude_ps * 544ULL) + 50000ULL) / 100000ULL;

  if (magnitude_ticks > INT32_MAX)
  {
    return HAL_ERROR;
  }

  *signed_ticks = (delay_ps_64 < 0)
                  ? -(int32_t)magnitude_ticks
                  : (int32_t)magnitude_ticks;
  return HAL_OK;
}

static HAL_StatusTypeDef PulseGenerator_BuildSignedTiming(
    int32_t signed_ticks,
    PulseTimerTimingTicks *timing_ticks)
{
  int64_t signed_ticks_64 = signed_ticks;
  uint64_t magnitude_ticks;
  uint32_t offset_ticks;

  if (timing_ticks == NULL)
  {
    return HAL_ERROR;
  }

  magnitude_ticks = (signed_ticks_64 < 0)
                    ? (uint64_t)(-signed_ticks_64)
                    : (uint64_t)signed_ticks_64;
  if (magnitude_ticks > UINT32_MAX)
  {
    return HAL_ERROR;
  }

  offset_ticks = (uint32_t)magnitude_ticks;
  timing_ticks->channel1_delay_ticks = PULSE_TEST_BASE_DELAY_TICKS;
  timing_ticks->channel1_width_ticks = PULSE_TEST_WIDTH_TICKS;
  timing_ticks->channel2_delay_ticks = PULSE_TEST_BASE_DELAY_TICKS;
  timing_ticks->channel2_width_ticks = PULSE_TEST_WIDTH_TICKS;

  if (signed_ticks >= 0)
  {
    timing_ticks->channel2_delay_ticks += offset_ticks;
  }
  else
  {
    timing_ticks->channel1_delay_ticks += offset_ticks;
  }

  return HAL_OK;
}

static HAL_StatusTypeDef PulseGenerator_ConfigureTimer(
    uint32_t timer_index,
    const PulseTimerTimingTicks *timing_ticks)
{
  HRTIM_TimeBaseCfgTypeDef time_base_cfg = {0};
  HRTIM_CompareCfgTypeDef compare_cfg = {0};
  uint32_t compare_units[4] =
  {
    HRTIM_COMPAREUNIT_1,
    HRTIM_COMPAREUNIT_2,
    HRTIM_COMPAREUNIT_3,
    HRTIM_COMPAREUNIT_4
  };
  uint32_t compare_ticks[4];
  uint64_t channel1_end_ticks;
  uint64_t channel2_end_ticks;
  uint64_t period_ticks_64;
  uint32_t period_ticks;
  uint32_t index;

  if ((timing_ticks == NULL) ||
      (timing_ticks->channel1_delay_ticks < HRTIM_MIN_COMPARE_TICKS) ||
      (timing_ticks->channel1_width_ticks < HRTIM_MIN_PULSE_TICKS) ||
      (timing_ticks->channel2_delay_ticks < HRTIM_MIN_COMPARE_TICKS) ||
      (timing_ticks->channel2_width_ticks < HRTIM_MIN_PULSE_TICKS))
  {
    return HAL_ERROR;
  }

  channel1_end_ticks = (uint64_t)timing_ticks->channel1_delay_ticks
                       + timing_ticks->channel1_width_ticks;
  channel2_end_ticks = (uint64_t)timing_ticks->channel2_delay_ticks
                       + timing_ticks->channel2_width_ticks;
  period_ticks_64 = ((channel1_end_ticks > channel2_end_ticks)
                     ? channel1_end_ticks : channel2_end_ticks)
                    + PULSE_PERIOD_GUARD_TICKS;

  if ((channel1_end_ticks > HRTIM_MAX_TICKS) ||
      (channel2_end_ticks > HRTIM_MAX_TICKS) ||
      (period_ticks_64 > HRTIM_MAX_TICKS))
  {
    return HAL_ERROR;
  }

  compare_ticks[0] = timing_ticks->channel1_delay_ticks;
  compare_ticks[1] = (uint32_t)channel1_end_ticks;
  compare_ticks[2] = timing_ticks->channel2_delay_ticks;
  compare_ticks[3] = (uint32_t)channel2_end_ticks;
  period_ticks = (uint32_t)period_ticks_64;

  if ((compare_ticks[1] <= compare_ticks[0]) ||
      (compare_ticks[3] <= compare_ticks[2]) ||
      (period_ticks <= compare_ticks[1]) ||
      (period_ticks <= compare_ticks[3]) ||
      (period_ticks > HRTIM_MAX_TICKS))
  {
    return HAL_ERROR;
  }

  time_base_cfg.Period = period_ticks;
  time_base_cfg.RepetitionCounter = 0x00;
  time_base_cfg.PrescalerRatio = HRTIM_PRESCALERRATIO_MUL32;
  time_base_cfg.Mode = HRTIM_MODE_SINGLESHOT_RETRIGGERABLE;

  if (HAL_HRTIM_TimeBaseConfig(&hhrtim1, timer_index,
                               &time_base_cfg) != HAL_OK)
  {
    return HAL_ERROR;
  }

  compare_cfg.AutoDelayedMode = HRTIM_AUTODELAYEDMODE_REGULAR;
  compare_cfg.AutoDelayedTimeout = 0x0000;

  for (index = 0U; index < 4U; index++)
  {
    compare_cfg.CompareValue = compare_ticks[index];
    if (HAL_HRTIM_WaveformCompareConfig(&hhrtim1, timer_index,
                                        compare_units[index],
                                        &compare_cfg) != HAL_OK)
    {
      return HAL_ERROR;
    }
  }

  return HAL_OK;
}

static HAL_StatusTypeDef PulseGenerator_SetIn1DelayPs(
    int32_t signed_delay_ps)
{
  int32_t signed_ticks;

  if (PulseGenerator_PsToTicks(signed_delay_ps, &signed_ticks) != HAL_OK)
  {
    return HAL_ERROR;
  }

  if (PulseGenerator_BuildSignedTiming(signed_ticks,
                                       &timer_d_timing_ticks) != HAL_OK)
  {
    return HAL_ERROR;
  }

  if (PulseGenerator_ConfigureTimer(HRTIM_TIMERINDEX_TIMER_D,
                                    &timer_d_timing_ticks) != HAL_OK)
  {
    return HAL_ERROR;
  }

  return HAL_OK;
}

static HAL_StatusTypeDef PulseGenerator_UpdateIn1DelayPs(
    int32_t signed_delay_ps)
{
  return PulseGenerator_SetIn1DelayPs(signed_delay_ps);
}

static HAL_StatusTypeDef PulseGenerator_StartTimerD(void)
{
  if (HAL_HRTIM_WaveformOutputStart(&hhrtim1,
                                    HRTIM_OUTPUT_TD1 |
                                    HRTIM_OUTPUT_TD2) != HAL_OK)
  {
    return HAL_ERROR;
  }

  if (HAL_HRTIM_WaveformCountStart(&hhrtim1,
                                   HRTIM_TIMERID_TIMER_D) != HAL_OK)
  {
    return HAL_ERROR;
  }

  return HAL_OK;
}

static HAL_StatusTypeDef PulseGenerator_StopTimerD(void)
{
  if (HAL_HRTIM_WaveformOutputStop(&hhrtim1,
                                   HRTIM_OUTPUT_TD1 |
                                   HRTIM_OUTPUT_TD2) != HAL_OK)
  {
    return HAL_ERROR;
  }

  if (HAL_HRTIM_WaveformCountStop(&hhrtim1,
                                  HRTIM_TIMERID_TIMER_D) != HAL_OK)
  {
    return HAL_ERROR;
  }

  return HAL_OK;
}

static HAL_StatusTypeDef PulseGenerator_ApplyTiming(void)
{
  if (PulseGenerator_ConfigureTimer(HRTIM_TIMERINDEX_TIMER_C,
                                    &timer_c_timing_ticks) != HAL_OK)
  {
    return HAL_ERROR;
  }

  if (PulseGenerator_SetIn1DelayPs(autonomous_sweep_delay_ps[0]) != HAL_OK)
  {
    return HAL_ERROR;
  }

  return HAL_OK;
}

static void PulseAutonomous_Reset(void)
{
  pulse_autonomous_step_index = 0U;
  pulse_autonomous_last_input_tick = 0U;
  pulse_autonomous_burst_active = 0U;
  pulse_autonomous_state = PULSE_AUTONOMOUS_RUNNING;
}

static void PulseAutonomous_EnableInputInterrupt(void)
{
  __HAL_RCC_SYSCFG_CLK_ENABLE();
  MODIFY_REG(SYSCFG->EXTICR[2],
             SYSCFG_EXTICR3_EXTI9,
             SYSCFG_EXTICR3_EXTI9_PB);
  CLEAR_BIT(EXTI->FTSR1, EXTI_FTSR1_FT9);
  SET_BIT(EXTI->RTSR1, EXTI_RTSR1_RT9);
  WRITE_REG(EXTI->PR1, EXTI_PR1_PIF9);
  SET_BIT(EXTI->IMR1, EXTI_IMR1_IM9);
  HAL_NVIC_SetPriority(EXTI9_5_IRQn, 1U, 0U);
  HAL_NVIC_EnableIRQ(EXTI9_5_IRQn);
}

static void PulseAutonomous_Process(void)
{
  uint32_t next_step;

  if ((pulse_autonomous_state == PULSE_AUTONOMOUS_RUNNING) &&
      (pulse_autonomous_burst_active != 0U))
  {
    if ((uint32_t)(HAL_GetTick() - pulse_autonomous_last_input_tick) <
        PULSE_BURST_IDLE_GAP_MS)
    {
      return;
    }
    pulse_autonomous_burst_active = 0U;
    pulse_autonomous_state = PULSE_AUTONOMOUS_TRANSITION;
  }

  if (pulse_autonomous_state != PULSE_AUTONOMOUS_TRANSITION)
  {
    return;
  }

  next_step = pulse_autonomous_step_index + 1U;
  if (next_step >= AUTONOMOUS_SWEEP_STEP_COUNT)
  {
    if (PulseGenerator_StopTimerD() != HAL_OK)
    {
      Error_Handler();
    }

    pulse_autonomous_state = PULSE_AUTONOMOUS_DONE;
    return;
  }

  if (PulseGenerator_UpdateIn1DelayPs(
          autonomous_sweep_delay_ps[next_step]) != HAL_OK)
  {
    Error_Handler();
  }

  pulse_autonomous_step_index = next_step;
  pulse_autonomous_last_input_tick = 0U;
  pulse_autonomous_burst_active = 0U;
  pulse_autonomous_state = PULSE_AUTONOMOUS_RUNNING;
}

void EXTI9_5_IRQHandler(void)
{
  if (READ_BIT(EXTI->PR1, EXTI_PR1_PIF9) == 0U)
  {
    return;
  }

  WRITE_REG(EXTI->PR1, EXTI_PR1_PIF9);
  if (pulse_autonomous_state != PULSE_AUTONOMOUS_RUNNING)
  {
    return;
  }

  pulse_autonomous_last_input_tick = HAL_GetTick();
  pulse_autonomous_burst_active = 1U;
}

/* USER CODE END 0 */

/**
  * @brief  The application entry point.
  * @retval int
  */
int main(void)
{

  /* USER CODE BEGIN 1 */

  /* USER CODE END 1 */

  /* MCU Configuration--------------------------------------------------------*/

  /* Reset of all peripherals, Initializes the Flash interface and the Systick. */
  HAL_Init();

  /* USER CODE BEGIN Init */

#if PULSE_INPUT_PATH_DIAGNOSTIC
  PulseGenerator_RunInputPathDiagnostic();
#endif

  /* USER CODE END Init */

  /* Configure the system clock */
  SystemClock_Config();

  /* USER CODE BEGIN SysInit */

  /* USER CODE END SysInit */

  /* Initialize all configured peripherals */
  MX_GPIO_Init();
  MX_HRTIM1_Init();
  MX_USB_Device_Init();
  /* USER CODE BEGIN 2 */

  if (PulseGenerator_ApplyTiming() != HAL_OK)
  {
    Error_Handler();
  }

  PulseAutonomous_Reset();
  PulseAutonomous_EnableInputInterrupt();

  if (HAL_HRTIM_WaveformOutputStart(&hhrtim1,
                                    HRTIM_OUTPUT_TC1 |
                                    HRTIM_OUTPUT_TC2) != HAL_OK)
  {
    Error_Handler();
  }

  if (HAL_HRTIM_WaveformCountStart(&hhrtim1,
                                   HRTIM_TIMERID_TIMER_C) != HAL_OK)
  {
    Error_Handler();
  }

  if (PulseGenerator_StartTimerD() != HAL_OK)
  {
    Error_Handler();
  }

  /* USER CODE END 2 */

  /* Infinite loop */
  /* USER CODE BEGIN WHILE */
  while (1)
  {
    /* USER CODE END WHILE */

    /* USER CODE BEGIN 3 */
    PulseAutonomous_Process();
  }
  /* USER CODE END 3 */
}

/**
  * @brief System Clock Configuration
  * @retval None
  */
void SystemClock_Config(void)
{
  RCC_OscInitTypeDef RCC_OscInitStruct = {0};
  RCC_ClkInitTypeDef RCC_ClkInitStruct = {0};
  RCC_CRSInitTypeDef pInit = {0};

  /** Configure the main internal regulator output voltage
  */
  HAL_PWREx_ControlVoltageScaling(PWR_REGULATOR_VOLTAGE_SCALE1_BOOST);

  /** Initializes the RCC Oscillators according to the specified parameters
  * in the RCC_OscInitTypeDef structure.
  */
  RCC_OscInitStruct.OscillatorType = RCC_OSCILLATORTYPE_HSI48|RCC_OSCILLATORTYPE_HSE;
  RCC_OscInitStruct.HSEState = RCC_HSE_ON;
  RCC_OscInitStruct.HSI48State = RCC_HSI48_ON;
  RCC_OscInitStruct.PLL.PLLState = RCC_PLL_ON;
  RCC_OscInitStruct.PLL.PLLSource = RCC_PLLSOURCE_HSE;
  RCC_OscInitStruct.PLL.PLLM = RCC_PLLM_DIV2;
  RCC_OscInitStruct.PLL.PLLN = 85;
  RCC_OscInitStruct.PLL.PLLP = RCC_PLLP_DIV2;
  RCC_OscInitStruct.PLL.PLLQ = RCC_PLLQ_DIV2;
  RCC_OscInitStruct.PLL.PLLR = RCC_PLLR_DIV2;
  if (HAL_RCC_OscConfig(&RCC_OscInitStruct) != HAL_OK)
  {
    Error_Handler();
  }

  /** Initializes the CPU, AHB and APB buses clocks
  */
  RCC_ClkInitStruct.ClockType = RCC_CLOCKTYPE_HCLK|RCC_CLOCKTYPE_SYSCLK
                              |RCC_CLOCKTYPE_PCLK1|RCC_CLOCKTYPE_PCLK2;
  RCC_ClkInitStruct.SYSCLKSource = RCC_SYSCLKSOURCE_PLLCLK;
  RCC_ClkInitStruct.AHBCLKDivider = RCC_SYSCLK_DIV1;
  RCC_ClkInitStruct.APB1CLKDivider = RCC_HCLK_DIV1;
  RCC_ClkInitStruct.APB2CLKDivider = RCC_HCLK_DIV1;

  if (HAL_RCC_ClockConfig(&RCC_ClkInitStruct, FLASH_LATENCY_4) != HAL_OK)
  {
    Error_Handler();
  }

  /** Enable the SYSCFG APB clock
  */
  __HAL_RCC_CRS_CLK_ENABLE();

  /** Configures CRS
  */
  pInit.Prescaler = RCC_CRS_SYNC_DIV1;
  pInit.Source = RCC_CRS_SYNC_SOURCE_USB;
  pInit.Polarity = RCC_CRS_SYNC_POLARITY_RISING;
  pInit.ReloadValue = __HAL_RCC_CRS_RELOADVALUE_CALCULATE(48000000,1000);
  pInit.ErrorLimitValue = 34;
  pInit.HSI48CalibrationValue = 32;

  HAL_RCCEx_CRSConfig(&pInit);
}

/**
  * @brief HRTIM1 Initialization Function
  * @param None
  * @retval None
  */
static void MX_HRTIM1_Init(void)
{

  /* USER CODE BEGIN HRTIM1_Init 0 */

  /* USER CODE END HRTIM1_Init 0 */

  HRTIM_EventCfgTypeDef pEventCfg = {0};
  HRTIM_TimeBaseCfgTypeDef pTimeBaseCfg = {0};
  HRTIM_TimerCtlTypeDef pTimerCtl = {0};
  HRTIM_TimerCfgTypeDef pTimerCfg = {0};
  HRTIM_CompareCfgTypeDef pCompareCfg = {0};
  HRTIM_OutputCfgTypeDef pOutputCfg = {0};

  /* USER CODE BEGIN HRTIM1_Init 1 */

  /* USER CODE END HRTIM1_Init 1 */
  hhrtim1.Instance = HRTIM1;
  hhrtim1.Init.HRTIMInterruptResquests = HRTIM_IT_NONE;
  hhrtim1.Init.SyncOptions = HRTIM_SYNCOPTION_NONE;
  if (HAL_HRTIM_Init(&hhrtim1) != HAL_OK)
  {
    Error_Handler();
  }
  if (HAL_HRTIM_DLLCalibrationStart(&hhrtim1, HRTIM_CALIBRATIONRATE_3) != HAL_OK)
  {
    Error_Handler();
  }
  if (HAL_HRTIM_PollForDLLCalibration(&hhrtim1, 10) != HAL_OK)
  {
    Error_Handler();
  }
  if (HAL_HRTIM_EventPrescalerConfig(&hhrtim1, HRTIM_EVENTPRESCALER_DIV1) != HAL_OK)
  {
    Error_Handler();
  }
  pEventCfg.Source = HRTIM_EEV3SRC_GPIO;
  pEventCfg.Polarity = HRTIM_EVENTPOLARITY_HIGH;
  pEventCfg.Sensitivity = HRTIM_EVENTSENSITIVITY_RISINGEDGE;
  pEventCfg.FastMode = HRTIM_EVENTFASTMODE_DISABLE;
  if (HAL_HRTIM_EventConfig(&hhrtim1, HRTIM_EVENT_3, &pEventCfg) != HAL_OK)
  {
    Error_Handler();
  }
  pEventCfg.Source = HRTIM_EEV5SRC_GPIO;
  if (HAL_HRTIM_EventConfig(&hhrtim1, HRTIM_EVENT_5, &pEventCfg) != HAL_OK)
  {
    Error_Handler();
  }
  pTimeBaseCfg.Period = 11424;
  pTimeBaseCfg.RepetitionCounter = 0x00;
  pTimeBaseCfg.PrescalerRatio = HRTIM_PRESCALERRATIO_MUL32;
  pTimeBaseCfg.Mode = HRTIM_MODE_SINGLESHOT_RETRIGGERABLE;
  if (HAL_HRTIM_TimeBaseConfig(&hhrtim1, HRTIM_TIMERINDEX_TIMER_C, &pTimeBaseCfg) != HAL_OK)
  {
    Error_Handler();
  }
  pTimerCtl.UpDownMode = HRTIM_TIMERUPDOWNMODE_UP;
  pTimerCtl.TrigHalf = HRTIM_TIMERTRIGHALF_DISABLED;
  pTimerCtl.GreaterCMP3 = HRTIM_TIMERGTCMP3_EQUAL;
  pTimerCtl.GreaterCMP1 = HRTIM_TIMERGTCMP1_EQUAL;
  pTimerCtl.DualChannelDacEnable = HRTIM_TIMER_DCDE_DISABLED;
  if (HAL_HRTIM_WaveformTimerControl(&hhrtim1, HRTIM_TIMERINDEX_TIMER_C, &pTimerCtl) != HAL_OK)
  {
    Error_Handler();
  }
  pTimerCfg.InterruptRequests = HRTIM_TIM_IT_NONE;
  pTimerCfg.DMARequests = HRTIM_TIM_DMA_NONE;
  pTimerCfg.DMASrcAddress = 0x0000;
  pTimerCfg.DMADstAddress = 0x0000;
  pTimerCfg.DMASize = 0x1;
  pTimerCfg.HalfModeEnable = HRTIM_HALFMODE_DISABLED;
  pTimerCfg.InterleavedMode = HRTIM_INTERLEAVED_MODE_DISABLED;
  pTimerCfg.StartOnSync = HRTIM_SYNCSTART_DISABLED;
  pTimerCfg.ResetOnSync = HRTIM_SYNCRESET_DISABLED;
  pTimerCfg.DACSynchro = HRTIM_DACSYNC_NONE;
  pTimerCfg.PreloadEnable = HRTIM_PRELOAD_DISABLED;
  pTimerCfg.UpdateGating = HRTIM_UPDATEGATING_INDEPENDENT;
  pTimerCfg.BurstMode = HRTIM_TIMERBURSTMODE_MAINTAINCLOCK;
  pTimerCfg.RepetitionUpdate = HRTIM_UPDATEONREPETITION_DISABLED;
  pTimerCfg.PushPull = HRTIM_TIMPUSHPULLMODE_DISABLED;
  pTimerCfg.FaultEnable = HRTIM_TIMFAULTENABLE_NONE;
  pTimerCfg.FaultLock = HRTIM_TIMFAULTLOCK_READWRITE;
  pTimerCfg.DeadTimeInsertion = HRTIM_TIMDEADTIMEINSERTION_DISABLED;
  pTimerCfg.DelayedProtectionMode = HRTIM_TIMER_A_B_C_DELAYEDPROTECTION_DISABLED;
  pTimerCfg.UpdateTrigger = HRTIM_TIMUPDATETRIGGER_NONE;
  pTimerCfg.ResetTrigger = HRTIM_TIMRESETTRIGGER_EEV_3;
  pTimerCfg.ResetUpdate = HRTIM_TIMUPDATEONRESET_DISABLED;
  pTimerCfg.ReSyncUpdate = HRTIM_TIMERESYNC_UPDATE_UNCONDITIONAL;
  if (HAL_HRTIM_WaveformTimerConfig(&hhrtim1, HRTIM_TIMERINDEX_TIMER_C, &pTimerCfg) != HAL_OK)
  {
    Error_Handler();
  }
  pTimerCfg.DelayedProtectionMode = HRTIM_TIMER_D_E_DELAYEDPROTECTION_DISABLED;
  pTimerCfg.ResetTrigger = HRTIM_TIMRESETTRIGGER_EEV_5;
  if (HAL_HRTIM_WaveformTimerConfig(&hhrtim1, HRTIM_TIMERINDEX_TIMER_D, &pTimerCfg) != HAL_OK)
  {
    Error_Handler();
  }
  pCompareCfg.CompareValue = 2720;
  if (HAL_HRTIM_WaveformCompareConfig(&hhrtim1, HRTIM_TIMERINDEX_TIMER_C, HRTIM_COMPAREUNIT_1, &pCompareCfg) != HAL_OK)
  {
    Error_Handler();
  }
  pCompareCfg.CompareValue = 5440;
  pCompareCfg.AutoDelayedMode = HRTIM_AUTODELAYEDMODE_REGULAR;
  pCompareCfg.AutoDelayedTimeout = 0x0000;

  if (HAL_HRTIM_WaveformCompareConfig(&hhrtim1, HRTIM_TIMERINDEX_TIMER_C, HRTIM_COMPAREUNIT_2, &pCompareCfg) != HAL_OK)
  {
    Error_Handler();
  }
  pCompareCfg.CompareValue = 8160;
  if (HAL_HRTIM_WaveformCompareConfig(&hhrtim1, HRTIM_TIMERINDEX_TIMER_C, HRTIM_COMPAREUNIT_3, &pCompareCfg) != HAL_OK)
  {
    Error_Handler();
  }
  pCompareCfg.CompareValue = 10880;

  if (HAL_HRTIM_WaveformCompareConfig(&hhrtim1, HRTIM_TIMERINDEX_TIMER_C, HRTIM_COMPAREUNIT_4, &pCompareCfg) != HAL_OK)
  {
    Error_Handler();
  }
  pCompareCfg.CompareValue = 16320;

  if (HAL_HRTIM_WaveformCompareConfig(&hhrtim1, HRTIM_TIMERINDEX_TIMER_D, HRTIM_COMPAREUNIT_4, &pCompareCfg) != HAL_OK)
  {
    Error_Handler();
  }
  pOutputCfg.Polarity = HRTIM_OUTPUTPOLARITY_HIGH;
  pOutputCfg.SetSource = HRTIM_OUTPUTSET_TIMCMP1;
  pOutputCfg.ResetSource = HRTIM_OUTPUTRESET_TIMCMP2;
  pOutputCfg.IdleMode = HRTIM_OUTPUTIDLEMODE_NONE;
  pOutputCfg.IdleLevel = HRTIM_OUTPUTIDLELEVEL_INACTIVE;
  pOutputCfg.FaultLevel = HRTIM_OUTPUTFAULTLEVEL_NONE;
  pOutputCfg.ChopperModeEnable = HRTIM_OUTPUTCHOPPERMODE_DISABLED;
  pOutputCfg.BurstModeEntryDelayed = HRTIM_OUTPUTBURSTMODEENTRY_REGULAR;
  if (HAL_HRTIM_WaveformOutputConfig(&hhrtim1, HRTIM_TIMERINDEX_TIMER_C, HRTIM_OUTPUT_TC1, &pOutputCfg) != HAL_OK)
  {
    Error_Handler();
  }
  if (HAL_HRTIM_WaveformOutputConfig(&hhrtim1, HRTIM_TIMERINDEX_TIMER_D, HRTIM_OUTPUT_TD1, &pOutputCfg) != HAL_OK)
  {
    Error_Handler();
  }
  pOutputCfg.SetSource = HRTIM_OUTPUTSET_TIMCMP3;
  pOutputCfg.ResetSource = HRTIM_OUTPUTRESET_TIMCMP4;
  if (HAL_HRTIM_WaveformOutputConfig(&hhrtim1, HRTIM_TIMERINDEX_TIMER_C, HRTIM_OUTPUT_TC2, &pOutputCfg) != HAL_OK)
  {
    Error_Handler();
  }
  if (HAL_HRTIM_WaveformOutputConfig(&hhrtim1, HRTIM_TIMERINDEX_TIMER_D, HRTIM_OUTPUT_TD2, &pOutputCfg) != HAL_OK)
  {
    Error_Handler();
  }
  pTimeBaseCfg.Period = 16864;
  if (HAL_HRTIM_TimeBaseConfig(&hhrtim1, HRTIM_TIMERINDEX_TIMER_D, &pTimeBaseCfg) != HAL_OK)
  {
    Error_Handler();
  }
  if (HAL_HRTIM_WaveformTimerControl(&hhrtim1, HRTIM_TIMERINDEX_TIMER_D, &pTimerCtl) != HAL_OK)
  {
    Error_Handler();
  }
  pCompareCfg.CompareValue = 5440;
  if (HAL_HRTIM_WaveformCompareConfig(&hhrtim1, HRTIM_TIMERINDEX_TIMER_D, HRTIM_COMPAREUNIT_1, &pCompareCfg) != HAL_OK)
  {
    Error_Handler();
  }
  pCompareCfg.CompareValue = 8160;

  if (HAL_HRTIM_WaveformCompareConfig(&hhrtim1, HRTIM_TIMERINDEX_TIMER_D, HRTIM_COMPAREUNIT_2, &pCompareCfg) != HAL_OK)
  {
    Error_Handler();
  }
  pCompareCfg.CompareValue = 13600;
  if (HAL_HRTIM_WaveformCompareConfig(&hhrtim1, HRTIM_TIMERINDEX_TIMER_D, HRTIM_COMPAREUNIT_3, &pCompareCfg) != HAL_OK)
  {
    Error_Handler();
  }
  /* USER CODE BEGIN HRTIM1_Init 2 */

  /* USER CODE END HRTIM1_Init 2 */
  HAL_HRTIM_MspPostInit(&hhrtim1);

}

/**
  * @brief GPIO Initialization Function
  * @param None
  * @retval None
  */
static void MX_GPIO_Init(void)
{
  /* USER CODE BEGIN MX_GPIO_Init_1 */

  /* USER CODE END MX_GPIO_Init_1 */

  /* GPIO Ports Clock Enable */
  __HAL_RCC_GPIOF_CLK_ENABLE();
  __HAL_RCC_GPIOB_CLK_ENABLE();
  __HAL_RCC_GPIOA_CLK_ENABLE();

  /* USER CODE BEGIN MX_GPIO_Init_2 */

  /* USER CODE END MX_GPIO_Init_2 */
}

/* USER CODE BEGIN 4 */

/* USER CODE END 4 */

/**
  * @brief  This function is executed in case of error occurrence.
  * @retval None
  */
void Error_Handler(void)
{
  /* USER CODE BEGIN Error_Handler_Debug */
  /* User can add his own implementation to report the HAL error return state */
  __disable_irq();
  while (1)
  {
  }
  /* USER CODE END Error_Handler_Debug */
}
#ifdef USE_FULL_ASSERT
/**
  * @brief  Reports the name of the source file and the source line number
  *         where the assert_param error has occurred.
  * @param  file: pointer to the source file name
  * @param  line: assert_param error line source number
  * @retval None
  */
void assert_failed(uint8_t *file, uint32_t line)
{
  /* USER CODE BEGIN 6 */
  /* User can add his own implementation to report the file name and line number,
     ex: printf("Wrong parameters value: file %s on line %d\r\n", file, line) */
  /* USER CODE END 6 */
}
#endif /* USE_FULL_ASSERT */
