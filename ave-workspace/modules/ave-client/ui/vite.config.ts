import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  base: '/ui/',
  plugins: [react(), tailwindcss()],
  build: { outDir: '../static/ui', emptyOutDir: true },
})
