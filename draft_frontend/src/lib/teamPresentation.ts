import type { CSSProperties } from 'react'
import type { Team } from './types'

export function teamName(team: Pick<Team, 'id' | 'name'>): string {
  return team.name ?? `Team #${team.id}`
}

export function teamColor(rgb: number | null): string | null {
  return rgb === null ? null : `#${rgb.toString(16).padStart(6, '0')}`
}

/** Choose readable initials for any opaque sRGB team color, including black. */
export function teamAvatarStyle(rgb: number | null): CSSProperties | undefined {
  const backgroundColor = teamColor(rgb)
  if (rgb === null || backgroundColor === null) return undefined
  const linear = [rgb >> 16, (rgb >> 8) & 255, rgb & 255].map((channel) => {
    const value = channel / 255
    return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4
  })
  const luminance = 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
  return { backgroundColor, color: luminance > 0.179 ? '#000000' : '#ffffff' }
}
