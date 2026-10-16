import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发时 /api 代理到本机 FastAPI（8015）；构建产物输出到 dist/，由 FastAPI 或任意静态服务托管。
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8015', changeOrigin: false },
      '/readyz': { target: 'http://127.0.0.1:8015', changeOrigin: false },
      '/healthz': { target: 'http://127.0.0.1:8015', changeOrigin: false },
    },
  },
  build: {
    outDir: 'dist',
    // 保留已打开网页仍在引用的旧版哈希资源。
    emptyOutDir: false,
    chunkSizeWarningLimit: 2000,
    rollupOptions: {
      output: {
        manualChunks: {
          'vendor-element': ['element-plus'],
          'vendor-g6': ['@antv/g6'],
        },
      },
    },
  },
})
