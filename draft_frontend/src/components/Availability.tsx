import { useId, useState, type FormEvent } from 'react'
import {
  CalendarDays,
  Check,
  ChevronLeft,
  ChevronRight,
  Clock3,
  Copy,
  Globe2,
  Pencil,
  Plus,
  RefreshCw,
  Trash2,
} from 'lucide-react'
import { api } from '../lib/api'
import { teamName } from '../lib/teamPresentation'
import type { LeagueSnapshot, Team } from '../lib/types'
import type { AvailabilityWindowInput, TeamAvailabilitySnapshot } from '../lib/availabilityTypes'
import { useAvailability } from '../lib/useAvailability'
import {
  addDays,
  browserTimeZone,
  compareInstants,
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
} from '../lib/availabilityTime'
import { Modal } from './Modal'

interface Props {
  snapshot: LeagueSnapshot
  userId: number
  busy: boolean
  mutate: (operation: () => Promise<unknown>) => Promise<boolean>
  initialTeamId?: number
  initialCompareTeamId?: number
  initialDate?: string
}
type AvailabilityRead = ReturnType<typeof useAvailability>
type Window = AvailabilityWindowInput
function validWeek(value: string): boolean {
  try {
    addDays(value, 7)
    return true
  } catch {
    return false
  }
}
function shiftedWeek(value: string, days: number): string | null {
  try {
    const next = addDays(value, days)
    return validWeek(next) ? next : null
  } catch {
    return null
  }
}
const zones = [
  'UTC',
  'America/Los_Angeles',
  'America/Denver',
  'America/Chicago',
  'America/New_York',
  'America/Phoenix',
  'America/Anchorage',
  'America/Toronto',
  'America/Sao_Paulo',
  'Pacific/Honolulu',
  'Europe/London',
  'Europe/Paris',
  'Europe/Berlin',
  'Africa/Johannesburg',
  'Asia/Dubai',
  'Asia/Kolkata',
  'Asia/Kathmandu',
  'Asia/Singapore',
  'Asia/Tokyo',
  'Australia/Perth',
  'Australia/Adelaide',
  'Australia/Sydney',
  'Pacific/Auckland',
]

function ZoneField({
  value,
  onChange,
  label,
  disabled = false,
}: {
  value: string
  onChange: (value: string) => void
  label: string
  disabled?: boolean
}) {
  const id = useId()
  return (
    <label className="field availability-zone">
      <span>
        <Globe2 size={14} /> {label}
      </span>
      <input
        list={id}
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
        spellCheck={false}
        aria-invalid={!isTimeZone(value)}
        required
      />
      <datalist id={id}>
        {Array.from(new Set([browserTimeZone(), ...zones, value]))
          .filter(isTimeZone)
          .map((zone) => (
            <option key={zone} value={zone} />
          ))}
      </datalist>
      {!isTimeZone(value) && (
        <small role="alert">Enter a time zone such as America/Los_Angeles.</small>
      )}
    </label>
  )
}

function timeLabel(instant: string, zone: string) {
  return new Intl.DateTimeFormat(undefined, {
    timeZone: zone,
    hour: 'numeric',
    minute: '2-digit',
    timeZoneName: 'shortOffset',
  }).format(new Date(instant))
}

function Windows({ windows, zone, date }: { windows: Window[]; zone: string; date: string }) {
  return (
    <ul className="availability-windows">
      {windows.map((window, index) => (
        <li
          key={index}
          title={`${formatWindow(window.starts_at, zone)} – ${formatWindow(window.ends_at, zone)}`}
        >
          <time dateTime={window.starts_at}>{timeLabel(window.starts_at, zone)}</time>
          <span>
            – {timeLabel(window.ends_at, zone)}
            {localDate(window.ends_at, zone) !== date && ' (next day)'}
          </span>
        </li>
      ))}
    </ul>
  )
}

function Week({
  windows,
  week,
  zone,
  team,
  compare,
  onAdd,
}: {
  windows: Window[]
  week: string
  zone: string
  team: Team
  compare?: { team: Team; state: TeamAvailabilitySnapshot }
  onAdd?: (date: string) => void
}) {
  const available = mergeWindows(windows)
  const other = compare ? mergeWindows(compare.state.windows) : []
  const shared = compare ? intersectWindows(available, other) : available
  const days = Array.from({ length: 7 }, (_, index) => addDays(week, index))
  const dayWindows = days.map((date) => windowsForDate(shared, date, zone))
  const hours = dayWindows
    .flat()
    .reduce(
      (sum, window) =>
        sum + (Date.parse(window.ends_at) - Date.parse(window.starts_at)) / 3_600_000,
      0,
    )
  return (
    <section
      className="card availability-calendar"
      aria-label={compare ? 'Shared availability by day' : 'Team availability by day'}
    >
      <div className="availability-calendar-heading">
        <div>
          <span className="eyebrow">{compare ? 'FIND A TIME TO PLAY' : 'WEEKLY AVAILABILITY'}</span>
          <h3>{compare ? `${teamName(team)} + ${teamName(compare.team)}` : teamName(team)}</h3>
        </div>
        <span className="availability-total">
          <Clock3 size={15} />
          {new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(hours)}{' '}
          {compare ? 'shared hours' : 'available hours'}
        </span>
      </div>
      <div className="availability-days">
        {days.map((date, index) => (
          <section
            className={`availability-day ${dayWindows[index].length ? 'has-windows' : ''}`}
            key={date}
          >
            <h4>{formatDay(date)}</h4>
            {compare && <span className="availability-shared-label">Both available</span>}
            {dayWindows[index].length ? (
              <Windows windows={dayWindows[index]} zone={zone} date={date} />
            ) : (
              <p className="availability-no-time">
                {compare ? 'No shared time' : 'No times added'}
              </p>
            )}
            {compare && (
              <details className="availability-individual">
                <summary>Team windows</summary>
                <strong>{teamName(team)}</strong>
                <Windows windows={windowsForDate(available, date, zone)} zone={zone} date={date} />
                <strong>{teamName(compare.team)}</strong>
                <Windows windows={windowsForDate(other, date, zone)} zone={zone} date={date} />
              </details>
            )}
            {onAdd && (
              <button
                className="availability-add-day"
                onClick={() => onAdd(date)}
                aria-label={`Add time on ${date}`}
              >
                <Plus size={14} />
                Add time
              </button>
            )}
          </section>
        ))}
      </div>
      <p className="availability-calendar-note">
        Times shown in {zone}. Availability helps you agree on a time; it does not book a match.
      </p>
    </section>
  )
}

function Comparison({
  team,
  windows,
  compareTeam,
  week,
  zone,
  props,
}: {
  team: Team
  windows: Window[]
  compareTeam: Team
  week: string
  zone: string
  props: Props
}) {
  const read = useAvailability({
    teamId: compareTeam.id,
    identityKey: props.userId,
    mutate: props.mutate,
  })
  return (
    <>
      {read.error && (
        <div className="error-banner" role="alert">
          <span>
            {teamName(compareTeam)}: {read.error}
          </span>
          <button
            className="button secondary"
            disabled={read.loading}
            onClick={() => void read.refresh()}
          >
            Retry
          </button>
        </div>
      )}
      {read.loading && (
        <p className="muted" role="status">
          Updating {teamName(compareTeam)}’s availability…
        </p>
      )}
      {read.state && (
        <>
          <Week
            windows={windows}
            week={week}
            zone={zone}
            team={team}
            compare={{ team: compareTeam, state: read.state }}
          />
          {!read.state.availability && (
            <p className="muted">{teamName(compareTeam)} has not shared availability yet.</p>
          )}
        </>
      )}
    </>
  )
}

type Entry = {
  start: string
  end: string
  startChoice: string
  endChoice: string
  index: number | null
  original?: Window
}
function candidates(value: string, zone: string) {
  try {
    return resolveLocalDateTime(value, zone)
  } catch {
    return []
  }
}
function Endpoint({
  label,
  value,
  choice,
  zone,
  onChange,
  onChoose,
}: {
  label: string
  value: string
  choice: string
  zone: string
  onChange: (value: string) => void
  onChoose: (value: string) => void
}) {
  const options = candidates(value, zone)
  return (
    <div>
      <label className="field">
        {label}
        <input
          type="datetime-local"
          step="any"
          value={value.replace(/(\.\d{3})\d+$/, '$1')}
          onChange={(event) => onChange(event.target.value)}
          required
        />
      </label>
      {value && !options.length && (
        <p className="availability-inline-error" role="alert">
          This local time does not exist or is invalid. Choose another time.
        </p>
      )}
      {options.length > 1 && (
        <label className="field">
          {label} occurrence
          <select value={choice} onChange={(event) => onChoose(event.target.value)} required>
            <option value="">Choose an occurrence</option>
            {options.map((option, index) => (
              <option key={option} value={option}>
                {index === 0 ? 'First' : 'Second'} — {timeLabel(option, zone)}
              </option>
            ))}
          </select>
        </label>
      )}
    </div>
  )
}

function EditAvailability({
  state,
  current,
  team,
  date,
  busy,
  blocked,
  read,
  onClose,
  onSaved,
  startAdding,
}: {
  state: TeamAvailabilitySnapshot
  current: TeamAvailabilitySnapshot
  team: Team
  date: string
  busy: boolean
  blocked: boolean
  read: AvailabilityRead
  onClose: () => void
  onSaved: () => void
  startAdding: boolean
}) {
  const [base, setBase] = useState(state)
  const [windows, setWindows] = useState<Window[]>(
    state.windows.map(({ starts_at, ends_at }) => ({ starts_at, ends_at })),
  )
  const [zone, setZone] = useState(state.availability?.time_zone ?? browserTimeZone())
  const [step, setStep] = useState(state.availability?.display_step_minutes ?? 30)
  const newEntry = (): Entry => {
    const end = new Date(`${date}T18:00:00Z`)
    end.setUTCMinutes(end.getUTCMinutes() + Math.min(step, 1440))
    return {
      start: `${date}T18:00`,
      end: end.toISOString().slice(0, 16),
      startChoice: '',
      endChoice: '',
      index: null,
    }
  }
  const [entry, setEntry] = useState<Entry | null>(startAdding ? newEntry() : null)
  const [error, setError] = useState('')
  const [confirmClear, setConfirmClear] = useState(false)
  const stale = (base.availability?.revision ?? null) !== (current.availability?.revision ?? null)
  const validZone = isTimeZone(zone)
  const displayZone = validZone ? zone : browserTimeZone()
  const loadLatest = () => {
    setBase(current)
    setWindows(current.windows.map(({ starts_at, ends_at }) => ({ starts_at, ends_at })))
    setZone(current.availability?.time_zone ?? browserTimeZone())
    setStep(current.availability?.display_step_minutes ?? 30)
    setEntry(null)
    setConfirmClear(false)
    setError('')
    read.clearError()
  }
  const applyEntry = () => {
    if (!entry || !validZone) return
    const endpoint = (value: string, choice: string, original?: string) => {
      const options = candidates(value, zone)
      const selected =
        options.length === 1 ? options[0] : options.find((option) => option === choice)
      return original && selected && compareInstants(selected, original) === 0 ? original : selected
    }
    const start = endpoint(entry.start, entry.startChoice, entry.original?.starts_at)
    const end = endpoint(entry.end, entry.endChoice, entry.original?.ends_at)
    if (!start || !end) {
      setError(
        'Enter valid start and end times, and choose an occurrence for any repeated clock time.',
      )
      return
    }
    if (compareInstants(end, start) <= 0) {
      setError('The end must be after the start. Use the following date for an overnight window.')
      return
    }
    const next = { starts_at: start, ends_at: end }
    setWindows((old) =>
      entry.index === null
        ? [...old, next]
        : old.map((window, index) => (index === entry.index ? next : window)),
    )
    setEntry(null)
    setConfirmClear(false)
    setError('')
  }
  const save = async (event: FormEvent) => {
    event.preventDefault()
    if (busy || blocked || stale || entry || !validZone || !Number.isInteger(step) || step <= 0)
      return
    if (base.windows.length && !windows.length && !confirmClear) {
      setError('Confirm that you want to clear all saved times.')
      return
    }
    if (
      await read.run(() =>
        api.setTeamAvailability({
          p_team: team.id,
          p_revision: base.availability?.revision ?? null,
          p_time_zone: zone,
          p_display_step_minutes: step,
          p_windows: windows,
        }),
      )
    )
      onSaved()
  }
  return (
    <Modal title={`Edit ${teamName(team)} availability`} onClose={onClose}>
      <form className="availability-editor" onSubmit={(event) => void save(event)}>
        <p className="muted">
          Manage saved times across all dates. Changing the time zone preserves the actual times.
          Availability is publicly readable.
        </p>
        {(error || read.error) && (
          <div className="error-banner" role="alert">
            {error || read.error}
          </div>
        )}
        {stale && (
          <div className="tournament-stale" role="alert">
            This availability changed while you were editing. Reload the saved times before making
            another change.
            <button type="button" className="button secondary" onClick={loadLatest}>
              Reload saved times
            </button>
          </div>
        )}
        <fieldset className="form-fieldset" disabled={busy || blocked || stale}>
          <div className="availability-preferences">
            <ZoneField
              label="Team time zone"
              value={zone}
              onChange={setZone}
              disabled={entry !== null}
            />
            <label className="field">
              Time increment
              <select
                value={step}
                onChange={(event) => setStep(Number(event.target.value))}
                disabled={entry !== null}
              >
                {Array.from(new Set([15, 30, 60, step]))
                  .sort((a, b) => a - b)
                  .map((value) => (
                    <option key={value} value={value}>
                      {value} minutes
                    </option>
                  ))}
              </select>
              <small>Default duration for new windows; exact saved times are preserved.</small>
            </label>
          </div>
          <div className="availability-edit-heading">
            <h3>
              Saved time windows <span>({windows.length})</span>
            </h3>
            <button
              type="button"
              className="button secondary"
              disabled={!!entry || !validZone}
              onClick={() => {
                setEntry(newEntry())
                setError('')
              }}
            >
              <Plus size={15} />
              Add window
            </button>
          </div>
          {!windows.length && (
            <p className="availability-empty-list">
              No saved windows. Add a time when you can play.
            </p>
          )}
          <ul className="availability-edit-list">
            {windows.map((window, index) => (
              <li key={index}>
                <div>
                  <strong>{formatWindow(window.starts_at, displayZone)}</strong>
                  <span>to {formatWindow(window.ends_at, displayZone)}</span>
                </div>
                <button
                  type="button"
                  className="icon-button"
                  aria-label={`Edit window ${index + 1}`}
                  disabled={!!entry || !validZone}
                  onClick={() => {
                    setEntry({
                      index,
                      start: localDateTime(window.starts_at, zone),
                      end: localDateTime(window.ends_at, zone),
                      startChoice:
                        candidates(localDateTime(window.starts_at, zone), zone).find(
                          (value) => Date.parse(value) === Date.parse(window.starts_at),
                        ) ?? '',
                      endChoice:
                        candidates(localDateTime(window.ends_at, zone), zone).find(
                          (value) => Date.parse(value) === Date.parse(window.ends_at),
                        ) ?? '',
                      original: window,
                    })
                    setError('')
                  }}
                >
                  <Pencil size={15} />
                </button>
                <button
                  type="button"
                  className="icon-button"
                  disabled={!!entry}
                  aria-label={`Remove window ${index + 1}`}
                  onClick={() => {
                    setWindows((old) => old.filter((_, i) => index !== i))
                    setConfirmClear(false)
                  }}
                >
                  <Trash2 size={15} />
                </button>
              </li>
            ))}
          </ul>
          {entry && (
            <section className="availability-entry">
              <h3>{entry.index === null ? 'New time window' : 'Edit time window'}</h3>
              <p className="muted">
                Local times in {zone}. For overnight availability, set the end to the next date.
              </p>
              <div className="availability-entry-fields">
                <Endpoint
                  label="Starts"
                  value={entry.start}
                  choice={entry.startChoice}
                  zone={zone}
                  onChange={(value) => setEntry({ ...entry, start: value, startChoice: '' })}
                  onChoose={(value) => setEntry({ ...entry, startChoice: value })}
                />
                <Endpoint
                  label="Ends"
                  value={entry.end}
                  choice={entry.endChoice}
                  zone={zone}
                  onChange={(value) => setEntry({ ...entry, end: value, endChoice: '' })}
                  onChoose={(value) => setEntry({ ...entry, endChoice: value })}
                />
              </div>
              <div className="availability-entry-actions">
                <button
                  type="button"
                  className="button secondary"
                  onClick={() => {
                    setEntry(null)
                    setError('')
                  }}
                >
                  Cancel window
                </button>
                <button type="button" className="button primary" onClick={applyEntry}>
                  <Check size={15} />
                  {entry.index === null ? 'Add to schedule' : 'Apply window'}
                </button>
              </div>
            </section>
          )}
          {!!base.windows.length && !windows.length && (
            <label className="tournament-check">
              <input
                type="checkbox"
                checked={confirmClear}
                onChange={(event) => setConfirmClear(event.target.checked)}
              />
              Clear all saved availability across every week.
            </label>
          )}
          {entry && <p className="muted">Add or cancel this window before saving the schedule.</p>}
          <div className="modal-actions">
            <button type="button" className="button secondary" onClick={onClose}>
              Cancel
            </button>
            <button
              type="submit"
              className="button primary"
              disabled={
                !!entry || !validZone || (!!base.windows.length && !windows.length && !confirmClear)
              }
            >
              Save availability
            </button>
          </div>
        </fieldset>
      </form>
    </Modal>
  )
}

function CopyWeek({
  state,
  current,
  team,
  date,
  busy,
  blocked,
  read,
  onClose,
  onSaved,
}: {
  state: TeamAvailabilitySnapshot
  current: TeamAvailabilitySnapshot
  team: Team
  date: string
  busy: boolean
  blocked: boolean
  read: AvailabilityRead
  onClose: () => void
  onSaved: (date: string) => void
}) {
  const [base, setBase] = useState(state)
  const [source, setSource] = useState(date)
  const [target, setTarget] = useState(shiftedWeek(date, 7) ?? '')
  const zone = base.availability!.time_zone
  const stale = base.availability!.revision !== current.availability?.revision
  const sourceHasWindows =
    validWeek(source) &&
    Array.from({ length: 7 }, (_, i) =>
      windowsForDate(base.windows, addDays(source, i), zone),
    ).some((windows) => windows.length)
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (
      busy ||
      blocked ||
      stale ||
      !validWeek(source) ||
      !validWeek(target) ||
      source === target ||
      !sourceHasWindows
    )
      return
    if (
      await read.run(() =>
        api.copyTeamAvailabilityWeek({
          p_team: team.id,
          p_revision: base.availability!.revision,
          p_source: source,
          p_target: target,
        }),
      )
    )
      onSaved(target)
  }
  return (
    <Modal title={`Copy ${teamName(team)}’s week`} onClose={onClose}>
      <form className="availability-editor" onSubmit={(event) => void submit(event)}>
        <p className="muted">
          Copy seven days of saved availability using local clock times in <strong>{zone}</strong>.
          Copied windows are added to the destination; existing times stay saved.
        </p>
        {read.error && (
          <div className="error-banner" role="alert">
            {read.error}
          </div>
        )}
        {stale && (
          <div className="tournament-stale" role="alert">
            Availability changed. Review the latest saved times before copying.
            <button
              type="button"
              className="button secondary"
              onClick={() => {
                setBase(current)
                read.clearError()
              }}
            >
              Use latest saved times
            </button>
          </div>
        )}
        <fieldset className="form-fieldset" disabled={busy || blocked || stale}>
          <div className="availability-entry-fields">
            <label className="field">
              Source week starts
              <input
                type="date"
                min="0001-01-01"
                max="9999-12-24"
                required
                value={source}
                onChange={(event) => setSource(event.target.value)}
              />
            </label>
            <label className="field">
              Destination week starts
              <input
                type="date"
                min="0001-01-01"
                max="9999-12-24"
                required
                value={target}
                onChange={(event) => setTarget(event.target.value)}
              />
            </label>
          </div>
          {(!validWeek(source) || !validWeek(target)) && (
            <p className="availability-inline-error">
              Choose valid week dates between 0001-01-01 and 9999-12-24.
            </p>
          )}
          {validWeek(source) && !sourceHasWindows && (
            <p className="availability-inline-error">
              There are no saved times in this source week.
            </p>
          )}
          {source === target && (
            <p className="availability-inline-error">Choose a different destination week.</p>
          )}
          <p className="muted">
            Daylight-saving changes can change UTC offsets. A copy into a skipped clock time is
            rejected; repeated clock times use the server’s standard-time interpretation.
          </p>
          <div className="modal-actions">
            <button type="button" className="button secondary" onClick={onClose}>
              Cancel
            </button>
            <button
              type="submit"
              className="button primary"
              disabled={
                !validWeek(source) || !validWeek(target) || source === target || !sourceHasWindows
              }
            >
              <Copy size={15} />
              Copy week
            </button>
          </div>
        </fieldset>
      </form>
    </Modal>
  )
}

function TeamAvailability({
  team,
  compareTeam,
  week,
  zone,
  props,
  setWeek,
}: {
  team: Team
  compareTeam?: Team
  week: string
  zone: string
  props: Props
  setWeek: (value: string) => void
}) {
  const read = useAvailability({
    teamId: team.id,
    identityKey: props.userId,
    mutate: props.mutate,
  })
  const [editor, setEditor] = useState<{
    kind: 'edit' | 'copy'
    state: TeamAvailabilitySnapshot
    date: string
    adding: boolean
  } | null>(null)
  const [notice, setNotice] = useState('')
  const owns = team.manager_user_id === props.userId
  const blocked = read.loading || !!read.readError || !owns
  const openEdit = (date: string, adding: boolean) => {
    if (!read.state || blocked || props.busy) return
    setNotice('')
    read.clearError()
    setEditor({ kind: 'edit', state: read.state, date, adding })
  }
  return (
    <>
      <div className="availability-team-toolbar">
        <p>
          {owns ? 'Your team’s schedule' : 'Team schedule · only its manager can edit'}
          {read.state?.availability && <span>Saved in {read.state.availability.time_zone}</span>}
        </p>
        <div>
          <button
            className="icon-button"
            disabled={read.loading || props.busy}
            onClick={() => void read.refresh()}
            aria-label="Refresh availability"
          >
            <RefreshCw size={16} />
          </button>
          {owns && (
            <>
              <button
                className="button secondary"
                disabled={blocked || props.busy || !read.state?.availability}
                onClick={() => {
                  if (read.state?.availability) {
                    read.clearError()
                    setNotice('')
                    setEditor({ kind: 'copy', state: read.state, date: week, adding: false })
                  }
                }}
              >
                <Copy size={15} />
                Copy week
              </button>
              <button
                className="button primary"
                disabled={blocked || props.busy || !read.state}
                onClick={() => openEdit(week, false)}
              >
                <Pencil size={15} />
                Edit availability
              </button>
            </>
          )}
        </div>
      </div>
      {notice && (
        <p className="availability-notice" role="status">
          <Check size={16} />
          {notice}
        </p>
      )}
      {read.error && !editor && (
        <div className="error-banner" role="alert">
          <span>{read.error}</span>
          <button
            className="button secondary"
            disabled={read.loading}
            onClick={() => void read.refresh()}
          >
            Retry
          </button>
        </div>
      )}
      {read.loading && (
        <p className="muted" role="status">
          Updating availability…
        </p>
      )}
      {read.state && (
        <>
          {!read.state.availability && (
            <div className="availability-empty-note">
              <CalendarDays size={20} />
              <p>
                <strong>{teamName(team)} has not shared availability yet.</strong>
                <span>
                  {owns
                    ? 'Add the times you can play, then compare with another team.'
                    : 'Check back when this team has added times.'}
                </span>
              </p>
            </div>
          )}
          {compareTeam ? (
            <Comparison
              key={compareTeam.id}
              team={team}
              windows={read.state.windows}
              compareTeam={compareTeam}
              week={week}
              zone={zone}
              props={props}
            />
          ) : (
            <Week
              team={team}
              windows={read.state.windows}
              week={week}
              zone={zone}
              onAdd={owns && !blocked && !props.busy ? (date) => openEdit(date, true) : undefined}
            />
          )}
          {editor &&
            owns &&
            (editor.kind === 'edit' ? (
              <EditAvailability
                state={editor.state}
                current={read.state}
                team={team}
                date={editor.date}
                busy={props.busy}
                blocked={blocked}
                read={read}
                onClose={() => {
                  if (!props.busy) setEditor(null)
                }}
                startAdding={editor.adding}
                onSaved={() => {
                  setEditor(null)
                  setNotice('Availability saved.')
                }}
              />
            ) : (
              <CopyWeek
                state={editor.state}
                current={read.state}
                team={team}
                date={editor.date}
                busy={props.busy}
                blocked={blocked}
                read={read}
                onClose={() => {
                  if (!props.busy) setEditor(null)
                }}
                onSaved={(date) => {
                  setEditor(null)
                  setWeek(date)
                  setNotice('Week copied. Existing destination times were kept.')
                }}
              />
            ))}
        </>
      )}
    </>
  )
}

export function Availability(props: Props) {
  const { snapshot, userId } = props
  const [teamId, setTeamId] = useState(
    props.initialTeamId ??
      snapshot.teams.find((team) => team.manager_user_id === userId)?.id ??
      snapshot.teams[0]?.id,
  )
  const [compareId, setCompareId] = useState(props.initialCompareTeamId ?? 0)
  const [zone, setZone] = useState(browserTimeZone)
  const [zoneInput, setZoneInput] = useState(zone)
  const [week, setWeek] = useState(
    props.initialDate && validWeek(props.initialDate)
      ? props.initialDate
      : weekStart(localDate(snapshot.server_now, zone)),
  )
  const [dateError, setDateError] = useState('')
  const team =
    snapshot.teams.find((item) => item.id === teamId) ??
    snapshot.teams.find((item) => item.manager_user_id === userId) ??
    snapshot.teams[0]
  const compareTeam = snapshot.teams.find((item) => item.id === compareId && item.id !== team?.id)
  return (
    <div className="availability">
      <section className="availability-hero">
        <span className="availability-hero-icon">
          <CalendarDays size={28} />
        </span>
        <div>
          <span className="eyebrow">MAKE TIME FOR YOUR NEXT MATCH</span>
          <h2>Find your overlap.</h2>
          <p>Share when you can play and find a time that works for both teams.</p>
        </div>
      </section>
      {!team ? (
        <section className="card empty-state">
          <CalendarDays size={30} />
          <h3>No teams yet</h3>
          <p>Add teams to the league to start planning availability.</p>
        </section>
      ) : (
        <>
          <section className="card availability-controls">
            <label className="field">
              Team
              <select
                aria-label="Availability team"
                value={team.id}
                disabled={props.busy}
                onChange={(event) => setTeamId(Number(event.target.value))}
              >
                {snapshot.teams.map((item) => (
                  <option key={item.id} value={item.id}>
                    {teamName(item)}
                    {item.manager_user_id === userId ? ' (your team)' : ''}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              Compare with
              <select
                aria-label="Compare availability with"
                value={compareTeam?.id ?? 0}
                disabled={props.busy}
                onChange={(event) => setCompareId(Number(event.target.value))}
              >
                <option value={0}>No comparison</option>
                {snapshot.teams
                  .filter((item) => item.id !== team.id)
                  .map((item) => (
                    <option key={item.id} value={item.id}>
                      {teamName(item)}
                    </option>
                  ))}
              </select>
            </label>
            <ZoneField
              label="Display time zone"
              value={zoneInput}
              onChange={(value) => {
                setZoneInput(value)
                if (isTimeZone(value)) setZone(value)
              }}
            />
          </section>
          <div className="availability-week-toolbar">
            <div className="availability-week-nav">
              <button
                className="icon-button"
                aria-label="Previous week"
                disabled={!shiftedWeek(week, -7)}
                onClick={() => {
                  setWeek(shiftedWeek(week, -7) ?? week)
                  setDateError('')
                }}
              >
                <ChevronLeft size={19} />
              </button>
              <label className="field">
                Week starting
                <input
                  type="date"
                  min="0001-01-01"
                  max="9999-12-24"
                  value={week}
                  required
                  onChange={(event) => {
                    if (validWeek(event.target.value)) {
                      setWeek(event.target.value)
                      setDateError('')
                    } else
                      setDateError('Choose a valid week date between 0001-01-01 and 9999-12-24.')
                  }}
                />
              </label>
              <button
                className="icon-button"
                aria-label="Next week"
                disabled={!shiftedWeek(week, 7)}
                onClick={() => {
                  setWeek(shiftedWeek(week, 7) ?? week)
                  setDateError('')
                }}
              >
                <ChevronRight size={19} />
              </button>
              <button
                className="button secondary"
                onClick={() => {
                  setWeek(weekStart(localDate(snapshot.server_now, zone)))
                  setDateError('')
                }}
              >
                This week
              </button>
            </div>
            <span className="muted">
              {formatDay(week)} – {formatDay(addDays(week, 6))}
            </span>
          </div>
          {dateError && (
            <p className="availability-inline-error" role="alert">
              {dateError}
            </p>
          )}
          <TeamAvailability
            key={`${team.id}:${userId}:${team.manager_user_id}`}
            team={team}
            compareTeam={compareTeam}
            week={week}
            zone={zone}
            props={props}
            setWeek={setWeek}
          />
        </>
      )}
    </div>
  )
}
