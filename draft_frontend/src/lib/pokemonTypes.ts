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
