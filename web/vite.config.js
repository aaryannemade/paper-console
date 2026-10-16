import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    react(),
    tailwindcss()
  ],
  server: {
    // Forward every backend route the UI calls; print buttons use /action and /debug.
    proxy: Object.fromEntries(
      ['/api', '/action', '/debug'].map((prefix) => [
        prefix,
        { target: 'http://localhost:8000', changeOrigin: true },
      ]),
    ),
  },
})
