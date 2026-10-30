import { fileURLToPath, URL } from 'node:url'
import vue from '@vitejs/plugin-vue'
import { loadEnv } from 'vite'
import { defineConfig } from 'vitest/config'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const backend = process.env.VITE_API_TARGET || env.VITE_API_TARGET || 'http://127.0.0.1:8000'
  const proxy = {
    target: backend,
    changeOrigin: true,
    secure: false,
    ...(process.env.DEV_WECOM_USERID || env.DEV_WECOM_USERID
      ? { headers: { 'X-WeCom-UserId': process.env.DEV_WECOM_USERID || env.DEV_WECOM_USERID } }
      : {}),
  }

  return {
    base: process.env.VITE_BASE_PATH || env.VITE_BASE_PATH || '/',
    plugins: [vue()],
    resolve: {
      alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
    },
    server: {
      host: '0.0.0.0',
      port: 8080,
      strictPort: true,
      watch: { usePolling: (process.env.VITE_FILE_WATCH_POLLING || env.VITE_FILE_WATCH_POLLING) === 'true' },
      proxy: {
        '/api': proxy,
        '/auth': proxy,
        '/healthz': proxy,
        '^/issues/[^/]+/media/[^/]+$': proxy,
        '^/issues/drafts/[^/]+/media/[^/]+$': proxy,
        '^/issues/private-intakes/media/[^/]+$': proxy,
      },
    },
    build: { sourcemap: false },
    test: {
      environment: 'jsdom',
      setupFiles: ['./src/test/setup.ts'],
      clearMocks: true,
    },
  }
})
