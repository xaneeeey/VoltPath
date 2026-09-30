import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { copyFileSync, mkdirSync } from 'node:fs'
import { defineConfig, type Plugin } from 'vite'

// maplibre-gl v6 spawns its module worker from a URL computed at runtime, which bundlers can't
// see. Ship the worker + its shared chunk as static files and point setWorkerUrl() at them.
function maplibreWorker(): Plugin {
  return {
    name: 'maplibre-worker',
    buildStart() {
      mkdirSync('public/maplibre', { recursive: true })
      for (const f of ['maplibre-gl-worker.mjs', 'maplibre-gl-shared.mjs']) {
        copyFileSync(`node_modules/maplibre-gl/dist/${f}`, `public/maplibre/${f}`)
      }
    },
  }
}

export default defineConfig({
  plugins: [react(), tailwindcss(), maplibreWorker()],
  server: {
    port: Number(process.env.PORT) || 5174,
    proxy: {
      '/v1': { target: 'http://localhost:8000', ws: true, changeOrigin: true },
      '/.well-known': 'http://localhost:8000',
    },
  },
  build: { chunkSizeWarningLimit: 1500 },
})
