// 本项目仅供学习使用，商业授权请+Q 3559331368
/* ==========================================================================
 * LogSupportWidgets —— 日志页配套组件（2026-09-01 日志机制方案 C）
 * --------------------------------------------------------------------------
 * 三个部件：
 *  1. ExportDiagnosticsDialog  导出诊断包弹窗（时间范围/包含项/脱敏 → zip 下载）
 *  2. ErrorSummaryCard         最近异常聚合卡（模块×事件类型 计数排序）
 *  3. ResourceChart            显存/内存占用曲线（30s 采样，近 2 小时）
 * ========================================================================== */

import { useEffect, useMemo, useState } from 'react';
import { Activity, AlertTriangle, Download, Loader2 } from 'lucide-react';
import { useAppStore } from '@/stores/useAppStore';
import { API_BASE } from '@/services/api';
import { getErrorSummary } from '@/services/logApi';
import { getResourceSamples } from '@/services/hardwareApi';
import { Modal } from '../common/Modal';
import { reportBgError } from '@/utils/errors';

/* ─────────────────────── 1. 导出诊断包弹窗 ─────────────────────── */

const HOUR_OPTIONS = [
  { value: 1, label: '最近 1 小时' },
  { value: 24, label: '最近 24 小时' },
  { value: 168, label: '最近 7 天' },
  { value: 720, label: '最近 30 天' },
];

const INCLUDE_ITEMS = [
  { key: 'events', label: '事件记录', hint: '大白话事件（JSON+CSV）' },
  { key: 'flows', label: '失败流程明细', hint: '最近失败生成的全链路' },
  { key: 'raw', label: '原始日志', hint: '后端/启动/引擎日志文件' },
  { key: 'hardware', label: '硬件快照', hint: 'GPU/内存/资源采样' },
];

export function ExportDiagnosticsDialog({ onClose }: { onClose: () => void }) {
  const showToast = useAppStore((s) => s.showToast);
  const [hours, setHours] = useState(24);
  const [include, setInclude] = useState<string[]>(['events', 'flows', 'raw', 'hardware']);
  const [sanitize, setSanitize] = useState(false);
  const [busy, setBusy] = useState(false);

  const toggle = (key: string) =>
    setInclude((prev) =>
      prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key],
    );

  /** 导出并下载 zip（下载用浏览器原生 a[download]，与绘画页下载同款） */
  const handleExport = async () => {
    if (include.length === 0) {
      showToast('至少勾选一项内容', 'warning');
      return;
    }
    setBusy(true);
    try {
      const qs = new URLSearchParams({
        hours: String(hours),
        include: include.join(','),
        sanitize: String(sanitize),
        failed_flows: '10',
      });
      const resp = await fetch(`${API_BASE}/logs/export?${qs.toString()}`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `omnispace_diagnostics_${Date.now()}.zip`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      showToast('诊断包已导出（zip）', 'success');
      onClose();
    } catch {
      showToast('导出失败：后端服务可能未启动', 'error');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title="导出诊断包"
      width={480}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn btn-ghost" onClick={onClose} disabled={busy}>
            取消
          </button>
          <button type="button" className="btn btn-primary" onClick={() => void handleExport()} disabled={busy}>
            {busy ? <Loader2 size={14} className="animate-spin" /> : <Download size={14} />}
            导出 zip
          </button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <div>
          <div className="text-secondary mb-1.5" style={{ fontSize: 'var(--font-size-sm)' }}>时间范围</div>
          <div className="flex gap-2 flex-wrap">
            {HOUR_OPTIONS.map((o) => (
              <button
                key={o.value}
                type="button"
                className={`btn ${hours === o.value ? 'btn-primary' : 'btn-ghost'}`}
                style={{ padding: '4px 12px', fontSize: 'var(--font-size-xs)' }}
                onClick={() => setHours(o.value)}
              >
                {o.label}
              </button>
            ))}
          </div>
        </div>
        <div>
          <div className="text-secondary mb-1.5" style={{ fontSize: 'var(--font-size-sm)' }}>包含内容</div>
          <div className="flex flex-col gap-1.5">
            {INCLUDE_ITEMS.map((it) => (
              <label key={it.key} className="flex items-center gap-2 cursor-pointer" style={{ fontSize: 'var(--font-size-sm)' }}>
                <input type="checkbox" checked={include.includes(it.key)} onChange={() => toggle(it.key)} />
                <span>{it.label}</span>
                <span className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)' }}>{it.hint}</span>
              </label>
            ))}
          </div>
        </div>
        <label className="flex items-center gap-2 cursor-pointer" style={{ fontSize: 'var(--font-size-sm)' }}>
          <input type="checkbox" checked={sanitize} onChange={(e) => setSanitize(e.target.checked)} />
          脱敏导出（提示词/描述词打码——发给别人看时勾选）
        </label>
      </div>
    </Modal>
  );
}

/* ─────────────────────── 2. 最近异常聚合卡 ─────────────────────── */

export function ErrorSummaryCard() {
  const [data, setData] = useState<Awaited<ReturnType<typeof getErrorSummary>> | null>(null);

  useEffect(() => {
    void getErrorSummary(7)
      .then(setData)
      .catch((err: unknown) => reportBgError('ErrorSummaryCard', err));
  }, []);

  if (!data || data.total_errors === 0) return null;

  return (
    <div className="card hoverable mt-3" style={{ padding: 'var(--space-3) var(--space-4)' }}>
      <div className="flex items-center gap-2" style={{ marginBottom: 'var(--space-2)' }}>
        <AlertTriangle size={14} style={{ color: 'var(--color-danger)' }} aria-hidden="true" />
        <span className="text-secondary" style={{ fontWeight: 600, fontSize: 'var(--font-size-sm)' }}>
          最近 7 天异常聚合
        </span>
        <span className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)' }}>
          共 {data.total_errors} 条错误，按类型归并
        </span>
      </div>
      <div className="flex flex-col gap-1">
        {data.groups.slice(0, 6).map((g) => (
          <div key={`${g.module}:${g.event}`} className="flex items-center gap-2" style={{ fontSize: 'var(--font-size-xs)' }}>
            <span className="badge error" style={{ minWidth: 44, justifyContent: 'center' }}>×{g.count}</span>
            <span className="text-secondary" style={{ minWidth: 90 }}>
              [{g.module}]
            </span>
            <span className="text-tertiary ellipsis flex-1" title={g.example}>
              {g.example}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

/* ─────────────────────── 3. 资源占用曲线 ─────────────────────── */

type Sample = {
  timestamp: number;
  ram_pct: number | null;
  vram_pct: number | null;
};

/** 折线 path（0~100 → 高度映射；null 断开分段） */
function buildPath(samples: Sample[], pick: (s: Sample) => number | null, w: number, h: number): string {
  const n = samples.length;
  if (n === 0) return '';
  const step = n > 1 ? w / (n - 1) : 0;
  const segs: string[] = [];
  let cur: string[] = [];
  samples.forEach((s, i) => {
    const v = pick(s);
    if (v == null) {
      if (cur.length) segs.push(cur.join(' '));
      cur = [];
      return;
    }
    const x = i * step;
    const y = h - (Math.min(100, Math.max(0, v)) / 100) * h;
    cur.push(`${cur.length === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`);
  });
  if (cur.length) segs.push(cur.join(' '));
  return segs.join(' ');
}

export function ResourceChart() {
  const [samples, setSamples] = useState<Sample[]>([]);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let stopped = false;
    const load = () =>
      void getResourceSamples(480)
        .then((r) => {
          if (!stopped) setSamples(r.samples as Sample[]);
        })
        .catch((err: unknown) => reportBgError('ResourceSamples', err))
        .finally(() => {
          if (!stopped) setLoaded(true);
        });
    load();
    const t = window.setInterval(load, 30_000);
    return () => {
      stopped = true;
      window.clearInterval(t);
    };
  }, []);

  const W = 640;
  const H = 120;
  const vramPath = useMemo(() => buildPath(samples, (s) => s.vram_pct, W, H), [samples]);
  const ramPath = useMemo(() => buildPath(samples, (s) => s.ram_pct, W, H), [samples]);
  const last = samples.at(-1);

  return (
    <div className="card hoverable mt-4" style={{ padding: 'var(--space-4)' }}>
      <div className="flex items-center gap-2 flex-wrap">
        <Activity size={16} style={{ color: 'var(--color-primary)' }} aria-hidden="true" />
        <h3 className="card-title">资源占用曲线（近 2 小时，每 30 秒采样）</h3>
        <span className="flex-1" />
        <span className="flex items-center gap-1 text-tertiary" style={{ fontSize: 'var(--font-size-xs)' }}>
          <i className="inline-block rounded-full" style={{ width: 8, height: 8, background: 'var(--color-primary)' }} />
          显存 {last?.vram_pct != null ? `${last.vram_pct.toFixed(0)}%` : '—'}
        </span>
        <span className="flex items-center gap-1 text-tertiary" style={{ fontSize: 'var(--font-size-xs)', marginLeft: 12 }}>
          <i className="inline-block rounded-full" style={{ width: 8, height: 8, background: 'var(--color-warning)' }} />
          内存 {last?.ram_pct != null ? `${last.ram_pct.toFixed(0)}%` : '—'}
        </span>
      </div>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        style={{ width: '100%', height: 140, marginTop: 'var(--space-3)' }}
        role="img"
        aria-label="显存与内存占用曲线"
      >
        {[0.25, 0.5, 0.75].map((r) => (
          <line key={r} x1={0} x2={W} y1={H * r} y2={H * r} stroke="var(--color-divider)" strokeWidth={1} />
        ))}
        <path d={ramPath} fill="none" stroke="var(--color-warning)" strokeWidth={1.5} />
        <path d={vramPath} fill="none" stroke="var(--color-primary)" strokeWidth={1.8} />
      </svg>
      {!loaded ? (
        <div className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)' }}>加载中…</div>
      ) : samples.length === 0 ? (
        <div className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)' }}>
          暂无采样数据（采样器随后端启动，每 30 秒一条，保持页面打开即可积累）
        </div>
      ) : (
        <div className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)' }}>
          曲线断开处 = 当次采集失败（诚实零读数，不伪造）
        </div>
      )}
    </div>
  );
}
