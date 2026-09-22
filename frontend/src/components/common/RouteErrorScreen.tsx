// 本项目仅供学习使用，商业授权请+Q 3553191368
/* ==========================================================================
 * RouteErrorScreen.tsx —— 路由级错误屏（品牌化，2026-09-22）
 * --------------------------------------------------------------------------
 * 用户实弹：开发期前端产物重建后，在线旧页面引用旧指纹资源 → 懒加载
 * 404 → React Router 默认英文错误屏（丑+用户看不懂）。
 * 本屏接 router 根路由 errorElement：
 *   - 「动态模块加载失败」= 应用已更新 → 会话内首次自动刷新自愈
 *     （sessionStorage 防刷新循环；二次失败如实显示带手动刷新按钮）；
 *   - 其余错误：友好中文卡片 + 可折叠技术详情 + 刷新/回对话页出路。
 * ========================================================================== */

import { useEffect } from 'react';
import { useNavigate, useRouteError } from 'react-router-dom';
import { RefreshCw, Home, TriangleAlert } from 'lucide-react';

const CHUNK_FAIL_RE =
  /Failed to fetch dynamically imported module|Importing a module script failed|error loading dynamically imported module/i;
const CHUNK_RELOAD_FLAG = 'omnispace.chunk.reload';

export default function RouteErrorScreen() {
  const error = useRouteError();
  const navigate = useNavigate();
  const message =
    error instanceof Error ? error.message : String(error ?? '未知错误');
  const isChunkFail = CHUNK_FAIL_RE.test(message);

  useEffect(() => {
    if (!isChunkFail) return;
    try {
      if (!sessionStorage.getItem(CHUNK_RELOAD_FLAG)) {
        sessionStorage.setItem(CHUNK_RELOAD_FLAG, '1');
        window.location.reload();
      }
    } catch {
      /* silent-intent: 会话存储不可用则走手动刷新 */
    }
  }, [isChunkFail]);

  return (
    <div className="page" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', minHeight: '60vh' }}>
      <div className="card" style={{ maxWidth: 520, padding: 'var(--space-5)', display: 'flex', flexDirection: 'column', gap: 'var(--space-3)' }}>
        <div className="flex items-center gap-2">
          <TriangleAlert size={18} style={{ color: 'var(--color-warning)' }} aria-hidden="true" />
          <strong style={{ fontSize: 'var(--font-size-base)' }}>
            {isChunkFail ? '应用已更新，正在自动恢复…' : '页面出了点问题'}
          </strong>
        </div>
        <p className="text-secondary text-sm">
          {isChunkFail
            ? '检测到新版本前端资源（旧页面引用的文件已被替换）。首次会自动刷新恢复；若本提示反复出现，请手动刷新一次。'
            : '当前页面加载遇到异常。可以刷新重试，或先回到对话页继续使用；问题持续可在「系统日志」查看详情。'}
        </p>
        <details className="text-xs text-tertiary">
          <summary style={{ cursor: 'pointer' }}>技术详情</summary>
          <pre style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-all', margin: 'var(--space-2) 0 0' }}>
            {message}
          </pre>
        </details>
        <div className="flex justify-end gap-2">
          <button type="button" className="btn" onClick={() => navigate('/chat')}>
            <Home size={14} /> 回到对话
          </button>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => {
              try {
                sessionStorage.removeItem(CHUNK_RELOAD_FLAG);
              } catch {
                /* silent-intent: 会话存储不可用不影响手动刷新 */
              }
              window.location.reload();
            }}
          >
            <RefreshCw size={14} /> 刷新页面
          </button>
        </div>
      </div>
    </div>
  );
}
