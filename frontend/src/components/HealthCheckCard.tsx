/* ==========================================================================
 * HealthCheckCard —— 设置页「体检与修复」卡片（自愈批4）
 * --------------------------------------------------------------------------
 * docs/自愈与横切内建方案-2026-09-10.md §批4：普通用户「不专业也不怕」——
 * 一个按钮出体检报告（大白话），可修项（清过期日志/重建目录/清确认孤儿
 * 推理进程）一键修复并自动复验。修复动作白名单制（零数据风险），后端
 * 拒绝白名单外动作。
 * ========================================================================== */
import { useState } from 'react';

import {
  runHealthCheck,
  runHealthRepair,
  type HealthCheckItem,
  type HealthCheckReport,
} from '@/services/systemApi';
import { useAppStore } from '@/stores/useAppStore';
import { reportActionError } from '@/utils/errors';

const LEVEL_META: Record<string, { color: string; label: string }> = {
  ok: { color: 'var(--color-success)', label: '正常' },
  warn: { color: 'var(--color-warning)', label: '注意' },
  fail: { color: 'var(--color-error)', label: '异常' },
  unknown: { color: 'var(--color-text-secondary, currentColor)', label: '未知' },
};

export default function HealthCheckCard() {
  const [report, setReport] = useState<HealthCheckReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [repairing, setRepairing] = useState('');

  const refresh = async (): Promise<HealthCheckReport | null> => {
    setLoading(true);
    try {
      const r = await runHealthCheck();
      setReport(r);
      return r;
    } catch (err) {
      reportActionError(err, '一键体检');
      return null;
    } finally {
      setLoading(false);
    }
  };

  const repair = async (action: string): Promise<void> => {
    setRepairing(action);
    try {
      const r = await runHealthRepair(action);
      useAppStore.getState().showToast(r.friendly, 'success');
      await refresh(); // 修完自动复验，转绿可见
    } catch (err) {
      reportActionError(err, '一键修复');
    } finally {
      setRepairing('');
    }
  };

  return (
    <div className="settings-section card">
      <div className="settings-section-header">
        <h2 className="settings-section-title">体检与修复</h2>
        <button
          className="btn btn-secondary btn-sm"
          disabled={loading}
          onClick={() => {
            void refresh();
          }}
        >
          {loading ? '体检中…' : report === null ? '开始体检' : '重新体检'}
        </button>
      </div>
      <p className="settings-section-desc">
        一键检查显存、残留进程、模型完整性、磁盘空间等运行状态；可自动处理的问题支持一键修复。
      </p>
      {report === null ? (
        <div className="settings-empty">
          <p>点击「开始体检」查看系统运行状况。</p>
        </div>
      ) : (
        <>
          <p className="settings-section-desc">
            {report.summary}（共 {report.counts.total} 项检查）
          </p>
          <div>
            {report.items.map((it: HealthCheckItem) => {
              const meta = LEVEL_META[it.level] ?? LEVEL_META.unknown;
              const fixAction =
                it.fixable === true && typeof it.fix_action === 'string'
                  ? it.fix_action
                  : null;
              return (
                <div
                  key={it.key}
                  style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '4px 0' }}
                >
                  <span
                    title={meta.label}
                    style={{
                      width: 8,
                      height: 8,
                      borderRadius: '50%',
                      background: meta.color,
                      flexShrink: 0,
                    }}
                  />
                  <span style={{ flex: 1 }}>{it.friendly}</span>
                  {fixAction !== null && (
                    <button
                      className="btn btn-secondary btn-sm"
                      disabled={repairing !== ''}
                      onClick={() => {
                        void repair(fixAction);
                      }}
                    >
                      {repairing === fixAction ? '处理中…' : '一键修复'}
                    </button>
                  )}
                </div>
              );
            })}
          </div>
        </>
      )}
    </div>
  );
}
