import { describe, expect, it } from 'vitest'
import { teamAvatarStyle, teamColor, teamName } from './teamPresentation'

describe('team presentation', () => {
  it.each([
    [0, '#000000'],
    [0xffffff, '#ffffff'],
    [0x00010f, '#00010f'],
    [null, null],
  ] as const)('renders RGB %s as %s without treating black as unfinished', (rgb, expected) => {
    expect(teamColor(rgb)).toBe(expected)
  })

  it('uses the saved team name or a stable fallback while setup is unfinished', () => {
    expect(teamName({ id: 7, name: 'Midnight' })).toBe('Midnight')
    expect(teamName({ id: 7, name: null })).toBe('Team #7')
  })

  it('keeps initials readable on black, white, and bright saturated colors', () => {
    expect(teamAvatarStyle(0)).toEqual({ backgroundColor: '#000000', color: '#ffffff' })
    expect(teamAvatarStyle(0xffffff)).toEqual({ backgroundColor: '#ffffff', color: '#000000' })
    expect(teamAvatarStyle(0x00ff00)).toEqual({ backgroundColor: '#00ff00', color: '#000000' })
    expect(teamAvatarStyle(null)).toBeUndefined()
  })
})
