import { useRef, useState, type KeyboardEvent } from 'react'
import type { BaseStats, Draftable, MegaEvolution } from '../lib/types'
import { PokemonImage } from './Catalog'
import { PointsRibbon, TypePills } from './PokemonParts'

const STATS: [keyof BaseStats, string][] = [
  ['hp', 'HP'],
  ['attack', 'Attack'],
  ['defense', 'Defense'],
  ['special_attack', 'Sp. Atk'],
  ['special_defense', 'Sp. Def'],
  ['speed', 'Speed'],
]
const MAX_STAT = 255

const total = (stats: BaseStats) => STATS.reduce((sum, [key]) => sum + stats[key], 0)
const signed = (value: number) => (value > 0 ? `+${value}` : String(value))

function StatTable({ stats, base }: { stats: BaseStats; base: BaseStats | null }) {
  const change = (value: number, baseValue: number) =>
    base && value !== baseValue ? (
      <span className={`stat-change ${value < baseValue ? 'lower' : ''}`}>
        {signed(value - baseValue)}
      </span>
    ) : null
  return (
    <dl className="stat-table">
      {STATS.map(([key, label]) => (
        <div className="stat-row" key={key}>
          <dt>{label}</dt>
          <dd>
            <strong>{stats[key]}</strong>
            <span className="stat-bar" aria-hidden="true">
              <span style={{ width: `${(stats[key] / MAX_STAT) * 100}%` }} />
            </span>
            {change(stats[key], base?.[key] ?? stats[key])}
          </dd>
        </div>
      ))}
      <div className="stat-row total">
        <dt>Total</dt>
        <dd>
          <strong>{total(stats)}</strong>
          <span />
          {base && change(total(stats), total(base))}
        </dd>
      </div>
    </dl>
  )
}

/** Sprite, types, abilities, and base stats, with one tab per Mega Evolution. */
export function PokemonDetails({ item }: { item: Draftable }) {
  const [selected, setSelected] = useState(0)
  const forms: (Draftable | MegaEvolution)[] = [item, ...item.megas]
  const form = forms[selected] ?? item
  const mega = selected > 0
  // Champions treats hidden abilities like any other, so they share one list.
  const abilities = [...(form.abilities ?? []), form.hidden_ability].filter(
    (ability): ability is string => !!ability,
  )
  const tabRefs = useRef<(HTMLButtonElement | null)[]>([])
  const tabId = (index: number) => `pokemon-form-tab-${item.id}-${index}`
  const onTabKey = (event: KeyboardEvent) => {
    const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key]
    if (!step) return
    event.preventDefault()
    const next = (selected + step + forms.length) % forms.length
    setSelected(next)
    tabRefs.current[next]?.focus()
  }

  return (
    <div className="pokemon-details">
      <div className="pokemon-details-head">
        <div className={`pokemon-art color-${(item.dex ?? item.id) % 6}`}>
          <PokemonImage item={form} />
          <PointsRibbon points={item.point_cost} />
        </div>
        <div className="pokemon-details-title">
          <span className="eyebrow">
            {item.dex !== null ? `POKÉDEX #${String(item.dex).padStart(3, '0')}` : 'DRAFTABLE'}
            {item.form && ` · ${item.form}`}
          </span>
          <h2>{mega ? form.name : item.species || item.name}</h2>
          {form.types && <TypePills types={form.types} />}
        </div>
      </div>
      {item.megas.length > 0 && (
        <div className="form-tabs" role="tablist" aria-label="Forms" onKeyDown={onTabKey}>
          {forms.map((entry, index) => (
            <button
              key={index}
              ref={(node) => {
                tabRefs.current[index] = node
              }}
              id={tabId(index)}
              role="tab"
              aria-selected={index === selected}
              tabIndex={index === selected ? 0 : -1}
              className={index === selected ? 'selected' : ''}
              onClick={() => setSelected(index)}
            >
              {index ? entry.name : item.form || 'Base form'}
            </button>
          ))}
        </div>
      )}
      {form.base_stats ? (
        <div
          className="pokemon-details-body"
          role={item.megas.length ? 'tabpanel' : undefined}
          aria-labelledby={item.megas.length ? tabId(selected) : undefined}
        >
          <section>
            <h3 className="details-label">Abilities</h3>
            <ul className="ability-list">
              {abilities.map((ability) => (
                <li key={ability}>{ability}</li>
              ))}
            </ul>
          </section>
          <section>
            <h3 className="details-label">Base stats</h3>
            <StatTable stats={form.base_stats} base={mega ? item.base_stats : null} />
          </section>
        </div>
      ) : (
        item.dex !== null && (
          <p className="muted details-missing">Battle details have not been imported yet.</p>
        )
      )}
    </div>
  )
}
