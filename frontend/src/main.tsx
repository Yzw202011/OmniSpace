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

// 前端异常采集（2026-09-01 日志机制方案 C）：window.onerror 与
// unhandledrejection 进入系统日志事件库（/logs/frontend-event），
// 排障时前端崩溃不再无声消失。节流：同消息 10s 一条，防风暴。
void import('./services/logApi').then(({ postFrontendEvent }) => {
  const lastSent = new Map<string, number>();
  const report = (kind: 'error' | 'warning', message: string, stack: string) => {
    const key = message.slice(0, 80);
    const now = Date.now();
    if (now - (lastSent.get(key) ?? 0) < 10_000) return;
    lastSent.set(key, now);
    void postFrontendEvent(kind, message, stack);
  };
  window.addEventListener('error', (e) => {
    report('error', e.message || '未知脚本错误', e.error?.stack ?? '');
  });
  window.addEventListener('unhandledrejection', (e) => {
    const reason = e.reason;
    report(
      'error',
      typeof reason === 'string' ? reason : reason?.message || '未处理的 Promise 拒绝',
      reason?.stack ?? '',
    );
  });
});
// 本项目仅供学习使用，商业授权请+Q 3559331368
