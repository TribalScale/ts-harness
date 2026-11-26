/**
 * Dates relative to the current week, for fixtures, seeds and test data.
 *
 * Data pinned to a calendar date ages: a fixture written for "the week of Nov 9" shows every
 * order as overdue a month later, and the screens that matter (this week's plan, what's due
 * next) are empty. Build dates from `now` instead, so a fresh environment and every smoke run
 * look like a normal working week. A test that needs a fixed day passes its own `now`.
 */
const DAY_MS = 24 * 60 * 60 * 1000;

/** Midnight UTC at the start of `now`'s ISO week (Monday). */
export function startOfWeek(now: Date = new Date()): Date {
  const midnight = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
  const weekday = (new Date(midnight).getUTCDay() + 6) % 7; // Monday 0 ... Sunday 6
  return new Date(midnight - weekday * DAY_MS);
}

/** `days` after (or before, when negative) `now`, keeping the time of day. */
export function daysFromNow(days: number, now: Date = new Date()): Date {
  return new Date(now.getTime() + days * DAY_MS);
}

/**
 * A day of a week counted from the current one: `weekDay(0, 0)` is this Monday, `weekDay(1, 4,
 * 14)` next Friday at 14:00 UTC, `weekDay(-1, 2)` last Wednesday.
 */
export function weekDay(
  weekOffset: number,
  weekday: number,
  hour = 0,
  now: Date = new Date(),
): Date {
  return new Date(
    startOfWeek(now).getTime() + (weekOffset * 7 + weekday) * DAY_MS + hour * 60 * 60 * 1000,
  );
}

/** `YYYY-MM-DD` of a date, for date-only fields. */
export function isoDate(date: Date): string {
  return date.toISOString().slice(0, 10);
}
