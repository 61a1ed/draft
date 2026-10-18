import { describe, expect, it } from 'vitest'
import {
  addDays,
  browserTimeZone,
  formatDay,
  formatWindow,
  intersectWindows,
  isTimeZone,
  localDate,
  localDateTime,
  mergeWindows,
  resolveLocalDateTime,
  weekStart,
  windowsForDate,
  type TimeWindow,
} from './availabilityTime'

const la = 'America/Los_Angeles'
const window = (starts_at: string, ends_at: string): TimeWindow => ({ starts_at, ends_at })

describe('availability wall times', () => {
  it('converts Kathmandu quarter-hour offsets and local midnight without using the host zone', () => {
    expect(resolveLocalDateTime('2026-10-19T00:00', 'Asia/Kathmandu')).toEqual([
      '2026-10-18T18:15:00.000Z',
    ])
    expect(localDate('2026-10-18T18:15:00Z', 'Asia/Kathmandu')).toBe('2026-10-19')
    expect(localDateTime('2026-10-18T18:15:00Z', 'Asia/Kathmandu')).toBe('2026-10-19T00:00:00')
    expect(localDate('2026-10-18T18:15:00Z', la)).toBe('2026-10-18')
    expect(formatWindow('2026-10-18T18:15:00Z', 'Asia/Kathmandu')).toContain('(UTC+05:45)')
  })

  it('rejects the spring DST gap rather than silently shifting an entered appointment', () => {
    expect(resolveLocalDateTime('2026-03-08T02:30', la)).toEqual([])
    expect(resolveLocalDateTime('2026-03-08T01:59:59', la)).toEqual(['2026-03-08T09:59:59.000Z'])
    expect(resolveLocalDateTime('2026-03-08T03:00', la)).toEqual(['2026-03-08T10:00:00.000Z'])
  })

  it('offers both fall-back occurrences in chronological order with distinct offsets', () => {
    const candidates = resolveLocalDateTime('2026-11-01T01:30', la)
    expect(candidates).toEqual(['2026-11-01T08:30:00.000Z', '2026-11-01T09:30:00.000Z'])
    expect(candidates.map((value) => localDateTime(value, la))).toEqual([
      '2026-11-01T01:30:00.000',
      '2026-11-01T01:30:00.000',
    ])
    expect(formatWindow(candidates[0], la)).toContain('(UTC-07:00)')
    expect(formatWindow(candidates[1], la)).toContain('(UTC-08:00)')
  })

  it('supports half-hour transitions instead of assuming DST always changes by an hour', () => {
    expect(resolveLocalDateTime('2026-04-05T01:45', 'Australia/Lord_Howe')).toEqual([
      '2026-04-04T14:45:00.000Z',
      '2026-04-04T15:15:00.000Z',
    ])
    expect(resolveLocalDateTime('2026-10-04T02:15', 'Australia/Lord_Howe')).toEqual([])
  })

  it('preserves seconds and imported microseconds through a wall-time round trip', () => {
    const stored = '2026-10-19T18:12:45.123456+00:00'
    const input = localDateTime(stored, la)
    expect(input).toBe('2026-10-19T11:12:45.123456')
    expect(resolveLocalDateTime(input, la)).toEqual(['2026-10-19T18:12:45.123456Z'])
    expect(resolveLocalDateTime('2026-10-19T11:12:45.000001', la)).toEqual([
      '2026-10-19T18:12:45.000001Z',
    ])
  })

  it('rejects invalid dates, clock values, zones, and naive timestamps', () => {
    expect(() => resolveLocalDateTime('2026-02-30T12:00', la)).toThrow(RangeError)
    expect(() => resolveLocalDateTime('2026-10-19T24:00', la)).toThrow(RangeError)
    expect(() => resolveLocalDateTime('2026-10-19T12:00:60', la)).toThrow(RangeError)
    expect(() => resolveLocalDateTime('2026-10-19T12:00', 'Bad/Zone')).toThrow(RangeError)
    expect(() => localDateTime('2026-10-19T12:00:00', la)).toThrow(RangeError)
    expect(isTimeZone('Asia/Kathmandu')).toBe(true)
    expect(isTimeZone('UTC')).toBe(true)
    expect(isTimeZone('Bad/Zone')).toBe(false)
    expect(isTimeZone('')).toBe(false)
    expect(isTimeZone(browserTimeZone())).toBe(true)
  })
})

describe('availability calendar dates', () => {
  it('keeps Monday week boundaries across DST, month ends, year ends, and leap days', () => {
    expect(weekStart('2026-03-08')).toBe('2026-03-02')
    expect(weekStart('2026-11-01')).toBe('2026-10-26')
    expect(weekStart('2026-10-19')).toBe('2026-10-19')
    expect(addDays('2026-12-31', 1)).toBe('2027-01-01')
    expect(addDays('2024-03-01', -1)).toBe('2024-02-29')
    expect(addDays('2026-03-08', 7)).toBe('2026-03-15')
    expect(formatDay('2026-10-19')).toBe('Mon, Oct 19')
  })

  it('rejects rolled-over dates and noninteger offsets', () => {
    expect(() => addDays('2026-02-29', 1)).toThrow(RangeError)
    expect(() => addDays('2026-10-19', 0.5)).toThrow(RangeError)
    expect(() => weekStart('2026-13-01')).toThrow(RangeError)
  })
})

describe('availability interval algebra', () => {
  it('unions overlapping and adjacent windows without mutating input or losing exact boundaries', () => {
    const original = [
      window('2026-10-19T11:00:00Z', '2026-10-19T12:00:00.123456Z'),
      window('2026-10-19T09:00:00.000001Z', '2026-10-19T10:30:00Z'),
      window('2026-10-19T10:00:00Z', '2026-10-19T11:00:00Z'),
    ]
    const snapshot = structuredClone(original)
    expect(mergeWindows(original)).toEqual([
      window('2026-10-19T09:00:00.000001Z', '2026-10-19T12:00:00.123456Z'),
    ])
    expect(original).toEqual(snapshot)
    expect(() => mergeWindows([window(original[0].ends_at, original[0].starts_at)])).toThrow(
      RangeError,
    )
  })

  it('treats touching intervals as no shared time and handles unsorted overlapping inputs', () => {
    const a = [
      window('2026-10-19T10:00:00Z', '2026-10-19T12:00:00Z'),
      window('2026-10-19T09:00:00Z', '2026-10-19T11:00:00Z'),
    ]
    expect(intersectWindows(a, [window('2026-10-19T12:00:00Z', '2026-10-19T13:00:00Z')])).toEqual(
      [],
    )
    expect(intersectWindows(a, [window('2026-10-19T11:00:00Z', '2026-10-19T13:00:00Z')])).toEqual([
      window('2026-10-19T11:00:00Z', '2026-10-19T12:00:00Z'),
    ])
    expect(intersectWindows(a, [])).toEqual([])
  })

  it('does not merge microsecond gaps or discard microsecond-duration windows', () => {
    const first = window('2026-10-19T09:00:00.000001Z', '2026-10-19T09:00:00.000002Z')
    const second = window('2026-10-19T09:00:00.000003Z', '2026-10-19T09:00:00.000004Z')
    expect(mergeWindows([second, first])).toEqual([first, second])
    expect(intersectWindows([first], [first])).toEqual([first])
  })

  it('compares equivalent instants expressed with different numeric offsets', () => {
    expect(
      mergeWindows([
        window('2026-10-19T10:00:00+01:00', '2026-10-19T11:00:00+01:00'),
        window('2026-10-19T10:00:00Z', '2026-10-19T11:00:00Z'),
      ]),
    ).toEqual([window('2026-10-19T10:00:00+01:00', '2026-10-19T11:00:00Z')])
  })
})

describe('availability day clipping', () => {
  it('clips overnight windows in the viewer zone and excludes the next midnight', () => {
    const overnight = window('2026-10-19T06:30:00Z', '2026-10-19T08:30:00Z')
    expect(windowsForDate([overnight], '2026-10-18', la)).toEqual([
      window(overnight.starts_at, '2026-10-19T07:00:00.000Z'),
    ])
    expect(windowsForDate([overnight], '2026-10-19', la)).toEqual([
      window('2026-10-19T07:00:00.000Z', overnight.ends_at),
    ])
    expect(
      windowsForDate([window('2026-10-19T06:30:00Z', '2026-10-19T07:00:00Z')], '2026-10-19', la),
    ).toEqual([])
  })

  it('uses 23-hour spring days and 25-hour fall days', () => {
    const all = [window('2026-01-01T00:00:00Z', '2027-01-01T00:00:00Z')]
    expect(windowsForDate(all, '2026-03-08', la)).toEqual([
      window('2026-03-08T08:00:00.000Z', '2026-03-09T07:00:00.000Z'),
    ])
    expect(windowsForDate(all, '2026-11-01', la)).toEqual([
      window('2026-11-01T07:00:00.000Z', '2026-11-02T08:00:00.000Z'),
    ])
  })

  it('handles midnight clock changes and an entirely skipped calendar date', () => {
    const all = [window('2010-01-01T00:00:00Z', '2019-01-01T00:00:00Z')]
    expect(windowsForDate(all, '2018-11-04', 'America/Sao_Paulo')).toEqual([
      window('2018-11-04T03:00:00.000Z', '2018-11-05T02:00:00.000Z'),
    ])
    expect(windowsForDate(all, '2011-12-30', 'Pacific/Apia')).toEqual([])
  })
})
