import type { Draftable } from './types'

export type SearchFilter = {
  kind: 'type' | 'ability' | 'move' | 'mega'
  value: string
  label: string
}

export type CatalogFilterState = {
  search: string
  filters: SearchFilter[]
  affordable: boolean
}

export const INITIAL_CATALOG_FILTERS: CatalogFilterState = {
  search: '',
  filters: [],
  affordable: false,
}

export const MEGA_FILTER: SearchFilter = { kind: 'mega', value: 'mega', label: 'Has Mega Evolution' }
export const filterKey = (filter: SearchFilter) => `${filter.kind}:${filter.value}`

export function matchesFilter(item: Draftable, filter: SearchFilter) {
  if (filter.kind === 'type') return !!item.types?.includes(filter.value)
  if (filter.kind === 'ability')
    return !!item.abilities?.includes(filter.value) || item.hidden_ability === filter.value
  if (filter.kind === 'move') return !!item.moves?.some((move) => move.name === filter.value)
  return item.megas.length > 0
}

export function buildAllFilters(catalog: Draftable[]): SearchFilter[] {
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
}

export function matchesCatalogQuery(item: Draftable, query: string) {
  const trimmed = query.trim().toLowerCase()
  if (!trimmed) return true
  return (
    item.name.toLowerCase().includes(trimmed) || String(item.dex ?? '').includes(trimmed)
  )
}
