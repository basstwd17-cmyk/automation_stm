/* Generated from a tools/automation measurement config. */
#ifndef AUTONOMOUS_SWEEP_H
#define AUTONOMOUS_SWEEP_H

#include <stdint.h>

#define AUTONOMOUS_SWEEP_PLAN_SIGNATURE "83c3249d5877068b08365b424967b010108bc51a069a6c93b4253545293ab7f0"
#define AUTONOMOUS_SWEEP_PULSES_PER_STEP 1300U
#define AUTONOMOUS_SWEEP_TRANSITION_GUARD_PULSES 30U
#define AUTONOMOUS_SWEEP_STEP_COUNT 4U

static const int32_t autonomous_sweep_delay_ps[
    AUTONOMOUS_SWEEP_STEP_COUNT] =
{
  0L,  /* step 0 */
  2022L,  /* step 1 */
  -4044L,  /* step 2 */
  4044L  /* step 3 */
};

#endif /* AUTONOMOUS_SWEEP_H */
