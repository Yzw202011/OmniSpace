// 本项目仅供学习使用，商业授权请+Q 3559331368
/* ==========================================================================
 * BrowserView.tsx —— 内置浏览器实时查看（TASK-036 / TASK-037）
 * --------------------------------------------------------------------------
 * - 截图轮询：每 2s GET /v1/browser/screenshot（展开时）
 * - 地址栏显示当前 URL（复用 browserStatus.current_url，不调 current-page；
 *   该端点内部含 3 个 ≥1s 节流操作，轮询它会导致操作队列积压、
 *   占满连接池，表现为所有按钮点击延迟过高）
 * - AI 当前操作描述 / AI 思考内容（复用 session status）
 * - [用户接管] / [交还AI] 按钮（POST /v1/browser/takeover|handback）
 * 后端未就绪时显示占位提示（fallback）。
 * ========================================================================== */

import React, { useEffect, useState } from 'react';
import { Globe, Bot, User, MousePointerClick, Flower2, MessageCircle, ArrowRight } from 'lucide-react';
import { useLearningStore } from '@/stores/useLearningStore';
import { getBrowserScreenshot, browserNavigate } from '@/services/learningApi';
import { isApiError } from '@/services/api';

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
  /** 后端不可用原因（如 Chromium 未安装；轮询成功即清除，服务恢复自动切回） */
  const [unavailableReason, setUnavailableReason] = useState<string>('');
  /** 地址栏输入值（受控；聚焦时不再被轮询覆盖，见 syncInput） */
  const [urlInput, setUrlInput] = useState<string>('');
  const [navigating, setNavigating] = useState<boolean>(false);
  const urlInputRef = React.useRef<HTMLInputElement | null>(null);
  const lastShownUrlRef = React.useRef<string>('');

  /** 轮询到的当前 URL 同步到输入框（用户聚焦输入时不打断编辑） */
  const syncUrlInput = (url: string) => {
    const focused = urlInputRef.current === document.activeElement;
    if (!focused && url !== lastShownUrlRef.current) {
      lastShownUrlRef.current = url;
      setUrlInput(url);
    }
  };

  /** 手动导航（回车/按钮提交；POST /browser/navigate，成功后立即刷新截图） */
  const handleNavigate = async () => {
    let url = urlInput.trim();
    if (!url || navigating) return;
    if (!/^https?:\/\//i.test(url)) {
      url = `https://${url}`;
    }
    setNavigating(true);
    try {
      await browserNavigate(url);
      // 导航已使后端截图缓存失效，立即拉取新截图（不等下个轮询周期）
      const shotRes = await getBrowserScreenshot();
      let src: string | null = null;
      if (typeof shotRes === 'string') {
        src = shotRes.startsWith('data:') ? shotRes : `data:image/png;base64,${shotRes}`;
      } else if (shotRes?.image_base64) {
        src = `data:${shotRes.mime || 'image/png'};base64,${shotRes.image_base64}`;
      } else if (shotRes?.data_url) {
        src = shotRes.data_url;
      } else if (shotRes?.image) {
        src = shotRes.image.startsWith('data:') ? shotRes.image : `data:image/png;base64,${shotRes.image}`;
      }
      if (src) {
        setShot(src);
        setAvailable(true);
        setUnavailableReason('');
      }
      // 刷新状态获取跳转后的真实 URL（可能被重定向），驱动地址栏显示
      await fetchBrowserStatus();
      const finalUrl = useLearningStore.getState().browserStatus?.current_url || url;
      lastShownUrlRef.current = finalUrl;
      setUrlInput(finalUrl);
      setCurrentUrl(finalUrl);
    } catch (err) {
      setUnavailableReason(
        isApiError(err) && err.message ? err.message : '导航失败，请检查地址后重试',
      );
    } finally {
      setNavigating(false);
    }
  };

  /* 展开时：每 2 秒轮询截图（后端 2.5s 结果缓存，隔次命中） */
  useEffect(() => {
    if (!isBrowserVisible) return;
    let cancelled = false;

    const poll = async () => {
      try {
        const shotRes = await getBrowserScreenshot();
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
        setAvailable(true);
        setUnavailableReason('');
      } catch (err) {
        // 透传后端真实原因（如"Chromium 浏览器未安装"），避免"未实现"误导文案
        if (!cancelled) {
          setAvailable(false);
          setUnavailableReason(
            isApiError(err) && err.message ? err.message : '截图服务暂不可用，请稍后重试',
          );
        }
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

  /* 当前 URL 由 browserStatus.current_url 驱动（fetchBrowserStatus 每 2s 刷新，
     /browser/status 不走节流队列，几毫秒即返回） */
  useEffect(() => {
    const url = browserStatus?.current_url || '';
    if (!url) return;
    setCurrentUrl(url);
    syncUrlInput(url);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [browserStatus?.current_url]);

  if (!isBrowserVisible) return null;

  // 控制权判定：后端字段 user_takeover（bool）优先；旧 controller 字段兜底
  const controller: 'ai' | 'user' =
    browserStatus?.user_takeover === true
      ? 'user'
      : browserStatus?.controller ?? 'ai';

  return (
    <section className="card hoverable" aria-label="内置浏览器">
      <div className="flex items-center justify-between mb-3">
        <h3 className="card-title" style={{ marginBottom: 0 }}><Globe size={16} aria-hidden="true" /> 内置浏览器</h3>
        <div className="flex items-center gap-2">
          <span className={`badge ${controller === 'ai' ? 'info' : 'warning'}`}>
            <span className="inline-flex items-center gap-1.5">
              {controller === 'ai' ? (
                <><Bot size={13} aria-hidden="true" /> AI 控制中</>
              ) : (
                <><User size={13} aria-hidden="true" /> 用户接管中</>
              )}
            </span>
          </span>
          {controller === 'ai' ? (
            <button className="btn btn-secondary btn-sm" onClick={takeover}><MousePointerClick size={14} aria-hidden="true" /> 用户接管</button>
          ) : (
            <button className="btn btn-primary btn-sm" onClick={handback}><Bot size={14} aria-hidden="true" /> 交还AI</button>
          )}
        </div>
      </div>

      {/* 地址栏（可输入导航：回车或点击箭头提交，POST /browser/navigate） */}
      <div className="flex items-center gap-2 mb-3">
        <input
          ref={urlInputRef}
          className="input mono flex-1"
          style={{ fontSize: 'var(--font-size-xs)' }}
          value={urlInput}
          onChange={(e) => setUrlInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') handleNavigate();
          }}
          placeholder={currentUrl || browserStatus?.current_url || '输入网址，回车导航（如 baidu.com）'}
          aria-label="浏览器地址栏"
          spellCheck={false}
        />
        <button
          className="btn btn-secondary btn-sm"
          onClick={handleNavigate}
          disabled={navigating || !urlInput.trim()}
          title="导航到该地址"
          aria-label="导航"
        >
          {navigating ? <div className="spinner" style={{ width: 14, height: 14 }} /> : <ArrowRight size={14} aria-hidden="true" />}
        </button>
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
            {available ? <div className="spinner" /> : <Flower2 size={28} aria-hidden="true" style={{ color: 'var(--color-primary)' }} />}
            <div className="text-secondary" style={{ fontSize: 'var(--font-size-sm)' }}>
              {available ? '截图加载中…' : unavailableReason || '截图服务暂不可用，请稍后重试'}
            </div>
          </div>
        )}
      </div>

      {/* AI 操作与思考 */}
      {(session?.current_action || session?.thinking) && (
        <div className="mt-3 flex flex-col gap-1">
          {session.current_action && (
            <div className="text-sm inline-flex items-center gap-1.5"><Bot size={14} aria-hidden="true" /> 当前操作：{session.current_action}</div>
          )}
          {session.thinking && (
            <div
              className="text-secondary inline-flex items-center gap-1.5"
              style={{ fontSize: 'var(--font-size-xs)', fontStyle: 'italic' }}
            >
              <MessageCircle size={13} aria-hidden="true" /> AI 思考：{session.thinking}
            </div>
          )}
        </div>
      )}
    </section>
  );
};

export default BrowserView;
