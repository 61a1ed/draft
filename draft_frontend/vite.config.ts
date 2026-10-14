import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const target = env.API_TARGET || 'http://127.0.0.1:8000'
  // Keep the browser on one origin. The backend validates Origin against
  // PUBLIC_BASE_URL, which must be the frontend URL during development.
  const proxy = Object.fromEntries(
    ['/api', '/auth', '/images', '/healthz'].map((path) => [path, { target, changeOrigin: true }]),
  )
  return {
    plugins: [react()],
    server: { port: 5173, strictPort: true, proxy },
    preview: { port: 4173, proxy },
  }
})
