import { describe, expect, it } from 'vitest';
import { daysFromNow, isoDate, startOfWeek, weekDay } from './relativeDates.js';

const wednesday = new Date('2026-11-25T15:30:00Z');

describe('relativeDates', () => {
  it('starts the week on Monday', () => {
    expect(isoDate(startOfWeek(wednesday))).toBe('2026-11-23');
    expect(isoDate(startOfWeek(new Date('2026-11-29T23:00:00Z')))).toBe('2026-11-23');
    expect(isoDate(startOfWeek(new Date('2026-11-23T00:00:00Z')))).toBe('2026-11-23');
  });

  it('counts days from now, either way', () => {
    expect(daysFromNow(2, wednesday).toISOString()).toBe('2026-11-27T15:30:00.000Z');
    expect(daysFromNow(-3, wednesday).toISOString()).toBe('2026-11-22T15:30:00.000Z');
  });

  it('names a weekday of a week relative to this one', () => {
    expect(weekDay(1, 4, 14, wednesday).toISOString()).toBe('2026-12-04T14:00:00.000Z');
    expect(isoDate(weekDay(-1, 2, 0, wednesday))).toBe('2026-11-18');
  });
});
