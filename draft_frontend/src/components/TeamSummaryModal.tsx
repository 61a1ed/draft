import { useEffect, useMemo, useState } from 'react'
import { Check, ClipboardCopy, Plus, Users } from 'lucide-react'
import type { Draftable, LeagueSnapshot, RosterPick, Team, User } from '../lib/types'
import { teamAvatarStyle, teamName } from '../lib/teamPresentation'
import { PokemonImage } from './Catalog'
import { Modal } from './Modal'
import { PokemonDetails } from './PokemonDetails'
import { TypePills } from './PokemonParts'

const MAX_SHOWDOWN_EXPORT = 6

const FORM_TO_SHOWDOWN_SUFFIX: Record<string, string> = {
  Alolan: '-Alola',
  Galarian: '-Galar',
  Hisuian: '-Hisui',
  'Paldean (Combat Breed)': '-Paldea-Combat',
  'Paldean (Blaze Breed)': '-Paldea-Blaze',
  'Paldean (Aqua Breed)': '-Paldea-Aqua',
  Heat: '-Heat',
  Wash: '-Wash',
  Frost: '-Frost',
  Fan: '-Fan',
  Mow: '-Mow',
  Male: '',
  Female: '-F',
  Midday: '',
  Midnight: '-Midnight',
  Dusk: '-Dusk',
  Medium: '',
  Small: '-Small',
  Large: '-Large',
  Jumbo: '-Super',
  Amped: '',
  'Low Key': '-Low-Key',
  'Green Plumage': '',
  'Blue Plumage': '-Blue',
  'Yellow Plumage': '-Yellow',
  'White Plumage': '-White',
}

export function toShowdownSpeciesName(item: Draftable): string {
  const species = item.species?.trim()
  const form = item.form?.trim() ?? ''
  if (!species) return item.name
  if (!form) return species
  if (form in FORM_TO_SHOWDOWN_SUFFIX) {
    return `${species}${FORM_TO_SHOWDOWN_SUFFIX[form]}`
  }
  return `${species}-${form}`
}

export function formatShowdownPokepaste(items: Draftable[]): string {
  return items
    .slice(0, MAX_SHOWDOWN_EXPORT)
    .map((item) => {
      const lines = [toShowdownSpeciesName(item)]
      const ability = item.abilities?.[0] || item.hidden_ability
      if (ability) {
        lines.push(`Ability: ${ability}`)
      }
      lines.push('Level: 50')
      return lines.join('\n')
    })
    .join('\n\n')
}

function initials(name: string) {
  return name
    .split(' ')
    .slice(0, 2)
    .map((word) => word[0])
    .join('')
    .toUpperCase()
}

export function TeamSummaryModal({
  team,
  snapshot,
  catalog,
  users = [],
  userId,
  onClose,
}: {
  team: Team
  snapshot: LeagueSnapshot
  catalog: Draftable[]
  users?: User[]
  userId?: number
  onClose: () => void
}) {
  const catalogById = useMemo(() => new Map(catalog.map((entry) => [entry.id, entry])), [catalog])

  const picks = useMemo(
    () =>
      snapshot.rosters
        .filter((pick) => pick.team_id === team.id)
        .sort((a, b) => a.pick_number - b.pick_number),
    [snapshot.rosters, team.id],
  )

  const [selectedPickId, setSelectedPickId] = useState<number | null>(() => picks[0]?.pick_id ?? null)
  const [exportedPickIds, setExportedPickIds] = useState<number[]>(() =>
    picks.slice(0, MAX_SHOWDOWN_EXPORT).map((pick) => pick.pick_id),
  )
  const [showPreview, setShowPreview] = useState(false)
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    setSelectedPickId(picks[0]?.pick_id ?? null)
    setExportedPickIds(picks.slice(0, MAX_SHOWDOWN_EXPORT).map((pick) => pick.pick_id))
    setShowPreview(false)
    setCopied(false)
  }, [team.id])

  const activePick: RosterPick | undefined =
    picks.find((pick) => pick.pick_id === selectedPickId) ?? picks[0]
  const activeItem = activePick ? catalogById.get(activePick.draftable_id) ?? null : null

  const exportedSet = useMemo(() => new Set(exportedPickIds), [exportedPickIds])
  const exportedItems = useMemo(
    () =>
      picks
        .filter((pick) => exportedSet.has(pick.pick_id))
        .map((pick) => catalogById.get(pick.draftable_id))
        .filter((item): item is Draftable => Boolean(item))
        .slice(0, MAX_SHOWDOWN_EXPORT),
    [picks, exportedSet, catalogById],
  )

  const pokepasteText = useMemo(() => formatShowdownPokepaste(exportedItems), [exportedItems])

  const toggleExportPick = (pickId: number) => {
    setCopied(false)
    setExportedPickIds((current) => {
      if (current.includes(pickId)) {
        return current.filter((id) => id !== pickId)
      }
      if (current.length >= MAX_SHOWDOWN_EXPORT) {
        return current
      }
      return [...current, pickId]
    })
  }

  const copyPokepaste = async () => {
    if (!pokepasteText) return
    try {
      await navigator.clipboard?.writeText(pokepasteText)
    } catch {
      /* Clipboard permissions may be unavailable in some environments. */
    }
    setCopied(true)
  }

  const manager = users.find((u) => u.id === team.manager_user_id)
  const isMyTeam = userId !== undefined && team.manager_user_id === userId
  const slotCount = Math.max(snapshot.league.roster_size, picks.length)
  const displayTitle = teamName(team)

  return (
    <Modal title={`${displayTitle} — Team Summary`} onClose={onClose}>
      <div className="team-summary-modal">
        <div className="team-summary-banner">
          <div className="team-summary-identity">
            <span className="team-avatar" style={teamAvatarStyle(team.color_rgb)}>
              {initials(displayTitle)}
            </span>
            <div>
              <div className="team-summary-title-row">
                <h3>{displayTitle}</h3>
                {isMyTeam && <span className="you-label">Your team</span>}
                {team.draft_position !== null && (
                  <span className="mini-pill">Slot #{String(team.draft_position).padStart(2, '0')}</span>
                )}
              </div>
              <p className="muted">
                {manager ? `Manager: ${manager.display_name} · ` : ''}
                {picks.length} / {snapshot.league.roster_size} Pokémon drafted
                {team.roster_locked ? ' · Roster locked' : ''}
              </p>
            </div>
          </div>
          <div className="team-summary-kpis">
            <div className="team-summary-kpi">
              <small>Points spent</small>
              <strong>
                {team.points_spent} <span className="muted">/ {team.point_budget}</span>
              </strong>
            </div>
            <div className="team-summary-kpi">
              <small>Points left</small>
              <strong>{team.points_remaining} pts</strong>
            </div>
          </div>
        </div>

        <div className="team-summary-layout">
          <div className="team-summary-party-col">
            <div className="team-summary-section-head">
              <h4>
                Drafted Party ({picks.length}/{snapshot.league.roster_size})
              </h4>
              <span className="team-export-counter">
                Showdown export: <strong>{exportedItems.length} / {MAX_SHOWDOWN_EXPORT}</strong>
              </span>
            </div>

            <div className="team-party-grid" role="list" aria-label="Team roster slots">
              {Array.from({ length: slotCount }, (_, index) => {
                const pick = picks[index]
                if (!pick) {
                  return (
                    <div
                      className="team-party-slot empty"
                      key={`empty-${index}`}
                      role="listitem"
                    >
                      <span className="slot-placeholder">
                        <Plus size={16} />
                      </span>
                      <span>Open slot #{index + 1}</span>
                    </div>
                  )
                }

                const item = catalogById.get(pick.draftable_id)
                const isSelected = activePick?.pick_id === pick.pick_id
                const isChecked = exportedSet.has(pick.pick_id)
                const isCheckDisabled = !isChecked && exportedPickIds.length >= MAX_SHOWDOWN_EXPORT
                const pokeName = item?.name ?? `Pick #${pick.pick_number}`

                return (
                  <div
                    key={pick.pick_id}
                    role="listitem"
                    className={`team-party-slot filled ${isSelected ? 'selected' : ''}`}
                  >
                    <button
                      type="button"
                      className="team-party-select-btn"
                      aria-pressed={isSelected}
                      aria-label={`Inspect ${pokeName}`}
                      onClick={() => setSelectedPickId(pick.pick_id)}
                    >
                      <div className="team-party-sprite">
                        <span className="team-party-pick-tag">#{pick.pick_number}</span>
                        {item ? (
                          <PokemonImage item={item} />
                        ) : (
                          <span className="slot-placeholder">?</span>
                        )}
                      </div>
                      <div className="team-party-info">
                        <div className="team-party-top">
                          <strong className="team-party-name">{pokeName}</strong>
                          <span className="team-party-cost">{pick.point_cost} pts</span>
                        </div>
                        <div className="team-party-badges">
                          {item?.types && item.types.length > 0 && (
                            <TypePills types={item.types} small />
                          )}
                          {item && item.megas.length > 0 && (
                            <span className="team-party-mega">MEGA</span>
                          )}
                        </div>
                      </div>
                    </button>
                    <label
                      className={`team-party-export-check ${isChecked ? 'checked' : ''} ${isCheckDisabled ? 'disabled' : ''}`}
                      title={
                        isCheckDisabled
                          ? `Maximum ${MAX_SHOWDOWN_EXPORT} Pokémon per Showdown team`
                          : 'Include in Showdown Pokepaste export'
                      }
                    >
                      <input
                        type="checkbox"
                        checked={isChecked}
                        disabled={isCheckDisabled}
                        aria-label={`Export ${pokeName} to Showdown`}
                        onChange={() => toggleExportPick(pick.pick_id)}
                      />
                      <span>Export</span>
                    </label>
                  </div>
                )
              })}
            </div>

            <div className="team-summary-export-bar">
              <div className="team-summary-export-meta">
                <span>
                  <strong>Showdown Pokepaste</strong> (max {MAX_SHOWDOWN_EXPORT} Pokémon)
                </span>
                {picks.length > 0 && (
                  <button
                    type="button"
                    className="button text-button"
                    onClick={() => setShowPreview((open) => !open)}
                  >
                    {showPreview ? 'Hide preview' : 'Preview text'}
                  </button>
                )}
              </div>
              {showPreview && (
                <pre className="team-summary-paste-preview" aria-label="Showdown Pokepaste preview">
                  {pokepasteText || 'No Pokémon selected for export.'}
                </pre>
              )}
              <button
                type="button"
                className="button primary full-width"
                disabled={exportedItems.length === 0}
                onClick={() => void copyPokepaste()}
              >
                {copied ? (
                  <>
                    <Check size={16} />
                    Copied Showdown Pokepaste!
                  </>
                ) : (
                  <>
                    <ClipboardCopy size={16} />
                    Copy Showdown Pokepaste ({exportedItems.length})
                  </>
                )}
              </button>
            </div>
          </div>

          <div className="team-summary-spotlight-col" aria-label="Selected Pokémon spotlight">
            {activeItem ? (
              <div className="team-summary-spotlight-card">
                <PokemonDetails key={activeItem.id} item={activeItem} />
              </div>
            ) : (
              <div className="card empty-state team-summary-empty-spotlight">
                <Users size={28} />
                <h3>No Pokémon drafted yet</h3>
                <p>Once {displayTitle} makes a pick, you can inspect its stats and moves here.</p>
              </div>
            )}
          </div>
        </div>
      </div>
    </Modal>
  )
}
