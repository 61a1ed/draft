import { useMemo, useState } from 'react'
import {
  ArrowDownWideNarrow,
  ArrowRight,
  Bookmark,
  Check,
  Search,
  SlidersHorizontal,
  X,
} from 'lucide-react'
import type { Draftable, LeagueSnapshot } from '../lib/types'

export function PokemonImage({ item, className = '' }: { item: Draftable; className?: string }) {
  return (
    <img
      className={className}
      src={item.image_url || '/pokeball.svg'}
      alt={item.name}
      loading="lazy"
      onError={(event) => {
        event.currentTarget.onerror = null
        event.currentTarget.src = '/pokeball.svg'
      }}
    />
  )
}

export function Catalog({
  catalog,
  snapshot,
  watchlist,
  toggleWatch,
  onSelect,
}: {
  catalog: Draftable[]
  snapshot: LeagueSnapshot
  watchlist: number[]
  toggleWatch: (id: number) => void
  onSelect: (item: Draftable) => void
}) {
  const [search, setSearch] = useState('')
  const [tab, setTab] = useState<'available' | 'all' | 'watchlist'>('available')
  const [sort, setSort] = useState('dex')
  const [affordable, setAffordable] = useState(false)
  const [limit, setLimit] = useState(12)
  const drafted = useMemo(
    () => new Set(snapshot.rosters.map((pick) => pick.draftable_id)),
    [snapshot.rosters],
  )
  const activeTeam = snapshot.teams.find((team) => team.id === snapshot.state.next_team_id)
  const remaining = activeTeam?.points_remaining
  const filtered = useMemo(
    () =>
      catalog
        .filter((item) => {
          const query = search.trim().toLowerCase()
          return (
            (tab !== 'available' || !drafted.has(item.id)) &&
            (tab !== 'watchlist' || watchlist.includes(item.id)) &&
            (!affordable || remaining === undefined || item.point_cost <= remaining) &&
            (!query ||
              item.name.toLowerCase().includes(query) ||
              String(item.dex ?? '').includes(query))
          )
        })
        .sort((a, b) =>
          sort === 'price-high'
            ? b.point_cost - a.point_cost || a.name.localeCompare(b.name)
            : sort === 'price-low'
              ? a.point_cost - b.point_cost || a.name.localeCompare(b.name)
              : sort === 'name'
                ? a.name.localeCompare(b.name)
                : (a.dex ?? a.id) - (b.dex ?? b.id) || a.id - b.id,
        ),
    [catalog, search, tab, affordable, remaining, drafted, watchlist, sort],
  )

  return (
    <section className="catalog-section" aria-label="Pokémon catalog">
      <div className="section-heading">
        <div>
          <h2>Pokémon</h2>
        </div>
        <span className="count-pill">{catalog.length - drafted.size} available</span>
      </div>
      <div className="catalog-tabs" aria-label="Catalog filter">
        {(['available', 'all', 'watchlist'] as const).map((value) => (
          <button
            key={value}
            aria-pressed={tab === value}
            className={tab === value ? 'selected' : ''}
            onClick={() => {
              setTab(value)
              setLimit(12)
            }}
          >
            {value === 'watchlist' && <Bookmark size={14} />}
            {value === 'available' ? 'Available' : value === 'all' ? 'All Pokémon' : 'Watchlist'}
            {value === 'watchlist' && <span>{watchlist.length}</span>}
          </button>
        ))}
      </div>
      <div className="catalog-controls">
        <div className="search-field">
          <Search size={18} />
          <input
            type="search"
            aria-label="Search Pokémon by name or Pokédex number"
            placeholder="Search Pokémon or Pokédex number..."
            value={search}
            onChange={(event) => {
              setSearch(event.target.value)
              setLimit(12)
            }}
          />
          {search && (
            <button className="icon-button" aria-label="Clear search" onClick={() => setSearch('')}>
              <X size={15} />
            </button>
          )}
        </div>
        <label className="sort-control">
          <ArrowDownWideNarrow size={16} />
          <select
            aria-label="Sort Pokémon"
            value={sort}
            onChange={(event) => {
              setSort(event.target.value)
              setLimit(12)
            }}
          >
            <option value="dex">Pokédex order</option>
            <option value="price-high">Points: high to low</option>
            <option value="price-low">Points: low to high</option>
            <option value="name">Name: A to Z</option>
          </select>
        </label>
        <button
          className={`filter-button ${affordable ? 'active' : ''}`}
          title="Show picks within the current team's budget"
          aria-pressed={affordable}
          disabled={!activeTeam}
          onClick={() => {
            setAffordable(!affordable)
            setLimit(12)
          }}
        >
          <SlidersHorizontal size={16} />
          <span>In budget</span>
        </button>
      </div>
      <div className="pokemon-grid">
        {filtered.slice(0, limit).map((item) => {
          const taken = drafted.has(item.id)
          const overBudget = remaining !== undefined && item.point_cost > remaining
          const watched = watchlist.includes(item.id)
          return (
            <article className={`pokemon-card ${taken ? 'taken' : ''}`} key={item.id}>
              <div className="pokemon-card-top">
                <span className="dex-number">
                  {item.dex !== null ? `#${String(item.dex).padStart(3, '0')}` : 'DRAFTABLE'}
                </span>
                <button
                  className={`bookmark-button ${watched ? 'saved' : ''}`}
                  aria-label={`${watched ? 'Remove' : 'Add'} ${item.name} ${watched ? 'from' : 'to'} watchlist`}
                  aria-pressed={watched}
                  onClick={() => toggleWatch(item.id)}
                >
                  <Bookmark size={17} fill={watched ? 'currentColor' : 'none'} />
                </button>
              </div>
              <button
                className="pokemon-main"
                onClick={() => onSelect(item)}
                aria-label={`View ${item.name}, ${item.point_cost} points${taken ? ', already drafted' : ''}`}
              >
                <div className={`pokemon-art color-${(item.dex ?? item.id) % 6}`}>
                  <PokemonImage item={item} />
                </div>
                <h3>{item.species || item.name}</h3>
                <p className="pokemon-form">
                  {item.form || (item.dex !== null ? 'Original form' : 'Draftable')}
                </p>
              </button>
              <div className="pokemon-card-footer">
                <span className={`price ${overBudget && !taken ? 'over-budget' : ''}`}>
                  {item.point_cost}
                  <small>pts</small>
                </span>
                <button className="pick-link" onClick={() => onSelect(item)}>
                  {taken ? (
                    <>
                      <Check size={13} />
                      Drafted
                    </>
                  ) : (
                    <>
                      View pick
                      <ArrowRight size={14} />
                    </>
                  )}
                </button>
              </div>
            </article>
          )
        })}
      </div>
      {filtered.length === 0 && (
        <div className="empty-state">
          <Search size={28} />
          <h3>
            {tab === 'watchlist' && watchlist.length === 0
              ? 'No saved Pokémon'
              : 'No Pokémon found'}
          </h3>
          <p>
            {tab === 'watchlist' && watchlist.length === 0
              ? 'Use the bookmark button to save a Pokémon.'
              : 'Change the search or clear the filters.'}
          </p>
          <button
            className="button secondary"
            onClick={() => {
              setSearch('')
              setAffordable(false)
              setTab('available')
            }}
          >
            Browse available Pokémon
          </button>
        </div>
      )}
      {filtered.length > 0 && (
        <div className="catalog-bottom">
          <span className="muted">
            Showing {Math.min(limit, filtered.length)} of {filtered.length} Pokémon
          </span>
          {limit < filtered.length && (
            <button className="button secondary" onClick={() => setLimit(limit + 12)}>
              Load more
              <ArrowRight size={15} />
            </button>
          )}
        </div>
      )}
    </section>
  )
}
