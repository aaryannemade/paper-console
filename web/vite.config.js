import process from 'node:process'
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
    // `just dev-* --bind ADDR` sets PC1_BACKEND_URL when the backend is not on localhost.
    proxy: Object.fromEntries(
      ['/api', '/action', '/debug'].map((prefix) => [
        prefix,
        { target: process.env.PC1_BACKEND_URL || 'http://localhost:8000', changeOrigin: true },
      ]),
    ),
  },
})
