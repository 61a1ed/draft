import { readableTextColor, TYPE_COLORS } from '../lib/pokemonTypes'

export function TypePills({ types, small = false }: { types: string[]; small?: boolean }) {
  return (
    <span className={small ? 'type-pills small' : 'type-pills'}>
      {types.map((type) => {
        const color = TYPE_COLORS[type]
        return (
          <span
            className="type-pill"
            key={type}
            style={color ? { background: color, color: readableTextColor(color) } : undefined}
          >
            {type}
          </span>
        )
      })}
    </span>
  )
}

/** Point cost pinned to the bottom-right corner of a sprite. */
export function PointsRibbon({
  points,
  overBudget = false,
}: {
  points: number
  overBudget?: boolean
}) {
  return (
    <span className={overBudget ? 'points-ribbon over-budget' : 'points-ribbon'}>
      <strong>{points}</strong>
      <small>pts</small>
    </span>
  )
}
