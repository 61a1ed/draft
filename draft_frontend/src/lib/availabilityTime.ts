export interface TimeWindow {
  starts_at: string
  ends_at: string
}

const formatters = new Map<string, Intl.DateTimeFormat>()
const dayBoundaries = new Map<string, string>()
const hour = 60 * 60 * 1000
const instantPattern = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.(\d+))?(?:Z|[+-]\d{2}:\d{2})$/i

function pad(value: number): string {
  return String(value).padStart(2, '0')
}

function utcDate(year: number, month: number, day: number): Date {
  const result = new Date(0)
  result.setUTCFullYear(year, month - 1, day)
  return result
}

function parseDate(value: string): Date {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value)
  if (!match) throw new RangeError('Enter a calendar date in YYYY-MM-DD format.')
  const [, year, month, day] = match.map(Number)
  const result = utcDate(year, month, day)
  if (
    year < 1 ||
    result.getUTCFullYear() !== year ||
    result.getUTCMonth() !== month - 1 ||
    result.getUTCDate() !== day
  ) {
    throw new RangeError('Enter a valid calendar date.')
  }
  return result
}

function dateString(value: Date): string {
  const year = value.getUTCFullYear()
  if (!Number.isFinite(value.getTime()) || year < 1 || year > 9999) {
    throw new RangeError('Calendar date is outside the supported range.')
  }
  return `${String(year).padStart(4, '0')}-${pad(value.getUTCMonth() + 1)}-${pad(value.getUTCDate())}`
}

function parseInstant(value: string): { date: Date; fraction: string } {
  const match = instantPattern.exec(value)
  const date = new Date(value)
  if (!match || !Number.isFinite(date.getTime())) {
    throw new RangeError('Enter an ISO timestamp with a UTC offset.')
  }
  return { date, fraction: match[1] ?? '' }
}

function formatter(zone: string): Intl.DateTimeFormat {
  let result = formatters.get(zone)
  if (!result) {
    if (!isTimeZone(zone)) throw new RangeError('Choose a valid IANA time zone.')
    result = new Intl.DateTimeFormat('en-CA', {
      timeZone: zone,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hourCycle: 'h23',
    })
    formatters.set(zone, result)
  }
  return result
}

function localParts(date: Date, zone: string) {
  const parts = formatter(zone).formatToParts(date)
  const part = (name: Intl.DateTimeFormatPartTypes) =>
    Number(parts.find((item) => item.type === name)?.value)
  return {
    year: part('year'),
    month: part('month'),
    day: part('day'),
    hour: part('hour'),
    minute: part('minute'),
    second: part('second'),
  }
}

function localEpoch(date: Date, zone: string): number {
  const parts = localParts(date, zone)
  const wall = utcDate(parts.year, parts.month, parts.day)
  wall.setUTCHours(parts.hour, parts.minute, parts.second)
  return wall.getTime()
}

function offsetAt(date: Date, zone: string): number {
  return localEpoch(date, zone) - Math.floor(date.getTime() / 1000) * 1000
}

function offsetLabel(date: Date, zone: string): string {
  const offset = offsetAt(date, zone) / 1000
  const absolute = Math.abs(offset)
  const seconds = absolute % 60
  return `UTC${offset < 0 ? '-' : '+'}${pad(Math.floor(absolute / 3600))}:${pad(Math.floor((absolute % 3600) / 60))}${seconds ? `:${pad(seconds)}` : ''}`
}

export function browserTimeZone(): string {
  try {
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone
    return isTimeZone(zone) ? zone : 'UTC'
  } catch {
    return 'UTC'
  }
}

export function isTimeZone(zone: string): boolean {
  if (!zone || zone.trim() !== zone || /^[+-]/.test(zone)) return false
  try {
    new Intl.DateTimeFormat('en-US', { timeZone: zone }).format(0)
    return true
  } catch {
    return false
  }
}

export function localDate(instant: string, zone: string): string {
  const parts = localParts(parseInstant(instant).date, zone)
  return `${String(parts.year).padStart(4, '0')}-${pad(parts.month)}-${pad(parts.day)}`
}

/** A datetime-local input value. Keep the original instant when an input is untouched. */
export function localDateTime(instant: string, zone: string): string {
  const { date, fraction } = parseInstant(instant)
  const parts = localParts(date, zone)
  return `${String(parts.year).padStart(4, '0')}-${pad(parts.month)}-${pad(parts.day)}T${pad(parts.hour)}:${pad(parts.minute)}:${pad(parts.second)}${fraction ? `.${fraction}` : ''}`
}

/** Empty means a DST gap; two results mean a repeated wall time, earliest first. */
export function resolveLocalDateTime(value: string, zone: string): string[] {
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?$/.exec(value)
  if (!match) throw new RangeError('Enter a local date and time.')
  const [, day, hours, minutes, seconds = '0', fraction = ''] = match
  const nominal = parseDate(day)
  if (Number(hours) > 23 || Number(minutes) > 59 || Number(seconds) > 59) {
    throw new RangeError('Enter a valid local time.')
  }
  nominal.setUTCHours(Number(hours), Number(minutes), Number(seconds))
  const wall = nominal.getTime()
  const offsets = new Set<number>()
  // Both sides of nearby IANA transitions are sampled, including half-hour DST
  // and date-line changes. Candidate round trips reject nonexistent wall times.
  for (let delta = -48; delta <= 48; delta += 6) {
    offsets.add(offsetAt(new Date(wall + delta * hour), zone))
  }
  return [...offsets]
    .map((offset) => new Date(wall - offset))
    .filter((candidate) => localEpoch(candidate, zone) === wall)
    .sort((a, b) => a.getTime() - b.getTime())
    .map((candidate) => candidate.toISOString().replace(/\.\d{3}Z$/, `.${fraction || '000'}Z`))
}

export function addDays(date: string, count: number): string {
  if (!Number.isSafeInteger(count)) throw new RangeError('Day count must be an integer.')
  const result = parseDate(date)
  result.setUTCDate(result.getUTCDate() + count)
  return dateString(result)
}

export function weekStart(date: string): string {
  const day = parseDate(date).getUTCDay()
  return addDays(date, -((day + 6) % 7))
}

export function formatDay(date: string): string {
  return new Intl.DateTimeFormat('en-US', {
    timeZone: 'UTC',
    weekday: 'short',
    month: 'short',
    day: 'numeric',
  }).format(parseDate(date))
}

export function formatWindow(instant: string, zone: string): string {
  const { date } = parseInstant(instant)
  const label = new Intl.DateTimeFormat('en-US', {
    timeZone: zone,
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  }).format(date)
  return `${label} (${offsetLabel(date, zone)})`
}

// Compare remaining fractional digits as well as Date's milliseconds, so imported
// microsecond boundaries do not become adjacent or empty due to JS truncation.
export function compareInstants(a: string, b: string): number {
  const left = parseInstant(a)
  const right = parseInstant(b)
  const milliseconds = left.date.getTime() - right.date.getTime()
  if (milliseconds !== 0) return milliseconds
  const precision = Math.max(left.fraction.length, right.fraction.length)
  const x = left.fraction.padEnd(precision, '0')
  const y = right.fraction.padEnd(precision, '0')
  return x < y ? -1 : x > y ? 1 : 0
}

export function mergeWindows(windows: readonly TimeWindow[]): TimeWindow[] {
  const sorted = windows.map(({ starts_at, ends_at }) => {
    if (compareInstants(starts_at, ends_at) >= 0) {
      throw new RangeError('Each availability window must end after it starts.')
    }
    return { starts_at, ends_at }
  })
  sorted.sort((a, b) => compareInstants(a.starts_at, b.starts_at))
  const result: TimeWindow[] = []
  for (const window of sorted) {
    const previous = result.at(-1)
    if (previous && compareInstants(window.starts_at, previous.ends_at) <= 0) {
      if (compareInstants(window.ends_at, previous.ends_at) > 0) previous.ends_at = window.ends_at
    } else {
      result.push(window)
    }
  }
  return result
}

export function intersectWindows(a: readonly TimeWindow[], b: readonly TimeWindow[]): TimeWindow[] {
  const left = mergeWindows(a)
  const right = mergeWindows(b)
  const result: TimeWindow[] = []
  let i = 0
  let j = 0
  while (i < left.length && j < right.length) {
    const start =
      compareInstants(left[i].starts_at, right[j].starts_at) >= 0
        ? left[i].starts_at
        : right[j].starts_at
    const end =
      compareInstants(left[i].ends_at, right[j].ends_at) <= 0 ? left[i].ends_at : right[j].ends_at
    if (compareInstants(start, end) < 0) result.push({ starts_at: start, ends_at: end })
    if (compareInstants(left[i].ends_at, right[j].ends_at) < 0) i += 1
    else j += 1
  }
  return result
}

function startOfDay(date: string, zone: string): string {
  const key = `${zone}/${date}`
  const cached = dayBoundaries.get(key)
  if (cached) return cached
  const midnight = resolveLocalDateTime(`${date}T00:00`, zone)[0]
  let result = midnight
  if (!result) {
    // Some zones move their clocks at midnight, and a date-line change can
    // skip an entire date. Find the first instant on or after the requested day.
    const nominal = parseDate(date).getTime()
    let low = nominal - 48 * hour
    let high = nominal + 48 * hour
    while (high - low > 1000) {
      const middle = Math.floor((low + high) / 2000) * 1000
      if (localDate(new Date(middle).toISOString(), zone) < date) low = middle
      else high = middle
    }
    result = new Date(high).toISOString()
  }
  if (dayBoundaries.size >= 512) dayBoundaries.clear()
  dayBoundaries.set(key, result)
  return result
}

/** Clip the union of windows to a viewer's calendar day; end boundaries are exclusive. */
export function windowsForDate(
  windows: readonly TimeWindow[],
  date: string,
  zone: string,
): TimeWindow[] {
  const starts_at = startOfDay(date, zone)
  const ends_at = startOfDay(addDays(date, 1), zone)
  if (compareInstants(starts_at, ends_at) >= 0) return []
  return intersectWindows(windows, [{ starts_at, ends_at }])
}
