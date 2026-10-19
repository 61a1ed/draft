import { useState, type FormEvent } from 'react'
import { Check, Save } from 'lucide-react'
import { api, ApiError, errorMessage } from '../lib/api'
import { teamAvatarStyle, teamColor, teamName } from '../lib/teamPresentation'
import type { LeagueSnapshot, Team } from '../lib/types'

interface Props {
  team: Team
  snapshot: LeagueSnapshot
  busy: boolean
  onMutation: (operation: () => Promise<unknown>) => Promise<unknown>
}

interface Draft {
  revision: number
  name: string
  color: string
}

const presets = [
  ['Blue', '#3b82f6'],
  ['Purple', '#8b5cf6'],
  ['Pink', '#ec4899'],
  ['Red', '#ef4444'],
  ['Orange', '#f97316'],
  ['Green', '#22c55e'],
  ['Teal', '#14b8a6'],
  ['Black', '#000000'],
] as const

export function TeamMetadata({ team, snapshot, busy, onMutation }: Props) {
  const [draft, setDraft] = useState<Draft | null>(null)
  const [submitted, setSubmitted] = useState<Draft | null>(null)
  const [saving, setSaving] = useState(false)
  const [feedback, setFeedback] = useState('')
  const [error, setError] = useState('')
  const [conflict, setConflict] = useState(false)
  const canEdit = snapshot.state.status === 'setup' && team.can_set_metadata
  const savedColor = teamColor(team.color_rgb) ?? ''
  // A write can succeed while the following read fails. Keep the confirmed values
  // visible and prevent a second write until an authoritative newer snapshot arrives.
  const awaitingSnapshot = submitted !== null && snapshot.revision <= submitted.revision
  const values =
    canEdit && draft
      ? draft
      : awaitingSnapshot
        ? submitted
        : { name: team.name ?? '', color: savedColor }
  const validColor = /^#[\da-f]{6}$/i.test(values.color.trim())
  const color = validColor ? Number.parseInt(values.color.trim().slice(1), 16) : null
  const stale = conflict || (draft !== null && draft.revision !== snapshot.revision)
  const changed = values.name.trim() !== (team.name ?? '') || color !== team.color_rgb
  const disabled = busy || saving || stale || awaitingSnapshot
  const savedComplete = awaitingSnapshot || team.metadata_complete
  const previewName = values.name.trim() || teamName({ ...team, name: null })

  const edit = (change: Partial<Pick<Draft, 'name' | 'color'>>) => {
    setFeedback('')
    setError('')
    setDraft((previous) => ({
      ...(previous ?? {
        revision: snapshot.revision,
        name: team.name ?? '',
        color: savedColor,
      }),
      ...change,
    }))
  }

  const reload = () => {
    setDraft(null)
    setConflict(false)
    setError('')
    setFeedback('Latest saved team loaded. Review it before making changes.')
  }

  const save = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!canEdit || disabled || !values.name.trim() || color === null || !changed) return
    const input = {
      p_league: snapshot.league_id,
      p_revision: draft?.revision ?? snapshot.revision,
      p_team: team.id,
      p_name: values.name.trim(),
      p_color_rgb: color,
    }
    setSaving(true)
    setError('')
    setFeedback('')
    let completed = false
    try {
      const result = await onMutation(async () => {
        try {
          const response = await api.setTeamMetadata(input)
          completed = true
          return response
        } catch (cause) {
          setError(errorMessage(cause))
          if (cause instanceof ApiError && cause.code === 'stale_revision') setConflict(true)
          throw cause
        }
      })
      if (completed && result !== false) {
        setSubmitted({
          revision: input.p_revision,
          name: input.p_name,
          color: teamColor(input.p_color_rgb)!,
        })
        setDraft(null)
        setFeedback('Team saved.')
      }
    } catch (cause) {
      setError(errorMessage(cause))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="team-metadata">
      <div className="team-metadata-preview">
        <span className="team-avatar" style={teamAvatarStyle(color)} aria-hidden="true">
          {previewName.slice(0, 2).toUpperCase()}
        </span>
        <div>
          <strong>{previewName}</strong>
          <p className="muted">
            {canEdit && draft ? 'Unsaved preview' : 'Saved team'}
            {' · '}
            {color === null ? 'No color chosen' : teamColor(color)?.toUpperCase()}
          </p>
        </div>
      </div>
      <p className={`team-metadata-status ${savedComplete ? 'is-complete' : ''}`}>
        {savedComplete && <Check size={15} aria-hidden="true" />}
        Saved status: {savedComplete ? 'Ready for draft' : 'Needs a name and color'}
      </p>
      {canEdit ? (
        <>
          {stale && (
            <div className="team-metadata-conflict" role="alert">
              <p>
                The league changed while you were editing. Your changes are kept below. Reload the
                saved team and review it before saving.
              </p>
              <button
                type="button"
                className="button secondary"
                disabled={busy || saving || (conflict && snapshot.revision === draft?.revision)}
                onClick={reload}
              >
                Reload saved team
              </button>
              {conflict && snapshot.revision === draft?.revision && (
                <p className="muted">Waiting for the latest league state. Refresh if needed.</p>
              )}
            </div>
          )}
          <form onSubmit={(event) => void save(event)}>
            <fieldset disabled={disabled} className="form-fieldset">
              <div className="form-grid team-metadata-fields">
                <label className="field">
                  <span>Team name</span>
                  <input
                    value={values.name}
                    onChange={(event) => edit({ name: event.target.value })}
                    placeholder="Choose your team name"
                    required
                    pattern=".*\S.*"
                  />
                </label>
                <label className="field">
                  <span>Team color</span>
                  <div className="team-color-inputs">
                    <input
                      type="color"
                      aria-label="Pick a custom team color"
                      value={validColor ? values.color.trim() : '#000000'}
                      onChange={(event) => edit({ color: event.target.value })}
                    />
                    <input
                      type="text"
                      aria-label="Team color hex code"
                      value={values.color}
                      onChange={(event) => edit({ color: event.target.value })}
                      placeholder="#1A2B3C"
                      pattern="#[a-fA-F0-9]{6}"
                      maxLength={7}
                      spellCheck={false}
                      required
                    />
                  </div>
                </label>
              </div>
              <div className="team-color-presets" role="group" aria-label="Team color presets">
                {presets.map(([label, hex]) => (
                  <button
                    type="button"
                    key={hex}
                    className="team-color-swatch"
                    aria-label={`Use ${label.toLowerCase()}`}
                    title={label}
                    aria-pressed={values.color.toLowerCase() === hex}
                    style={teamAvatarStyle(Number.parseInt(hex.slice(1), 16))}
                    onClick={() => edit({ color: hex })}
                  >
                    {values.color.toLowerCase() === hex && <Check size={16} aria-hidden="true" />}
                  </button>
                ))}
              </div>
              <p className="muted">Choose a preset or enter any six-digit hex color.</p>
              <div className="form-actions">
                <button
                  type="submit"
                  className="button primary"
                  disabled={disabled || !values.name.trim() || color === null || !changed}
                >
                  <Save size={16} aria-hidden="true" />
                  {saving ? 'Saving team…' : 'Save team details'}
                </button>
                {draft && !stale && (
                  <button type="button" className="button secondary" onClick={reload}>
                    Discard changes
                  </button>
                )}
              </div>
            </fieldset>
          </form>
        </>
      ) : (
        <p className="muted">
          {snapshot.state.status === 'setup'
            ? 'Team details are not available to edit with your current permissions.'
            : 'Team name and color are locked once the draft begins.'}
        </p>
      )}
      {feedback && (
        <p className="team-metadata-feedback" role="status">
          {feedback}
          {awaitingSnapshot && ' Waiting for refreshed league details.'}
        </p>
      )}
      {error && !stale && (
        <p className="team-metadata-error" role="alert">
          {error}
        </p>
      )}
    </div>
  )
}
