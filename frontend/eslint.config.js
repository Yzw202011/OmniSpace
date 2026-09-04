// OmniSpace AI 前端代码规范（ESLint 9 flat config，TASK-P1-03 激活）
// 用法：npm run lint（= eslint src/）
// 说明：typescript-eslint 仅启用解析器（TS 语法）+ 少量通用规则基线；
// 语义类检查交给 tsc --noEmit（build 脚本已含）。
import js from '@eslint/js';
import tseslint from 'typescript-eslint';
import reactHooks from 'eslint-plugin-react-hooks';

export default tseslint.config(
  js.configs.recommended,
  {
    files: ['src/**/*.{ts,tsx}'],
    plugins: {
      'react-hooks': reactHooks,
      '@typescript-eslint': tseslint.plugin,
    },
    languageOptions: {
      parser: tseslint.parser,
      ecmaVersion: 2022,
      sourceType: 'module',
      parserOptions: {
        ecmaFeatures: { jsx: true },
      },
      globals: {
        window: 'readonly',
        document: 'readonly',
        console: 'readonly',
        fetch: 'readonly',
        WebSocket: 'readonly',
        URL: 'readonly',
        URLSearchParams: 'readonly',
        setTimeout: 'readonly',
        setInterval: 'readonly',
        clearTimeout: 'readonly',
        clearInterval: 'readonly',
        requestAnimationFrame: 'readonly',
        cancelAnimationFrame: 'readonly',
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
      'no-redeclare': 'off',   // TS 函数重载语法合法，真实重复声明由 tsc 拦截
      'no-console': 'warn',
      'no-debugger': 'error',
      '@typescript-eslint/no-explicit-any': 'error', // 规范 F-002（2026-09-04 合规批0落地；存量为0，零噪音防新增）
      eqeqeq: ['warn', 'smart'],
      'prefer-const': 'warn',
      'no-var': 'error',
      // react-hooks：只启用两条经典规则（v7 编译器系规则对存量代码误报多，暂不引入）
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'warn',
    },
  },
  {
    ignores: ['dist/', 'node_modules/', '*.config.ts', 'vite.config.*'],
  },
);
