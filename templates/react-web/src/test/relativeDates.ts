/**
 * Dates in seeded test data, written relative to today so a scenario never ages.
 *
 * A Gherkin table cell `{today}`, `{today+3}`, `{today-7}`, `{monday}` (this week's Monday) or
 * `{monday+4}` (this Friday) becomes an ISO date (`2026-11-27`); add `T14:00` for a time
 * (`{monday+1T14:00}` is Tuesday 14:00 UTC as a full ISO timestamp). Anything else is left as
 * written. Data pinned to a calendar day ("2026-11-09") shows every order overdue a month later.
 */
const DAY_MS = 24 * 60 * 60 * 1000;
const RELATIVE = /^\{(today|monday)([+-]\d+)?(?:T(\d{2}):(\d{2}))?\}$/;

export function resolveRelativeDate(value: string, now: Date = new Date()): string {
  const match = RELATIVE.exec(value.trim());
  if (!match) return value;
  const [, anchor, offset, hours, minutes] = match;
  const midnight = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
  const weekday = (new Date(midnight).getUTCDay() + 6) % 7; // Monday 0 ... Sunday 6
  const base = anchor === 'monday' ? midnight - weekday * DAY_MS : midnight;
  const day = new Date(base + Number(offset ?? 0) * DAY_MS);
  if (hours === undefined) return day.toISOString().slice(0, 10);
  return new Date(day.getTime() + (Number(hours) * 60 + Number(minutes)) * 60 * 1000).toISOString();
}

/** Every string value of a seeded row, with relative dates resolved. */
export function withRelativeDates<T extends Record<string, unknown>>(
  row: T,
  now: Date = new Date(),
): T {
  return Object.fromEntries(
    Object.entries(row).map(([key, value]) => [
      key,
      typeof value === 'string' ? resolveRelativeDate(value, now) : value,
    ]),
  ) as T;
}
