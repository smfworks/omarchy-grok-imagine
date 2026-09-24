/** Keep in step with shot_count_for and split_durations in plan.py. */

export const MIN_PLAN_TARGET_SEC = 8;
export const MAX_PLAN_TARGET_SEC = 120;
export const MIN_PLAN_SHOTS = 2;
export const MAX_PLAN_SHOTS = 8;
const PREFERRED_SHOT_SEC = 8;
const MIN_CLIP_SEC = 1;
const MAX_CLIP_SEC = 15;

export const DURATION_PRESETS = [16, 24, 32, 48, 64] as const;

export function shotCountFor(targetSec: number): number {
  const quotient = Math.floor(targetSec / PREFERRED_SHOT_SEC);
  const remainder = targetSec % PREFERRED_SHOT_SEC;
  const rounded = remainder * 2 >= PREFERRED_SHOT_SEC ? quotient + 1 : quotient;
  return Math.min(MAX_PLAN_SHOTS, Math.max(MIN_PLAN_SHOTS, rounded));
}

export function splitDurations(targetSec: number, shotCount: number): number[] {
  const low = MIN_CLIP_SEC * shotCount;
  const high = MAX_CLIP_SEC * shotCount;
  const goal = Math.min(Math.max(targetSec, low), high);
  const base = Math.floor(goal / shotCount);
  const extra = goal % shotCount;
  return Array.from({ length: shotCount }, (_, index) => base + (index < extra ? 1 : 0));
}

export function planEstimate(targetSec: number): { count: number; durations: number[] } | null {
  if (
    !Number.isInteger(targetSec) ||
    targetSec < MIN_PLAN_TARGET_SEC ||
    targetSec > MAX_PLAN_TARGET_SEC
  ) {
    return null;
  }
  const count = shotCountFor(targetSec);
  return { count, durations: splitDurations(targetSec, count) };
}
