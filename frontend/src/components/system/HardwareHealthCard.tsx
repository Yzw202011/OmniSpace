// 本项目仅供学习使用，商业授权请+Q 3553191368
/* ==========================================================================
 * HardwareHealthCard.tsx —— 硬件健康中心（批4 P19，2026-09-19）
 * --------------------------------------------------------------------------
 * 显存/内存/温度/磁盘 四仪表（三色阈值与 resource_guard 同源）+ 保护
 * 动作清单。温度传感器 Windows 常缺——如实标「不可测」绝不编数。
 * ========================================================================== */

import { useCallback, useEffect, useState } from 'react';
import { Activity, RefreshCw } from 'lucide-react';
import { getHardwareHealth, type HardwareHealth } from '@/services/systemApi';
import { reportBgError } from '@/utils/errors';

const LEVEL_COLOR: Record<string, string> = {
  ok: 'var(--color-success)',
  warning: 'var(--color-warning)',
  danger: 'var(--color-error)',
};
const LEVEL_TEXT: Record<string, string> = {
  ok: '正常', warning: '警戒', danger: '危险',
};

function Gauge({ label, value, level, hint }: {
  label: string; value: string; level: string; hint?: string;
}) {
  return (
    <div className="card" style={{ padding: 'var(--space-3)', minWidth: 120 }} title={hint}>
      <div className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)' }}>{label}</div>
      <div className="flex items-baseline gap-2 mt-1">
        <strong style={{ fontSize: 'var(--font-size-lg)' }}>{value}</strong>
        <span style={{ color: LEVEL_COLOR[level] ?? 'var(--color-text-secondary)', fontSize: 'var(--font-size-xs)' }}>
          {LEVEL_TEXT[level] ?? level}
        </span>
      </div>
    </div>
  );
}

export default function HardwareHealthCard() {
  const [h, setH] = useState<HardwareHealth | null>(null);

  const load = useCallback(() => {
    getHardwareHealth()
      .then(setH)
      .catch((err: unknown) => {
        reportBgError('HardwareHealthCard', err);
        setH(null);
      });
  }, []);

  useEffect(() => {
    load();
    const timer = window.setInterval(load, 15_000);
    return () => window.clearInterval(timer);
  }, [load]);

  return (
    <div className="card hoverable" aria-label="硬件健康">
      <h3 className="card-title flex items-center gap-2">
        <Activity size={16} aria-hidden="true" /> 硬件健康
        <button type="button" className="btn-icon ml-auto" title="立即刷新" aria-label="立即刷新"
                onClick={load}>
          <RefreshCw size={13} />
        </button>
      </h3>
      {!h ? (
        <div className="text-secondary text-sm">读取中…</div>
      ) : (
        <>
          <div className="grid gap-2 mt-2" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(120px, 1fr))' }}>
            <Gauge
              label="显存"
              value={h.vram.available ? `${h.vram.used_mb}/${h.vram.total_mb}MB` : '不可测'}
              level={h.vram.available ? (h.vram.level ?? 'ok') : 'ok'}
              hint={`警戒 ${h.thresholds.vram_warn}% / 危险 ${h.thresholds.vram_crit}%`}
            />
            <Gauge
              label="内存"
              value={h.ram.available ? `${h.ram.used_gb}/${h.ram.total_gb}GB` : '不可测'}
              level={h.ram.available ? (h.ram.level ?? 'ok') : 'ok'}
              hint={`危险线 ${h.thresholds.ram_crit}%`}
            />
            <Gauge
              label="GPU 温度"
              value={h.temp.available ? `${h.temp.celsius}°C` : '不可测'}
              level={h.temp.available ? (h.temp.level ?? 'ok') : 'ok'}
              hint="≥80°C 警戒 / ≥90°C 暂停接单（传感器缺失时如实标不可测）"
            />
            {h.disks.map((d) => (
              <Gauge key={d.drive}
                     label={`磁盘 ${d.drive}`}
                     value={`剩 ${d.free_gb}GB`}
                     level={d.level}
                     hint={`剩余 ${d.free_gb}GB / ${d.total_gb}GB；<32GB 警戒 / <8GB 危险`}
              />
            ))}
          </div>
          <div className="flex gap-1.5 flex-wrap mt-3">
            {h.protections.map((p) => (
              <span key={p.key}
                    className="badge success"
                    title={`保护动作${p.enabled ? '已启用' : '未启用'}`}
                    style={{ opacity: p.enabled ? 1 : 0.4 }}>
                ✓ {p.label}
              </span>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
