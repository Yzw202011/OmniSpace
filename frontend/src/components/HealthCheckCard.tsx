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
  runDiagnose,
  runHealthCheck,
  runHealthRepair,
  type DiagnoseResult,
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

/** 深度体检状态色（后端语义：pass/warn/fail） */
const DIAG_META: Record<string, { color: string; label: string }> = {
  pass: { color: 'var(--color-success)', label: '通过' },
  warn: { color: 'var(--color-warning)', label: '注意' },
  fail: { color: 'var(--color-error)', label: '异常' },
};

export default function HealthCheckCard() {
  const [report, setReport] = useState<HealthCheckReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [repairing, setRepairing] = useState('');
  /** 深度体检（26 项环境级真实探测，审计 BK-002；2026-09-17 接线） */
  const [deep, setDeep] = useState<DiagnoseResult | null>(null);
  const [deepLoading, setDeepLoading] = useState(false);

  const runDeep = async (): Promise<void> => {
    setDeepLoading(true);
    try {
      setDeep(await runDiagnose());
    } catch (err) {
      reportActionError(err, '深度体检');
    } finally {
      setDeepLoading(false);
    }
  };

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
        <button
          className="btn btn-outline btn-sm"
          disabled={deepLoading}
          title="环境级 27 项真实探测（硬件/数据库/引擎/目录），与上方运行时体检互补"
          onClick={() => {
            void runDeep();
          }}
        >
          {deepLoading ? '深度体检中…' : '深度体检'}
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

      {/* 深度体检结果（26 项环境级；audit BK-002） */}
      {deep !== null && (
        <div style={{ marginTop: 'var(--space-3)', borderTop: '1px solid var(--color-border, rgba(128,128,128,.25))', paddingTop: 'var(--space-2)' }}>
          <p className="settings-section-desc">
            深度体检：{deep.summary.pass} 通过 / {deep.summary.warn} 注意 /{' '}
            {deep.summary.fail} 异常（共 {deep.summary.total} 项，只读探测不改动系统）
          </p>
          <table className="table" style={{ fontSize: 12 }}>
            <thead>
              <tr><th>#</th><th>检测项</th><th>结果</th><th>详情</th></tr>
            </thead>
            <tbody>
              {deep.items.map((it) => {
                const meta = DIAG_META[it.status] ?? DIAG_META.warn;
                return (
                  <tr key={it.index}>
                    <td>{it.index}</td>
                    <td>{it.name}</td>
                    <td style={{ color: meta.color }}>{meta.label}</td>
                    <td style={{ opacity: 0.8 }}>{it.detail}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
