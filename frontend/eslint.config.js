// OmniSpace AI 前端代码规范（ESLint 9 flat config）
// 启用方式：cd frontend && npm install && npx eslint src/
// （当前环境无 node 运行时，此配置为接入即用基线）
import js from '@eslint/js';

export default [
  js.configs.recommended,
  {
    files: ['src/**/*.{ts,tsx}'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: {
        window: 'readonly',
        document: 'readonly',
        console: 'readonly',
        fetch: 'readonly',
        WebSocket: 'readonly',
        URL: 'readonly',
        setTimeout: 'readonly',
        setInterval: 'readonly',
        clearTimeout: 'readonly',
        clearInterval: 'readonly',
        requestAnimationFrame: 'readonly',
        localStorage: 'readonly',
        navigator: 'readonly',
        history: 'readonly',
        location: 'readonly',
        HTMLElement: 'readonly',
        HTMLCanvasElement: 'readonly',
        HTMLImageElement: 'readonly',
        File: 'readonly',
        Blob: 'readonly',
        FormData: 'readonly',
        AbortController: 'readonly',
      },
    },
    rules: {
      // React 19 + TS 项目常用基线
      'no-unused-vars': 'off', // 交给 TS 编译器（tsc --noEmit 已在 build 中）
      'no-undef': 'off',       // 同上，TS 类型系统覆盖
      'no-console': 'warn',
      'no-debugger': 'error',
      eqeqeq: ['warn', 'smart'],
      'prefer-const': 'warn',
    },
  },
  {
    ignores: ['dist/', 'node_modules/', '*.config.ts', 'vite.config.*'],
  },
];
