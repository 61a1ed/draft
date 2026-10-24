import { useState } from 'react'
import { Moon, Sun } from 'lucide-react'

type Theme = 'light' | 'dark'

const hasDocument = typeof document !== 'undefined'

function applyTheme(theme: Theme) {
  if (!hasDocument) return
  document.documentElement.dataset.theme = theme
  document
    .querySelector('meta[name="theme-color"]')
    ?.setAttribute('content', theme === 'dark' ? '#0f0e14' : '#f6f7fb')
  try {
    localStorage.setItem('draft-club:theme', theme)
  } catch {}
}

export function ThemeToggle({ className = '' }: { className?: string }) {
  const [theme, setTheme] = useState<Theme>(() =>
    hasDocument && document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light',
  )
  const next = theme === 'dark' ? 'light' : 'dark'
  return (
    <button
      className={`icon-button theme-toggle ${className}`}
      aria-label={`Switch to ${next} mode`}
      title={`Switch to ${next} mode`}
      onClick={() => {
        applyTheme(next)
        setTheme(next)
      }}
    >
      {theme === 'dark' ? <Moon size={16} /> : <Sun size={16} />}
    </button>
  )
}
