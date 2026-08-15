/* ==========================================================================
 * BrowserView.tsx —— 内置浏览器实时查看（TASK-036 / TASK-037）
 * --------------------------------------------------------------------------
 * - 截图轮询：每 2s GET /v1/browser/screenshot（展开时）
 * - 地址栏显示当前 URL（GET /v1/browser/current-page）
 * - AI 当前操作描述 / AI 思考内容（复用 session status）
 * - [用户接管] / [交还AI] 按钮（POST /v1/browser/takeover|handback）
 * 后端未就绪时显示占位提示（fallback）。
 * ========================================================================== */

import React, { useEffect, useState } from 'react';
import { useLearningStore } from '@/stores/useLearningStore';
import { getBrowserScreenshot, getBrowserCurrentPage } from '@/services/learningApi';

export const BrowserView: React.FC = () => {
  const isBrowserVisible = useLearningStore((s) => s.isBrowserVisible);
  const browserStatus = useLearningStore((s) => s.browserStatus);
  const fetchBrowserStatus = useLearningStore((s) => s.fetchBrowserStatus);
  const session = useLearningStore((s) => s.session);
  const takeover = useLearningStore((s) => s.takeover);
  const handback = useLearningStore((s) => s.handback);

  const [shot, setShot] = useState<string | null>(null);
  const [currentUrl, setCurrentUrl] = useState<string>('');
  const [available, setAvailable] = useState<boolean>(true);

  /* 展开时：每 2 秒轮询截图与当前页 */
  useEffect(() => {
    if (!isBrowserVisible) return;
    let cancelled = false;

    const poll = async () => {
      try {
        const [shotRes, pageRes] = await Promise.all([
          getBrowserScreenshot(),
          getBrowserCurrentPage(),
        ]);
        if (cancelled) return;
        // 截图响应兼容：string（base64 或 dataURL）/ {image} / {data_url} / {image_base64, mime}
        let src: string | null = null;
        if (typeof shotRes === 'string') {
          src = shotRes.startsWith('data:') ? shotRes : `data:image/png;base64,${shotRes}`;
        } else if (shotRes?.data_url) {
          src = shotRes.data_url;
        } else if (shotRes?.image_base64) {
          src = `data:${shotRes.mime || 'image/png'};base64,${shotRes.image_base64}`;
        } else if (shotRes?.image) {
          src = shotRes.image.startsWith('data:')
            ? shotRes.image
            : `data:image/png;base64,${shotRes.image}`;
        }
        setShot(src);
        setCurrentUrl(pageRes?.url || '');
        setAvailable(true);
      } catch {
        if (!cancelled) setAvailable(false);
      }
      fetchBrowserStatus();
    };

    poll();
    const timer = setInterval(poll, 2000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [isBrowserVisible, fetchBrowserStatus]);

  if (!isBrowserVisible) return null;

  const controller = browserStatus?.controller ?? 'ai';

  return (
    <section className="card hoverable" aria-label="内置浏览器">
      <div className="flex items-center justify-between mb-3">
        <h3 className="card-title" style={{ marginBottom: 0 }}>🌐 内置浏览器</h3>
        <div className="flex items-center gap-2">
          <span className={`badge ${controller === 'ai' ? 'info' : 'warning'}`}>
            {controller === 'ai' ? '🤖 AI 控制中' : '🧑 用户接管中'}
          </span>
          {controller === 'ai' ? (
            <button className="btn btn-secondary btn-sm" onClick={takeover}>🖱 用户接管</button>
          ) : (
            <button className="btn btn-primary btn-sm" onClick={handback}>🤖 交还AI</button>
          )}
        </div>
      </div>

      {/* 地址栏 */}
      <div className="input flex items-center mb-3" style={{ cursor: 'default' }}>
        <span className="text-tertiary mono" style={{ fontSize: 'var(--font-size-xs)' }}>
          {currentUrl || browserStatus?.current_url || 'about:blank'}
        </span>
      </div>

      {/* 截图区域 */}
      <div
        className="flex-center"
        style={{
          aspectRatio: '16 / 9',
          borderRadius: 'var(--radius-lg)',
          border: '1px solid var(--color-border-light)',
          background: 'var(--color-surface-secondary)',
          overflow: 'hidden',
        }}
      >
        {shot ? (
          <img
            src={shot}
            alt="AI 浏览器实时截图"
            style={{ width: '100%', height: '100%', objectFit: 'contain' }}
          />
        ) : (
          <div className="loading-block">
            {available ? <div className="spinner" /> : <span style={{ fontSize: 28 }}>🌸</span>}
            <div className="text-secondary" style={{ fontSize: 'var(--font-size-sm)' }}>
              {available ? '截图加载中…' : '浏览器截图服务未就绪（后端 /v1/browser/screenshot 未实现）'}
            </div>
          </div>
        )}
      </div>

      {/* AI 操作与思考 */}
      {(session?.current_action || session?.thinking) && (
        <div className="mt-3 flex flex-col gap-1">
          {session.current_action && (
            <div className="text-sm">🤖 当前操作：{session.current_action}</div>
          )}
          {session.thinking && (
            <div
              className="text-secondary"
              style={{ fontSize: 'var(--font-size-xs)', fontStyle: 'italic' }}
            >
              💭 AI 思考：{session.thinking}
            </div>
          )}
        </div>
      )}
    </section>
  );
};

export default BrowserView;
