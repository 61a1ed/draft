export interface FavoritesList {
  id: number
  owner_user_id: number
  name: string
  revision: number
}

export interface FavoritesEntry {
  draftable_id: number
  /** Positions are ordered but may contain gaps after removing an entry. */
  ordinal: number
}

export interface FavoritesListSnapshot extends FavoritesList {
  entries: FavoritesEntry[]
}

export interface CreateFavoritesListInput {
  p_name: string
}

/** Favorites use their own revision, independent of any league or draft. */
export interface FavoritesListRevision {
  p_list: number
  p_revision: number
}

export interface RenameFavoritesListInput extends FavoritesListRevision {
  p_name: string
}

export interface FavoritesEntryInput extends FavoritesListRevision {
  p_draftable: number
}

export interface SetFavoritesOrderInput extends FavoritesListRevision {
  /** The complete list membership in its new order, including filtered entries. */
  p_draftables: number[]
}
