import { defineConfig } from 'vite';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

// OmniSpace AI v2.1 Vite配置（规格 §2 技术栈锁定）
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const src = path.resolve(__dirname, 'src').replace(/\\/g, '/');

export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
  ],
  // 路径别名（与 tsconfig.json paths 同步）
  // 使用数组形式，长前缀优先匹配，避免 @ 覆盖 @types/@services 等
  resolve: {
    alias: [
      { find: '@components', replacement: `${src}/components` },
      { find: '@services', replacement: `${src}/services` },
      { find: '@stores', replacement: `${src}/stores` },
      { find: '@types', replacement: `${src}/types` },
      { find: '@three', replacement: `${src}/three` },
      { find: '@hooks', replacement: `${src}/hooks` },
      { find: '@utils', replacement: `${src}/utils` },
      { find: '@styles', replacement: `${src}/styles` },
      { find: '@', replacement: src },
    ],
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api/v1': {
        target: 'http://127.0.0.1:5800',
        changeOrigin: true,
      },
      '/v1': {
        target: 'http://127.0.0.1:5800',
        changeOrigin: true,
      },
      '/ws': {
        target: 'ws://127.0.0.1:5800',
        ws: true,
      },
      '/health': {
        target: 'http://127.0.0.1:5800',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    chunkSizeWarningLimit: 2000,
    rollupOptions: {
      output: {
        manualChunks: {
          'three-vendor': ['three'],
          'react-vendor': ['react', 'react-dom', 'react-router-dom'],
        },
      },
    },
  },
  optimizeDeps: {
    include: ['three'],
  },
});
