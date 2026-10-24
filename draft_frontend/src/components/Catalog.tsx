import { useMemo, useState } from 'react'
import {
  ArrowDown,
  ArrowDownWideNarrow,
  ArrowUp,
  Bookmark,
  Check,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  SlidersHorizontal,
  Trash2,
  X,
} from 'lucide-react'
import { api } from '../lib/api'
import type { Draftable, LeagueSnapshot } from '../lib/types'
import type { FavoritesListSnapshot } from '../lib/favoritesTypes'
import type { FavoritesController } from '../lib/useFavorites'
import { Modal } from './Modal'
import { PointsRibbon, TypePills } from './PokemonParts'

export function PokemonImage({
  item,
  className = '',
}: {
  item: Pick<Draftable, 'name' | 'image_url'>
  className?: string
}) {
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

type ListDialog = { kind: 'create' } | { kind: 'rename' | 'delete'; list: FavoritesListSnapshot }

type SearchFilter = { kind: 'type' | 'ability' | 'move' | 'mega'; value: string; label: string }

const MEGA_FILTER: SearchFilter = { kind: 'mega', value: 'mega', label: 'Has Mega Evolution' }
const filterKey = (filter: SearchFilter) => `${filter.kind}:${filter.value}`

function matchesFilter(item: Draftable, filter: SearchFilter) {
  if (filter.kind === 'type') return !!item.types?.includes(filter.value)
  if (filter.kind === 'ability')
    return !!item.abilities?.includes(filter.value) || item.hidden_ability === filter.value
  if (filter.kind === 'move') return !!item.moves?.some((move) => move.name === filter.value)
  return item.megas.length > 0
}

export function Catalog({
  catalog,
  snapshot,
  favorites,
  legacyWatchlist = [],
  busy = false,
  onSelect,
}: {
  catalog: Draftable[]
  snapshot: LeagueSnapshot | null
  favorites: FavoritesController
  legacyWatchlist?: number[]
  busy?: boolean
  onSelect: (item: Draftable) => void
}) {
  const [search, setSearch] = useState('')
  const [filters, setFilters] = useState<SearchFilter[]>([])
  const [suggestOpen, setSuggestOpen] = useState(false)
  const [highlight, setHighlight] = useState(0)
  const [tab, setTab] = useState<'available' | 'all' | 'favorites' | 'legacy'>(
    snapshot ? 'available' : 'all',
  )
  const [sort, setSort] = useState('dex')
  const [affordable, setAffordable] = useState(false)
  const [dialog, setDialog] = useState<ListDialog | null>(null)
  const [name, setName] = useState('')
  const list = favorites.state
  const locked = busy || favorites.busy
  const cannotEdit = locked || favorites.loading || !!favorites.readError || !list
  const savedIds = useMemo(() => list?.entries.map((entry) => entry.draftable_id) ?? [], [list])
  const saved = useMemo(() => new Set(savedIds), [savedIds])
  const drafted = useMemo(
    () => new Set(snapshot?.rosters.map((pick) => pick.draftable_id) ?? []),
    [snapshot?.rosters],
  )
  const activeTeam = snapshot?.teams.find((team) => team.id === snapshot.state.next_team_id)
  const remaining = activeTeam?.points_remaining
  const allFilters = useMemo(() => {
    const types = new Set<string>()
    const abilities = new Set<string>()
    const moves = new Set<string>()
    let anyMega = false
    for (const item of catalog) {
      item.types?.forEach((type) => types.add(type))
      item.abilities?.forEach((ability) => abilities.add(ability))
      if (item.hidden_ability) abilities.add(item.hidden_ability)
      item.moves?.forEach((move) => moves.add(move.name))
      anyMega ||= item.megas.length > 0
    }
    const sorted = (values: Set<string>) => [...values].sort((a, b) => a.localeCompare(b))
    return [
      ...sorted(types).map((value): SearchFilter => ({
        kind: 'type',
        value,
        label: `${value} type`,
      })),
      ...sorted(abilities).map((value): SearchFilter => ({
        kind: 'ability',
        value,
        label: `${value} ability`,
      })),
      ...sorted(moves).map((value): SearchFilter => ({
        kind: 'move',
        value,
        label: `${value} move`,
      })),
      ...(anyMega ? [MEGA_FILTER] : []),
    ]
  }, [catalog])
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
        .filter((item) => {
          const query = search.trim().toLowerCase()
          return (
            (tab !== 'available' || !drafted.has(item.id)) &&
            (tab !== 'favorites' || saved.has(item.id)) &&
            (tab !== 'legacy' || legacyWatchlist.includes(item.id)) &&
            (!affordable || remaining === undefined || item.point_cost <= remaining) &&
            filters.every((filter) => matchesFilter(item, filter)) &&
            (!query ||
              item.name.toLowerCase().includes(query) ||
              String(item.dex ?? '').includes(query))
          )
        })
        .sort((a, b) =>
          sort === 'saved' && tab === 'favorites'
            ? savedIds.indexOf(a.id) - savedIds.indexOf(b.id)
            : sort === 'price-high'
              ? b.point_cost - a.point_cost || a.name.localeCompare(b.name)
              : sort === 'price-low'
                ? a.point_cost - b.point_cost || a.name.localeCompare(b.name)
                : sort === 'name'
                  ? a.name.localeCompare(b.name)
                  : (a.dex ?? a.id) - (b.dex ?? b.id) || a.id - b.id,
        ),
    [
      catalog,
      search,
      filters,
      tab,
      affordable,
      remaining,
      drafted,
      saved,
      savedIds,
      sort,
      legacyWatchlist,
    ],
  )
  const staleDialog =
    dialog &&
    dialog.kind !== 'create' &&
    (dialog.list.id !== list?.id || dialog.list.revision !== list?.revision)
  const openDialog = (next: ListDialog) => {
    favorites.clearError()
    setName(next.kind === 'rename' ? next.list.name : '')
    setDialog(next)
  }
  const toggleFavorite = (id: number) => {
    if (cannotEdit || !list) return
    const input = { p_list: list.id, p_revision: list.revision, p_draftable: id }
    void favorites.run(() =>
      saved.has(id) ? api.removeFavoritesEntry(input) : api.addFavoritesEntry(input),
    )
  }
  const move = (id: number, direction: -1 | 1) => {
    if (cannotEdit || !list) return
    const index = savedIds.indexOf(id)
    const target = index + direction
    if (index < 0 || target < 0 || target >= savedIds.length) return
    const order = [...savedIds]
    ;[order[index], order[target]] = [order[target], order[index]]
    void favorites.run(() =>
      api.setFavoritesOrder({
        p_list: list.id,
        p_revision: list.revision,
        p_draftables: order,
      }),
    )
  }

  return (
    <section className="catalog-section" aria-label="Pokémon catalog">
      <div className="section-heading">
        <h2>Pokémon</h2>
        <span className="count-pill">
          {catalog.filter((item) => !drafted.has(item.id)).length}{' '}
          {snapshot ? 'available' : 'Pokémon'}
        </span>
      </div>
      <div className="favorites-toolbar">
        <label className="favorites-select">
          <span>
            <span className="favorites-select-label">
              <Bookmark size={14} /> Favorites list
            </span>
            <span className="favorites-status" role="status" aria-atomic="true">
              {favorites.busy ? 'Saving favorites…' : favorites.loading ? 'Loading favorites…' : ''}
            </span>
          </span>
          <select
            aria-label="Favorites list"
            value={favorites.selectedId ?? ''}
            disabled={locked || !favorites.lists.length}
            onChange={(event) => {
              favorites.selectList(Number(event.target.value))
            }}
          >
            {!favorites.lists.length && (
              <option value="">
                {favorites.loading ? 'Loading lists…' : 'Create your first list'}
              </option>
            )}
            {favorites.lists.map((entry) => (
              <option key={entry.id} value={entry.id}>
                {entry.name}
              </option>
            ))}
          </select>
        </label>
        <div className="favorites-actions">
          <button
            className="button secondary"
            disabled={locked || favorites.loading || !!favorites.readError}
            onClick={() => openDialog({ kind: 'create' })}
          >
            <Plus size={14} /> New list
          </button>
          <button
            className="icon-button"
            aria-label="Rename favorites list"
            title="Rename list"
            disabled={cannotEdit}
            onClick={() => list && openDialog({ kind: 'rename', list })}
          >
            <Pencil size={16} />
          </button>
          <button
            className="icon-button"
            aria-label="Delete favorites list"
            title="Delete list"
            disabled={cannotEdit}
            onClick={() => list && openDialog({ kind: 'delete', list })}
          >
            <Trash2 size={16} />
          </button>
          <button
            className="icon-button"
            aria-label="Refresh favorites"
            title="Refresh favorites"
            disabled={locked || favorites.loading}
            onClick={() => void favorites.refresh()}
          >
            <RefreshCw size={16} />
          </button>
        </div>
        <p className="favorites-hint">
          Favorites are private to your account and available across leagues. Bookmark Pokémon into
          the selected list.
        </p>
      </div>
      {favorites.error && (
        <div className="inline-error" role="alert">
          {favorites.error}{' '}
          <button
            className="button text-button"
            disabled={locked || favorites.loading}
            onClick={() => void favorites.refresh()}
          >
            Retry favorites
          </button>
        </div>
      )}
      <div className="catalog-tabs" aria-label="Catalog filter">
        {(['available', 'all', 'favorites'] as const).map((value) => (
          <button
            key={value}
            aria-pressed={tab === value}
            className={tab === value ? 'selected' : ''}
            onClick={() => {
              setTab(value)
              setSort(value === 'favorites' ? 'saved' : 'dex')
            }}
          >
            {value === 'favorites' && <Bookmark size={14} />}
            {value === 'available' ? 'Available' : value === 'all' ? 'All Pokémon' : 'Favorites'}
            {value === 'favorites' && <span>{savedIds.length}</span>}
          </button>
        ))}
        {legacyWatchlist.length > 0 && (
          <button
            aria-pressed={tab === 'legacy'}
            className={tab === 'legacy' ? 'selected' : ''}
            onClick={() => {
              setTab('legacy')
              setSort('dex')
            }}
          >
            Browser watchlist <span>{legacyWatchlist.length}</span>
          </button>
        )}
      </div>
      {tab === 'legacy' && (
        <p className="favorites-hint legacy-watchlist-hint">
          Your earlier watchlist is still saved on this browser. Choose an account list above and
          bookmark Pokémon to copy them into it. Only you can view your account favorites.
        </p>
      )}
      <div className="catalog-controls">
        <div className="search-field">
          <Search size={18} />
          <input
            type="search"
            role="combobox"
            aria-label="Search Pokémon by name or Pokédex number"
            aria-autocomplete="list"
            aria-expanded={showSuggestions}
            aria-controls="catalog-search-suggestions"
            aria-activedescendant={showSuggestions ? `catalog-suggestion-${highlight}` : undefined}
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
            <button className="icon-button" aria-label="Clear search" onClick={() => setSearch('')}>
              <X size={15} />
            </button>
          )}
          {showSuggestions && (
            <ul
              className="search-suggestions"
              id="catalog-search-suggestions"
              role="listbox"
              aria-label="Search filters"
            >
              {suggestions.map((filter, index) => (
                <li
                  key={filterKey(filter)}
                  id={`catalog-suggestion-${index}`}
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
        <label className="sort-control">
          <ArrowDownWideNarrow size={16} />
          <select
            aria-label="Sort Pokémon"
            value={sort}
            onChange={(event) => {
              setSort(event.target.value)
            }}
          >
            {tab === 'favorites' && <option value="saved">Saved order</option>}
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
          }}
        >
          <SlidersHorizontal size={16} />
          <span>In budget</span>
        </button>
      </div>
      {filters.length > 0 && (
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
          <button className="button text-button" onClick={() => setFilters([])}>
            Clear filters
          </button>
        </div>
      )}
      <div className="pokemon-grid">
        {filtered.map((item) => {
          const taken = drafted.has(item.id)
          const overBudget = remaining !== undefined && item.point_cost > remaining
          const isSaved = saved.has(item.id)
          const position = savedIds.indexOf(item.id)
          return (
            <article className={`pokemon-card ${taken ? 'taken' : ''}`} key={item.id}>
              <button
                className={`pokemon-main ${item.megas.length ? '' : 'no-megas'}`}
                onClick={() => onSelect(item)}
                aria-label={`View ${item.name}, ${item.point_cost} points${taken ? ', already drafted' : overBudget ? ', over budget' : ''}`}
              >
                <div className={`pokemon-art color-${(item.dex ?? item.id) % 6}`}>
                  <PokemonImage item={item} />
                  <PointsRibbon points={item.point_cost} overBudget={overBudget && !taken} />
                </div>
                <h3>{item.species || item.name}</h3>
                <p className="pokemon-form">{item.form}</p>
                {item.types && <TypePills types={item.types} />}
                {item.megas.length > 0 && (
                  <div className="card-megas">
                    <span className="card-megas-label">Mega Evolutions</span>
                    {item.megas.map((mega) => (
                      <span className="card-mega" key={mega.id}>
                        <span>{mega.name}</span>
                        <TypePills types={mega.types} small />
                      </span>
                    ))}
                  </div>
                )}
                {taken && (
                  <span className="drafted-badge">
                    <Check size={13} />
                    Drafted
                  </span>
                )}
              </button>
              <button
                className={`bookmark-button card-bookmark ${isSaved ? 'saved' : ''}`}
                aria-label={`${isSaved ? 'Remove' : 'Add'} ${item.name} ${isSaved ? 'from' : 'to'} ${list?.name ?? 'favorites'}`}
                title={
                  list
                    ? `${isSaved ? 'Remove from' : 'Save to'} ${list.name}`
                    : 'Create a favorites list first'
                }
                aria-pressed={isSaved}
                disabled={cannotEdit}
                onClick={() => toggleFavorite(item.id)}
              >
                <Bookmark size={17} fill={isSaved ? 'currentColor' : 'none'} />
              </button>
              {tab === 'favorites' && sort === 'saved' && (
                <div className="favorite-order">
                  <span>Priority {position + 1}</span>
                  <button
                    className="icon-button"
                    aria-label={`Move ${item.name} earlier`}
                    disabled={cannotEdit || position === 0}
                    onClick={() => move(item.id, -1)}
                  >
                    <ArrowUp size={15} />
                  </button>
                  <button
                    className="icon-button"
                    aria-label={`Move ${item.name} later`}
                    disabled={cannotEdit || position === savedIds.length - 1}
                    onClick={() => move(item.id, 1)}
                  >
                    <ArrowDown size={15} />
                  </button>
                </div>
              )}
            </article>
          )
        })}
      </div>
      {filtered.length === 0 &&
        !(tab === 'favorites' && (favorites.loading || favorites.readError)) && (
          <div className="empty-state">
            {tab === 'favorites' ? <Bookmark size={28} /> : <Search size={28} />}
            <h3>
              {tab === 'favorites' && savedIds.length === 0
                ? list
                  ? 'This list is ready for your favorites'
                  : 'Plan your next picks'
                : 'No Pokémon found'}
            </h3>
            <p>
              {tab === 'favorites' && savedIds.length === 0
                ? list
                  ? 'Bookmark Pokémon from the catalog, then arrange them in your preferred order.'
                  : 'Create a favorites list to keep your picks together across leagues.'
                : 'Change the search or clear the filters.'}
            </p>
            <button
              className="button secondary"
              onClick={() => {
                if (tab === 'favorites' && !list) openDialog({ kind: 'create' })
                else {
                  setSearch('')
                  setFilters([])
                  setAffordable(false)
                  setTab('all')
                  setSort('dex')
                }
              }}
            >
              {tab === 'favorites' && !list ? 'Create favorites list' : 'Browse all Pokémon'}
            </button>
          </div>
        )}
      {filtered.length > 0 && (
        <div className="catalog-bottom">
          <span className="muted">Showing {filtered.length} Pokémon</span>
        </div>
      )}
      {dialog && (
        <Modal
          title={
            dialog.kind === 'create'
              ? 'Create favorites list'
              : dialog.kind === 'rename'
                ? 'Rename favorites list'
                : 'Delete favorites list?'
          }
          onClose={() => setDialog(null)}
        >
          {dialog.kind === 'delete' ? (
            <p className="modal-description">
              Delete “{dialog.list.name}” and its {dialog.list.entries.length} saved Pokémon? Your
              drafted teams will stay as they are.
            </p>
          ) : (
            <p className="modal-description">
              Keep a named list of Pokémon to use across your leagues.
            </p>
          )}
          {favorites.error && (
            <div className="inline-error" role="alert">
              {favorites.error}
            </div>
          )}
          {staleDialog && (
            <p className="selection-notice">
              This list changed. Close this dialog and review the latest list before continuing.
            </p>
          )}
          <form
            onSubmit={async (event) => {
              event.preventDefault()
              if (
                locked ||
                favorites.loading ||
                !!favorites.readError ||
                staleDialog ||
                (dialog.kind !== 'create' && cannotEdit)
              )
                return
              let success: boolean
              if (dialog.kind === 'create') {
                if (!name.trim()) return
                success = await favorites.create(name.trim())
              } else {
                const input = { p_list: dialog.list.id, p_revision: dialog.list.revision }
                if (dialog.kind === 'rename' && !name.trim()) return
                success = await favorites.run(() =>
                  dialog.kind === 'delete'
                    ? api.deleteFavoritesList(input)
                    : api.renameFavoritesList({ ...input, p_name: name.trim() }),
                )
              }
              if (success) setDialog((current) => (current === dialog ? null : current))
            }}
          >
            {dialog.kind !== 'delete' && (
              <label className="field">
                List name
                <input
                  aria-label="Favorites list name"
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  maxLength={120}
                  required
                  pattern=".*\S.*"
                  placeholder="e.g. First-round targets"
                  disabled={locked}
                />
              </label>
            )}
            <div className="modal-actions">
              <button type="button" className="button secondary" onClick={() => setDialog(null)}>
                Cancel
              </button>
              <button
                className={`button ${dialog.kind === 'delete' ? 'danger' : 'primary'}`}
                disabled={
                  locked ||
                  favorites.loading ||
                  !!favorites.readError ||
                  !!staleDialog ||
                  (dialog.kind !== 'create' && cannotEdit) ||
                  (dialog.kind !== 'delete' && !name.trim())
                }
              >
                {favorites.busy
                  ? 'Saving…'
                  : dialog.kind === 'create'
                    ? 'Create list'
                    : dialog.kind === 'rename'
                      ? 'Save name'
                      : 'Delete list'}
              </button>
            </div>
          </form>
        </Modal>
      )}
    </section>
  )
}
