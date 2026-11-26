import { describe, expect, it } from 'vitest';
import { resolveRelativeDate, withRelativeDates } from './relativeDates';

const wednesday = new Date('2026-11-25T15:30:00Z');

describe('relative dates in seeded data', () => {
  it('resolves today and Monday with day offsets', () => {
    expect(resolveRelativeDate('{today}', wednesday)).toBe('2026-11-25');
    expect(resolveRelativeDate('{today+3}', wednesday)).toBe('2026-11-28');
    expect(resolveRelativeDate('{today-7}', wednesday)).toBe('2026-11-18');
    expect(resolveRelativeDate('{monday}', wednesday)).toBe('2026-11-23');
    expect(resolveRelativeDate('{monday+4}', wednesday)).toBe('2026-11-27');
  });

  it('adds a time when one is given', () => {
    expect(resolveRelativeDate('{monday+1T14:00}', wednesday)).toBe('2026-11-24T14:00:00.000Z');
  });

  it('leaves everything else as written', () => {
    expect(resolveRelativeDate('Shift handover', wednesday)).toBe('Shift handover');
    expect(resolveRelativeDate('2026-11-09', wednesday)).toBe('2026-11-09');
    expect(withRelativeDates({ title: 'today', needBy: '{today+2}', qty: 3 }, wednesday)).toEqual({
      title: 'today',
      needBy: '2026-11-27',
      qty: 3,
    });
  });
});
