import { useEffect, useMemo, useRef, useState } from 'react'
import {
  BarChart3,
  Bookmark,
  Check,
  ChevronDown,
  ChevronUp,
  Search,
  SlidersHorizontal,
  X,
} from 'lucide-react'
import type { Draftable, LeagueSnapshot } from '../lib/types'
import type { FavoritesController } from '../lib/useFavorites'
import {
  buildAllFilters,
  filterKey,
  INITIAL_CATALOG_FILTERS,
  matchesCatalogQuery,
  matchesFilter,
  type CatalogFilterState,
  type SearchFilter,
} from '../lib/catalogFilters'
import { PokemonImage } from './Catalog'

function tierStatusClass(availableCount: number, totalCount: number) {
  if (availableCount === 0) return 'depleted'
  if (availableCount <= 2 || availableCount / totalCount <= 0.2) return 'critical'
  if (availableCount / totalCount <= 0.5) return 'thinning'
  return 'healthy'
}

export function BigBoard({
  catalog,
  snapshot,
  favorites,
  onSelect,
  filterState,
  onFilterStateChange,
}: {
  catalog: Draftable[]
  snapshot: LeagueSnapshot | null
  favorites?: FavoritesController
  onSelect: (item: Draftable) => void
  filterState?: CatalogFilterState
  onFilterStateChange?: (
    updater: CatalogFilterState | ((current: CatalogFilterState) => CatalogFilterState),
  ) => void
}) {
  const [localFilterState, setLocalFilterState] =
    useState<CatalogFilterState>(INITIAL_CATALOG_FILTERS)
  const activeFilterState = filterState ?? localFilterState
  const updateFilterState = (
    updater: CatalogFilterState | ((current: CatalogFilterState) => CatalogFilterState),
  ) => {
    if (onFilterStateChange) onFilterStateChange(updater)
    else setLocalFilterState(updater)
  }
  const { search, filters, affordable } = activeFilterState
  const setSearch = (next: string) => updateFilterState((current) => ({ ...current, search: next }))
  const setFilters = (updater: SearchFilter[] | ((current: SearchFilter[]) => SearchFilter[])) =>
    updateFilterState((current) => ({
      ...current,
      filters: typeof updater === 'function' ? updater(current.filters) : updater,
    }))
  const setAffordable = (next: boolean) =>
    updateFilterState((current) => ({ ...current, affordable: next }))

  const [suggestOpen, setSuggestOpen] = useState(false)
  const [highlight, setHighlight] = useState(0)
  const [minimapOpen, setMinimapOpen] = useState(true)

  const topScrollRef = useRef<HTMLDivElement | null>(null)
  const boardScrollRef = useRef<HTMLDivElement | null>(null)
  const syncingScrollRef = useRef<'top' | 'board' | null>(null)
  const columnRefs = useRef<Map<number, HTMLDivElement>>(new Map())
  const [scrollTrackWidth, setScrollTrackWidth] = useState(0)

  const drafted = useMemo(
    () => new Set(snapshot?.rosters.map((pick) => pick.draftable_id) ?? []),
    [snapshot?.rosters],
  )
  const saved = useMemo(
    () => new Set(favorites?.state?.entries.map((entry) => entry.draftable_id) ?? []),
    [favorites?.state?.entries],
  )
  const activeTeam = snapshot?.teams.find((team) => team.id === snapshot.state.next_team_id)
  const remaining = activeTeam?.points_remaining

  const allFilters = useMemo(() => buildAllFilters(catalog), [catalog])
  const suggestions = useMemo(() => {
    const query = search.trim().toLowerCase()
    if (!query) return []
    const active = new Set(filters.map(filterKey))
    return allFilters
      .filter(
        (filter) => !active.has(filterKey(filter)) && filter.label.toLowerCase().includes(query),
      )
      .slice(0, 8)
  }, [allFilters, filters, search])
  const showSuggestions = suggestOpen && suggestions.length > 0

  const addFilter = (filter: SearchFilter) => {
    setFilters((current) => [...current, filter])
    setSearch('')
    setSuggestOpen(false)
    setHighlight(0)
  }
  const removeFilter = (filter: SearchFilter) => {
    setFilters((current) => current.filter((entry) => filterKey(entry) !== filterKey(filter)))
  }

  const filtered = useMemo(
    () =>
      catalog
        .filter(
          (item) =>
            (!affordable || remaining === undefined || item.point_cost <= remaining) &&
            filters.every((filter) => matchesFilter(item, filter)) &&
            matchesCatalogQuery(item, search),
        )
        .sort((a, b) => (a.dex ?? a.id) - (b.dex ?? b.id) || a.id - b.id),
    [catalog, affordable, remaining, filters, search],
  )

  const tiers = useMemo(() => {
    const map = new Map<number, Draftable[]>()
    for (const item of filtered) {
      const list = map.get(item.point_cost) ?? []
      list.push(item)
      map.set(item.point_cost, list)
    }
    return [...map.entries()]
      .sort(([costA], [costB]) => costB - costA)
      .map(([cost, items]) => {
        const availableCount = items.filter((item) => !drafted.has(item.id)).length
        return { cost, items, availableCount, totalCount: items.length }
      })
  }, [filtered, drafted])

  const totalAvailable = useMemo(
    () => catalog.filter((item) => !drafted.has(item.id)).length,
    [catalog, drafted],
  )

  // Measure the board scrollWidth so the top scrollbar matches its exact scrollable width
  useEffect(() => {
    const board = boardScrollRef.current
    if (!board) return
    const updateWidth = () => {
      setScrollTrackWidth(board.scrollWidth)
    }
    updateWidth()
    if (typeof ResizeObserver !== 'undefined') {
      const observer = new ResizeObserver(updateWidth)
      observer.observe(board)
      return () => observer.disconnect()
    }
  }, [tiers.length])

  const handleTopScroll = () => {
    if (syncingScrollRef.current === 'board') {
      syncingScrollRef.current = null
      return
    }
    const top = topScrollRef.current
    const board = boardScrollRef.current
    if (!top || !board) return
    syncingScrollRef.current = 'top'
    board.scrollLeft = top.scrollLeft
  }

  const handleBoardScroll = () => {
    if (syncingScrollRef.current === 'top') {
      syncingScrollRef.current = null
      return
    }
    const top = topScrollRef.current
    const board = boardScrollRef.current
    if (!top || !board) return
    syncingScrollRef.current = 'board'
    top.scrollLeft = board.scrollLeft
  }

  const scrollToTier = (cost: number, index: number) => {
    const board = boardScrollRef.current
    const top = topScrollRef.current
    const col = columnRefs.current.get(cost)
    if (col && typeof col.scrollIntoView === 'function') {
      col.scrollIntoView({ behavior: 'smooth', inline: 'start', block: 'nearest' })
    }
    if (board) {
      const targetLeft = col?.offsetLeft ?? index * 196
      if (typeof board.scrollTo === 'function') {
        board.scrollTo({ left: targetLeft, behavior: 'smooth' })
      } else {
        board.scrollLeft = targetLeft
      }
      if (top) top.scrollLeft = targetLeft
    }
  }

  const fallbackTrackWidth = tiers.length > 0 ? tiers.length * 196 - 12 : 0
  const effectiveTrackWidth = scrollTrackWidth || fallbackTrackWidth

  return (
    <section className="big-board-section" aria-label="Draft Big Board">
      <div className="section-heading">
        <h2>Draft Board</h2>
        <span className="count-pill">
          {totalAvailable} {snapshot ? 'available' : 'Pokémon'}
        </span>
      </div>
      <div className="sticky-filter-bar">
        <div className="catalog-controls">
          <div className="search-field">
            <Search size={18} />
            <input
              type="search"
              role="combobox"
              aria-label="Search Pokémon by name or Pokédex number"
              aria-autocomplete="list"
              aria-expanded={showSuggestions}
              aria-controls="big-board-search-suggestions"
              aria-activedescendant={
                showSuggestions ? `big-board-suggestion-${highlight}` : undefined
              }
              placeholder="Search name, Pokédex number, type, ability, or move..."
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
                aria-label="Clear search"
                onClick={() => setSearch('')}
              >
                <X size={15} />
              </button>
            )}
            {showSuggestions && (
              <ul
                className="search-suggestions"
                id="big-board-search-suggestions"
                role="listbox"
                aria-label="Search filters"
              >
                {suggestions.map((filter, index) => (
                  <li
                    key={filterKey(filter)}
                    id={`big-board-suggestion-${index}`}
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
          <button
            className={`filter-button ${affordable ? 'active' : ''}`}
            title="Show picks within the current team's budget"
            aria-pressed={affordable}
            disabled={!activeTeam}
            onClick={() => {
              setAffordable(!affordable)
            }}
          >
            <SlidersHorizontal size={16} />
            <span>In budget</span>
          </button>
        </div>
        {(filters.length > 0 || affordable) && (
          <div className="search-filter-chips" aria-label="Active search filters">
            {filters.map((filter) => (
              <span className="search-filter-chip" key={filterKey(filter)}>
                {filter.label}
                <button
                  aria-label={`Remove ${filter.label} filter`}
                  onClick={() => removeFilter(filter)}
                >
                  <X size={12} />
                </button>
              </span>
            ))}
            {affordable && (
              <span className="search-filter-chip">
                In budget
                <button aria-label="Remove In budget filter" onClick={() => setAffordable(false)}>
                  <X size={12} />
                </button>
              </span>
            )}
            <button
              className="button clear-filters-button"
              onClick={() => {
                setSearch('')
                setFilters([])
                setAffordable(false)
              }}
            >
              Clear filters
            </button>
          </div>
        )}
      </div>
      {tiers.length > 0 ? (
        <>
          <div className="equalizer-minimap" aria-label="Pool equalizer minimap">
            <div className="equalizer-header">
              <button
                type="button"
                className="equalizer-toggle"
                aria-expanded={minimapOpen}
                aria-label={minimapOpen ? 'Collapse pool minimap' : 'Expand pool minimap'}
                onClick={() => setMinimapOpen((open) => !open)}
              >
                <BarChart3 size={15} />
                <strong>Pool Equalizer Minimap</strong>
                <span className="equalizer-hint">{minimapOpen ? 'Collapse' : 'Expand'}</span>
                {minimapOpen ? <ChevronUp size={15} /> : <ChevronDown size={15} />}
              </button>
              {minimapOpen && (
                <div className="equalizer-legend" aria-hidden="true">
                  <span>
                    <i className="eq-dot healthy" /> &gt;50%
                  </span>
                  <span>
                    <i className="eq-dot thinning" /> 20–50%
                  </span>
                  <span>
                    <i className="eq-dot critical" /> 1–2 left
                  </span>
                  <span>
                    <i className="eq-dot taken" /> Drafted
                  </span>
                </div>
              )}
            </div>
            {minimapOpen && (
              <div className="equalizer-grid" role="group" aria-label="Jump to point tier">
                {tiers.map(({ cost, availableCount, totalCount }, index) => {
                  const status = tierStatusClass(availableCount, totalCount)
                  return (
                    <button
                      type="button"
                      key={cost}
                      className={`equalizer-col ${status}`}
                      aria-label={`Jump to ${cost} point tier, ${availableCount} of ${totalCount} remaining`}
                      title={`${cost} pts: ${availableCount}/${totalCount} remaining — click to scroll`}
                      onClick={() => scrollToTier(cost, index)}
                    >
                      <span className="equalizer-stack" aria-hidden="true">
                        {Array.from({ length: totalCount }, (_, pipIndex) => (
                          <span
                            key={pipIndex}
                            className={`equalizer-pip ${pipIndex >= availableCount ? 'taken' : status}`}
                          />
                        ))}
                      </span>
                      <span className="equalizer-cost">{cost}</span>
                    </button>
                  )
                })}
              </div>
            )}
          </div>
          <div
            className="big-board-top-scroll"
            ref={topScrollRef}
            onScroll={handleTopScroll}
            aria-label="Horizontal board scroll"
          >
            <div
              className="big-board-top-scroll-spacer"
              style={{ width: `${effectiveTrackWidth}px` }}
            />
          </div>
          <div
            className="big-board-scroll"
            role="region"
            aria-label="Point tiers"
            ref={boardScrollRef}
            onScroll={handleBoardScroll}
          >
            {tiers.map(({ cost, items, availableCount, totalCount }) => {
              const overBudgetTier = remaining !== undefined && cost > remaining
              return (
                <div
                  className={`big-board-column ${availableCount === 0 ? 'depleted' : ''} ${overBudgetTier ? 'over-budget-tier' : ''}`}
                  key={cost}
                  aria-label={`${cost} point tier`}
                  ref={(node) => {
                    if (node) columnRefs.current.set(cost, node)
                    else columnRefs.current.delete(cost)
                  }}
                >
                  <header className="big-board-tier-header">
                    <div className="big-board-tier-title">
                      <strong>{cost}</strong>
                      <small>{cost === 1 ? 'pt' : 'pts'}</small>
                    </div>
                    <span className="big-board-tier-count">
                      {availableCount}/{totalCount} left
                    </span>
                  </header>
                  <div className="big-board-tier-track" aria-hidden="true">
                    <span
                      style={{
                        width: `${totalCount > 0 ? (availableCount / totalCount) * 100 : 0}%`,
                      }}
                    />
                  </div>
                  <div className="big-board-tier-list">
                    {items.map((item) => {
                      const taken = drafted.has(item.id)
                      const overBudget = remaining !== undefined && item.point_cost > remaining
                      const isSaved = saved.has(item.id)
                      return (
                        <button
                          key={item.id}
                          className={`big-board-item ${taken ? 'taken' : ''} ${isSaved ? 'saved' : ''}`}
                          onClick={() => onSelect(item)}
                          aria-label={`View ${item.name}, ${item.point_cost} points${taken ? ', already drafted' : overBudget ? ', over budget' : ''}`}
                        >
                          <span className="big-board-sprite">
                            <PokemonImage item={item} />
                          </span>
                          <span className="big-board-name">{item.name}</span>
                          {isSaved && !taken && (
                            <span className="big-board-fav" aria-hidden="true" title="In favorites">
                              <Bookmark size={12} fill="currentColor" />
                            </span>
                          )}
                          {taken && (
                            <span
                              className="big-board-taken-icon"
                              aria-hidden="true"
                              title="Drafted"
                            >
                              <Check size={12} />
                            </span>
                          )}
                        </button>
                      )
                    })}
                  </div>
                </div>
              )
            })}
          </div>
        </>
      ) : (
        <div className="empty-state">
          <Search size={28} />
          <h3>No Pokémon found</h3>
          <p>Change the search or clear the filters.</p>
          <button
            className="button clear-filters-button"
            onClick={() => {
              setSearch('')
              setFilters([])
              setAffordable(false)
            }}
          >
            Clear filters
          </button>
        </div>
      )}
    </section>
  )
}
