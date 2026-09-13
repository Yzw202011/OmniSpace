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
    dedupe: ['react', 'react-dom', 'react-router-dom', 'lucide-react'],
    alias: [
      { find: '@components', replacement: `${src}/components` },
      { find: '@services', replacement: `${src}/services` },
      { find: '@stores', replacement: `${src}/stores` },
      { find: '@types', replacement: `${src}/types` },
      // G-B6（W1 2026-09-13）：@hooks 别名删除——src/hooks 目录不存在，
      // 空别名属迁移残留
      { find: '@utils', replacement: `${src}/utils` },
      { find: '@styles', replacement: `${src}/styles` },
      { find: '@', replacement: src },
    ],
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      // ws:true —— 硬件实时 WS（/api/v1/hardware/realtime）与对话流
      // （/api/v1/dialog/stream/*）经此代理升级，缺失会导致状态栏遥测恒为 --
      '/api/v1': {
        target: 'http://127.0.0.1:5800',
        changeOrigin: true,
        ws: true,
      },
      // G-B7（W1 2026-09-13）：/v1 代理删除——ADR-03 已迁 /api/v1，
      // src 中无任何 /v1 业务调用，属迁移残留
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
        // 函数式 chunk 分组：把所有 node_modules 依赖聚合进少数 vendor，
        // 避免 zustand/lucide 等散入各懒加载页面 chunk、破坏浏览器缓存复用。
        // react-vendor 仅收纳 react 核心族；react-markdown 及其 unified/remark
        // 生态整体留在 vendor，否则 chunk 相互引用会形成循环依赖警告。
        manualChunks(id: string) {
          if (!id.includes('node_modules')) return undefined;
          if (
            /node_modules[\\/](react|react-dom|react-router|react-router-dom|react-is|scheduler|use-sync-external-store|zustand)[\\/]/.test(id)
          ) {
            return 'react-vendor';
          }
          return 'vendor';
        },
      },
    },
  },
  optimizeDeps: {
    // react/react-dom 必须保持 externalized——强制预打包会与
    // @vitejs/plugin-react 的运行时形成双 React 实例，导致
    // "Cannot read properties of null (reading 'useState/useEffect')" 崩溃
    esbuildOptions: {
      jsx: 'automatic',
      target: 'es2022',
    },
  },
});
