import { useCallback, useEffect, useRef, useState } from 'react';
import { get, post } from '../../services/api';
import type { ApiError } from '../../types';

/**
 * 产品内激活遮罩（P8 补洞：激活不止在启动页）。
 *
 * 大白话：只要任何一个接口报「产品尚未激活」，整个界面立刻蒙上一层
 * 激活窗（指纹+激活码），激活成功才放行——堵住「绕过启动页直接进界面」
 * 的路。事件由 services/api.ts 在统一错误出口派发。
 */

interface LicenseStatus {
  gate_enabled: boolean;
  activated: boolean;
  reason: string;
  fingerprints: string[];
}

const EV_LICENSE_REQUIRED = 'omnispace:license-required';

export default function LicenseGate() {
  const [open, setOpen] = useState(false);
  const [status, setStatus] = useState<LicenseStatus | null>(null);
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [copied, setCopied] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const refreshStatus = useCallback(async () => {
    try {
      const s = await get<LicenseStatus>('/license/status');
      setStatus(s);
      if (!s.activated) setOpen(true);
      else setOpen(false);
    } catch {
      /* 后端未就绪时保持现状 */
    }
  }, []);

  useEffect(() => {
    const onRequired = () => {
      setOpen(true);
      void refreshStatus();
    };
    window.addEventListener(EV_LICENSE_REQUIRED, onRequired);
    // 挂载即自检一次：界面若在事件之前就被打开，这里补上网
    void refreshStatus();
    // 后端就绪较慢时周期补检（拿到指纹供复制）
    pollRef.current = setInterval(() => {
      if (open) void refreshStatus();
    }, 5000);
    return () => {
      window.removeEventListener(EV_LICENSE_REQUIRED, onRequired);
      if (pollRef.current) clearInterval(pollRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const copyFp = async () => {
    const text = (status?.fingerprints ?? []).join('\n');
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 1600);
  };

  const activate = async () => {
    const trimmed = code.trim();
    if (!trimmed) {
      setMsg({ ok: false, text: '请先粘贴激活码' });
      return;
    }
    setBusy(true);
    setMsg(null);
    try {
      await post('/license/activate', { code: trimmed });
      setMsg({ ok: true, text: '✓ 激活成功，正在进入…' });
      setTimeout(() => window.location.reload(), 900);
    } catch (err) {
      const e = err as ApiError;
      setMsg({
        ok: false,
        text: '✕ ' + (e?.message || '激活失败')
          + (e?.suggestion ? `（${e.suggestion}）` : ''),
      });
    } finally {
      setBusy(false);
    }
  };

  if (!open) return null;

  const fpText = (status?.fingerprints ?? []).join('\n') || '（等待后端就绪后显示…）';

  return (
    <div style={{
      position: 'fixed', inset: 0, zIndex: 9999,
      background: 'rgba(6,9,14,.92)', backdropFilter: 'blur(6px)',
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      fontFamily: 'inherit', color: '#d7dde6',
    }}>
      <div style={{
        width: 'min(560px, 92vw)', background: '#161b22',
        border: '1px solid #2a313c', borderRadius: '12px',
        padding: '28px 30px', boxShadow: '0 24px 80px rgba(0,0,0,.55)',
      }}>
        <div style={{ fontSize: 18, fontWeight: 600, marginBottom: 6 }}>
          🔑 产品激活
        </div>
        <div style={{ fontSize: 13, color: '#8a93a3', marginBottom: 18 }}>
          把下面 3 行「机器指纹」发给卖家换取激活码，粘贴后完成激活（一次性操作）。
        </div>

        <div style={{ fontSize: 12, color: '#8a93a3', margin: '10px 0 4px' }}>机器指纹</div>
        <code style={{
          display: 'block', background: '#0b0e13', border: '1px solid #2a313c',
          borderRadius: '8px', padding: '10px 12px', fontSize: 12,
          lineHeight: 1.8, whiteSpace: 'pre', userSelect: 'all',
        }}>{fpText}</code>
        <button onClick={copyFp} style={btnGhost}>{copied ? '已复制 ✓' : '复制指纹'}</button>

        <div style={{ fontSize: 12, color: '#8a93a3', margin: '16px 0 4px' }}>
          激活码（卖家提供，长串带连字符）
        </div>
        <textarea
          value={code}
          onChange={(e) => setCode(e.target.value)}
          rows={3}
          placeholder="粘贴激活码…"
          style={{
            width: '100%', background: '#0b0e13', color: '#d7dde6',
            border: '1px solid #2a313c', borderRadius: '8px',
            padding: '10px 12px', fontSize: 12, resize: 'vertical',
            fontFamily: 'inherit', boxSizing: 'border-box',
          }}
        />
        <button onClick={activate} disabled={busy} style={btnPrimary}>
          {busy ? '激活中…' : '激活'}
        </button>

        {msg && (
          <div style={{
            marginTop: 12, padding: '10px 12px', borderRadius: '8px',
            fontSize: 13, background: msg.ok ? 'rgba(63,185,107,.12)' : 'rgba(224,96,79,.12)',
            color: msg.ok ? '#3fb96b' : '#e0604f',
          }}>{msg.text}</div>
        )}
      </div>
    </div>
  );
}

const btnPrimary: React.CSSProperties = {
  marginTop: 14, padding: '9px 22px', borderRadius: '8px',
  background: '#4f8cff', color: '#fff', border: 'none',
  fontSize: 14, cursor: 'pointer',
};
const btnGhost: React.CSSProperties = {
  ...btnPrimary, background: 'transparent', color: '#8a93a3',
  border: '1px solid #2a313c', marginTop: 10,
};
