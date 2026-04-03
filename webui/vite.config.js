import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    // Proxy API calls to FastAPI during dev
    proxy: {
      '/api': 'http://localhost:8000',
    },
  },
})