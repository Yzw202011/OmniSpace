/* ==========================================================================
 * OmniSpace AI v2.3.1 —— Vitest 配置（TASK-P1-04 落地）
 * --------------------------------------------------------------------------
 * - 路径别名与 vite.config.ts / tsconfig.json 同步（长前缀优先，@ 兜底）
 * - 纯逻辑测试：node 环境即可（store/schema/constants 均不依赖真实 DOM）
 * - 用法：npm run test（= vitest run，CI 单次执行退出）
 * ========================================================================== */

import { defineConfig } from 'vitest/config';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const src = path.resolve(__dirname, 'src').replace(/\\/g, '/');

export default defineConfig({
  resolve: {
    alias: [
      { find: '@components', replacement: `${src}/components` },
      { find: '@services', replacement: `${src}/services` },
      { find: '@stores', replacement: `${src}/stores` },
      { find: '@constants', replacement: `${src}/constants` },
      { find: '@types', replacement: `${src}/types` },
      { find: '@three', replacement: `${src}/three` },
      { find: '@hooks', replacement: `${src}/hooks` },
      { find: '@utils', replacement: `${src}/utils` },
      { find: '@styles', replacement: `${src}/styles` },
      { find: '@', replacement: src },
    ],
  },
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
    // 逐文件串行：store 测试共享模块级轮询器与全局 zustand 状态，避免交叉污染
    fileParallelism: false,
  },
});
