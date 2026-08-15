/* ==========================================================================
 * OmniSpace AI v2.1 —— 应用入口（规格 §6.4 启动时序）
 * --------------------------------------------------------------------------
 * - React 19 createRoot 渲染 <App />
 * - 导入全局样式 sakura.css（Tailwind 4 + Sakura 主题，COM-009 唯一主题）
 * - 移除 index.html 中的 boot-screen 启动屏（React 挂载后）
 * ========================================================================== */

import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import './styles/sakura.css';

const rootEl = document.getElementById('root');
if (!rootEl) {
  throw new Error('根节点 #root 未找到，请检查 index.html');
}

// React 19 createRoot 渲染
createRoot(rootEl).render(
  <StrictMode>
    <App />
  </StrictMode>,
);

// 移除启动屏（index.html 中的 #boot-screen）
const bootScreen = document.getElementById('boot-screen');
if (bootScreen) {
  bootScreen.classList.add('hidden');
  setTimeout(() => {
    bootScreen.remove();
  }, 320);
}
