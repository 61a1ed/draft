import type { BaseStats, Draftable } from './types'

/** Pill colors for the 18 Pokémon types. Unknown types fall back to a neutral pill. */
export const TYPE_COLORS: Record<string, string> = {
  Normal: '#A8A878',
  Fighting: '#C03028',
  Flying: '#A890F0',
  Poison: '#A040A0',
  Ground: '#E0C068',
  Rock: '#B8A038',
  Bug: '#A8B820',
  Ghost: '#705898',
  Steel: '#B8B8D0',
  Fire: '#F08030',
  Water: '#6890F0',
  Grass: '#78C850',
  Electric: '#F8D030',
  Psychic: '#F85888',
  Ice: '#98D8D8',
  Dragon: '#7038F8',
  Dark: '#705848',
  Fairy: '#EE99AC',
}

export const DARK_TEXT = '#1d1d1d'
export const LIGHT_TEXT = '#ffffff'

/** WCAG relative luminance of a #RRGGBB color. */
function luminance(hex: string) {
  const [r, g, b] = [1, 3, 5].map((index) => {
    const channel = parseInt(hex.slice(index, index + 2), 16) / 255
    return channel <= 0.03928 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4
  })
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

/** Dark or white text, whichever contrasts more with the background. */
export function readableTextColor(background: string) {
  const light = luminance(background)
  const againstDark = (light + 0.05) / (luminance(DARK_TEXT) + 0.05)
  const againstWhite = 1.05 / (light + 0.05)
  return againstDark >= againstWhite ? DARK_TEXT : LIGHT_TEXT
}

export interface BundledFormVariant {
  name: string
  types: string[]
  abilities: string[]
  hidden_ability: string | null
  base_stats: BaseStats
}

const GOURGEIST_FORMS: BundledFormVariant[] = [
  {
    name: 'Small',
    types: ['Ghost', 'Grass'],
    abilities: ['Pickup', 'Frisk'],
    hidden_ability: 'Insomnia',
    base_stats: {
      hp: 55,
      attack: 85,
      defense: 122,
      special_attack: 58,
      special_defense: 75,
      speed: 99,
    },
  },
  {
    name: 'Average',
    types: ['Ghost', 'Grass'],
    abilities: ['Pickup', 'Frisk'],
    hidden_ability: 'Insomnia',
    base_stats: {
      hp: 65,
      attack: 90,
      defense: 122,
      special_attack: 58,
      special_defense: 75,
      speed: 84,
    },
  },
  {
    name: 'Large',
    types: ['Ghost', 'Grass'],
    abilities: ['Pickup', 'Frisk'],
    hidden_ability: 'Insomnia',
    base_stats: {
      hp: 75,
      attack: 95,
      defense: 122,
      special_attack: 58,
      special_defense: 75,
      speed: 69,
    },
  },
  {
    name: 'Jumbo',
    types: ['Ghost', 'Grass'],
    abilities: ['Pickup', 'Frisk'],
    hidden_ability: 'Insomnia',
    base_stats: {
      hp: 85,
      attack: 100,
      defense: 122,
      special_attack: 58,
      special_defense: 75,
      speed: 54,
    },
  },
]

const SQUAWKABILLY_FORMS: BundledFormVariant[] = [
  {
    name: 'Green',
    types: ['Normal', 'Flying'],
    abilities: ['Intimidate', 'Hustle'],
    hidden_ability: 'Guts',
    base_stats: {
      hp: 82,
      attack: 96,
      defense: 51,
      special_attack: 45,
      special_defense: 51,
      speed: 92,
    },
  },
  {
    name: 'Blue',
    types: ['Normal', 'Flying'],
    abilities: ['Intimidate', 'Hustle'],
    hidden_ability: 'Guts',
    base_stats: {
      hp: 82,
      attack: 96,
      defense: 51,
      special_attack: 45,
      special_defense: 51,
      speed: 92,
    },
  },
  {
    name: 'Yellow',
    types: ['Normal', 'Flying'],
    abilities: ['Intimidate', 'Hustle'],
    hidden_ability: 'Sheer Force',
    base_stats: {
      hp: 82,
      attack: 96,
      defense: 51,
      special_attack: 45,
      special_defense: 51,
      speed: 92,
    },
  },
  {
    name: 'White',
    types: ['Normal', 'Flying'],
    abilities: ['Intimidate', 'Hustle'],
    hidden_ability: 'Sheer Force',
    base_stats: {
      hp: 82,
      attack: 96,
      defense: 51,
      special_attack: 45,
      special_defense: 51,
      speed: 92,
    },
  },
]

export function getBundledForms(
  item: Pick<Draftable, 'dex' | 'species' | 'name'>,
): BundledFormVariant[] | null {
  const species = (item.species || item.name).trim().toLowerCase()
  if (item.dex === 711 || species === 'gourgeist' || species.startsWith('gourgeist (')) {
    return GOURGEIST_FORMS
  }
  if (item.dex === 931 || species === 'squawkabilly' || species.startsWith('squawkabilly (')) {
    return SQUAWKABILLY_FORMS
  }
  return null
}
