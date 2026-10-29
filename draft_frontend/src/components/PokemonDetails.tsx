import { useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { Search, X } from 'lucide-react'
import type { BaseStats, Draftable, MegaEvolution, Move } from '../lib/types'
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

type MoveFilter = { kind: 'type' | 'category'; value: string; label: string }
const moveFilterKey = (filter: MoveFilter) => `${filter.kind}:${filter.value}`

function MovePanel({ moves, idPrefix }: { moves: Move[]; idPrefix: number }) {
  const [search, setSearch] = useState('')
  const [filters, setFilters] = useState<MoveFilter[]>([])
  const [suggestOpen, setSuggestOpen] = useState(false)
  const [highlight, setHighlight] = useState(0)

  const allFilters = useMemo(() => {
    const types = new Set<string>()
    const categories = new Set<string>()
    for (const move of moves) {
      types.add(move.type)
      categories.add(move.category)
    }
    const sorted = (values: Set<string>) => [...values].sort((a, b) => a.localeCompare(b))
    return [
      ...sorted(categories).map((value): MoveFilter => ({
        kind: 'category',
        value,
        label: `${value} category`,
      })),
      ...sorted(types).map((value): MoveFilter => ({
        kind: 'type',
        value,
        label: `${value} type`,
      })),
    ]
  }, [moves])

  const suggestions = useMemo(() => {
    const query = search.trim().toLowerCase()
    if (!query) return []
    const active = new Set(filters.map(moveFilterKey))
    return allFilters
      .filter(
        (filter) =>
          !active.has(moveFilterKey(filter)) && filter.label.toLowerCase().includes(query),
      )
      .slice(0, 8)
  }, [allFilters, filters, search])

  const showSuggestions = suggestOpen && suggestions.length > 0

  const addFilter = (filter: MoveFilter) => {
    setFilters((current) => [...current, filter])
    setSearch('')
    setSuggestOpen(false)
    setHighlight(0)
  }

  const removeFilter = (filter: MoveFilter) => {
    setFilters((current) =>
      current.filter((entry) => moveFilterKey(entry) !== moveFilterKey(filter)),
    )
  }

  const filteredMoves = useMemo(() => {
    const query = search.trim().toLowerCase()
    return moves.filter(
      (move) =>
        filters.every((filter) =>
          filter.kind === 'type' ? move.type === filter.value : move.category === filter.value,
        ) &&
        (!query ||
          move.name.toLowerCase().includes(query) ||
          move.type.toLowerCase().includes(query) ||
          move.category.toLowerCase().includes(query)),
    )
  }, [moves, filters, search])

  const listId = `pokemon-move-suggestions-${idPrefix}`

  return (
    <div className="pokemon-moves-panel">
      <div className="move-search-controls">
        <div className="search-field">
          <Search size={18} />
          <input
            type="search"
            role="combobox"
            aria-label="Search moves by name, type, or category"
            aria-autocomplete="list"
            aria-expanded={showSuggestions}
            aria-controls={listId}
            aria-activedescendant={
              showSuggestions ? `move-suggestion-${idPrefix}-${highlight}` : undefined
            }
            placeholder="Filter moves by name, type, or category..."
            value={search}
            onChange={(event) => {
              setSearch(event.target.value)
              setSuggestOpen(true)
              setHighlight(0)
            }}
            onFocus={() => setSuggestOpen(true)}
            onBlur={() => setSuggestOpen(false)}
            onKeyDown={(event) => {
              if (event.key === 'Backspace' && !search && filters.length) {
                removeFilter(filters[filters.length - 1])
              } else if (!showSuggestions) {
                return
              } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
                event.preventDefault()
                const step = event.key === 'ArrowDown' ? 1 : -1
                setHighlight((highlight + step + suggestions.length) % suggestions.length)
              } else if (event.key === 'Enter') {
                event.preventDefault()
                addFilter(suggestions[Math.min(highlight, suggestions.length - 1)])
              } else if (event.key === 'Escape') {
                setSuggestOpen(false)
              }
            }}
          />
          {search && (
            <button
              className="icon-button"
              aria-label="Clear move search"
              onClick={() => setSearch('')}
            >
              <X size={15} />
            </button>
          )}
          {showSuggestions && (
            <ul
              className="search-suggestions"
              id={listId}
              role="listbox"
              aria-label="Move search filters"
            >
              {suggestions.map((filter, index) => (
                <li
                  key={moveFilterKey(filter)}
                  id={`move-suggestion-${idPrefix}-${index}`}
                  role="option"
                  aria-selected={index === highlight}
                  className={index === highlight ? 'highlighted' : ''}
                  onMouseDown={(event) => event.preventDefault()}
                  onMouseEnter={() => setHighlight(index)}
                  onClick={() => addFilter(filter)}
                >
                  {filter.label}
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
      {filters.length > 0 && (
        <div className="search-filter-chips" aria-label="Active move filters">
          {filters.map((filter) => (
            <span className="search-filter-chip" key={moveFilterKey(filter)}>
              {filter.label}
              <button
                aria-label={`Remove ${filter.label} filter`}
                onClick={() => removeFilter(filter)}
              >
                <X size={12} />
              </button>
            </span>
          ))}
          <button className="button text-button" onClick={() => setFilters([])}>
            Clear filters
          </button>
        </div>
      )}
      {filteredMoves.length > 0 ? (
        <div className="move-table-scroll">
          <table className="move-table" aria-label="Learned moves">
            <thead>
              <tr>
                <th scope="col">Move</th>
                <th scope="col">Type</th>
                <th scope="col">Cat.</th>
                <th scope="col" className="num-col">
                  Power
                </th>
                <th scope="col" className="num-col">
                  Acc.
                </th>
                <th scope="col" className="num-col">
                  PP
                </th>
                <th scope="col">Description</th>
              </tr>
            </thead>
            <tbody>
              {filteredMoves.map((move) => (
                <tr key={move.name}>
                  <td className="move-name-cell">
                    <strong>{move.name}</strong>
                  </td>
                  <td>
                    <TypePills types={[move.type]} small />
                  </td>
                  <td>
                    <span className={`move-category-pill ${move.category.toLowerCase()}`}>
                      {move.category}
                    </span>
                  </td>
                  <td className="num-col">{move.power ?? '—'}</td>
                  <td className="num-col">{move.accuracy ? `${move.accuracy}%` : '—'}</td>
                  <td className="num-col">{move.pp ?? '—'}</td>
                  <td className="move-desc-cell">{move.description ?? ''}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="muted move-empty">No moves match the current filter.</p>
      )}
    </div>
  )
}

/** Sprite, types, abilities, base stats, and learned moves, with tabs for Megas and Moves. */
export function PokemonDetails({ item }: { item: Draftable }) {
  const [selected, setSelected] = useState(0)
  const forms: (Draftable | MegaEvolution)[] = [item, ...item.megas]
  const moves = item.moves ?? []
  const hasMoves = moves.length > 0
  const showTabs = item.megas.length > 0 || hasMoves
  const movesTabIndex = forms.length
  const totalTabs = forms.length + (hasMoves ? 1 : 0)
  const isMovesTab = hasMoves && selected === movesTabIndex
  const form = isMovesTab ? item : (forms[selected] ?? item)
  const mega = !isMovesTab && selected > 0
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
    const next = (selected + step + totalTabs) % totalTabs
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
      {showTabs && (
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
          {hasMoves && (
            <button
              ref={(node) => {
                tabRefs.current[movesTabIndex] = node
              }}
              id={tabId(movesTabIndex)}
              role="tab"
              aria-selected={isMovesTab}
              tabIndex={isMovesTab ? 0 : -1}
              className={`moves-tab ${isMovesTab ? 'selected' : ''}`.trim()}
              onClick={() => setSelected(movesTabIndex)}
            >
              Moves
            </button>
          )}
        </div>
      )}
      {isMovesTab ? (
        <div role="tabpanel" aria-labelledby={tabId(movesTabIndex)}>
          <MovePanel moves={moves} idPrefix={item.id} />
        </div>
      ) : form.base_stats ? (
        <div
          className="pokemon-details-body"
          role={showTabs ? 'tabpanel' : undefined}
          aria-labelledby={showTabs ? tabId(selected) : undefined}
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
