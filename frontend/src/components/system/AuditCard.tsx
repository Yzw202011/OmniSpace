// 本项目仅供学习使用，商业授权请+Q 3553191368
/* ==========================================================================
 * AuditCard.tsx —— 敏感操作审计卡（批5 P18，2026-09-19）
 * --------------------------------------------------------------------------
 * 删除/导出/设置变更/云端连接/激活解绑全部留痕；近 200 条可查+清空。
 * ========================================================================== */

import { useCallback, useEffect, useState } from 'react';
import { ScrollText, RefreshCw, Trash2 } from 'lucide-react';
import { get, post } from '@/services/api';
import { useAppStore } from '@/stores/useAppStore';
import { confirmDialog } from '@/stores/useConfirmStore';
import { getErrorMessage, reportBgError } from '@/utils/errors';

interface AuditItem {
  id: number;
  ts: string;
  module: string;
  action: string;
  target: string;
  result: string;
  detail: string;
}

const ACTION_LABELS: Record<string, string> = {
  project_delete: '删除作品', project_batch_delete: '批量删作品',
  sessions_batch_delete: '批量删会话', model_unregister: '注销模型',
  model_delete_files: '删模型文件', provider_delete: '删云端连接',
  settings_update: '更新设置', unbind: '解绑本机',
  diagnostics_export: '导出诊断包', audit_clear: '清空审计',
};

export default function AuditCard() {
  const showToast = useAppStore((s) => s.showToast);
  const [items, setItems] = useState<AuditItem[]>([]);
  const [loaded, setLoaded] = useState(false);

  const load = useCallback(() => {
    get<{ items: AuditItem[] }>('/system/audit/list', { limit: 200 })
      .then((d) => { setItems(d.items ?? []); setLoaded(true); })
      .catch((err: unknown) => { reportBgError('AuditCard', err); setLoaded(true); });
  }, []);

  useEffect(() => { load(); }, [load]);

  const clear = () => {
    void confirmDialog({
      level: 'medium',
      title: '清空操作审计',
      lines: ['将删除全部审计记录（不可恢复）'],
      confirmText: '清空',
    }).then((go) => {
      if (!go) return;
      post('/system/audit/clear')
        .then(() => { showToast('审计已清空', 'success'); load(); })
        .catch((err: unknown) => showToast(getErrorMessage(err, '清空失败'), 'error'));
    });
  };

  return (
    <div className="card hoverable" aria-label="操作审计">
      <h3 className="card-title flex items-center gap-2">
        <ScrollText size={16} aria-hidden="true" /> 操作审计
        <span className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)', fontWeight: 400 }}>
          {items.length} 条（删除/导出/设置/云端/激活留痕）
        </span>
        <button type="button" className="btn-icon" title="刷新" aria-label="刷新审计" onClick={load}>
          <RefreshCw size={13} />
        </button>
        {items.length > 0 && (
          <button type="button" className="btn-icon" title="清空审计" aria-label="清空审计" onClick={clear}>
            <Trash2 size={13} />
          </button>
        )}
      </h3>
      {!loaded ? (
        <div className="text-secondary text-sm">读取中…</div>
      ) : items.length === 0 ? (
        <div className="text-secondary text-sm">暂无记录——敏感操作（删除/导出/改设置/连云端）会留痕在这里</div>
      ) : (
        <div className="flex flex-col gap-1 mt-2" style={{ maxHeight: 260, overflowY: 'auto' }}>
          {items.map((it) => (
            <div key={it.id} className="flex items-center gap-2 text-xs"
                 title={it.detail || undefined}>
              <span className="text-tertiary" style={{ flexShrink: 0 }}>{it.ts}</span>
              <span className="badge primary" style={{ flexShrink: 0 }}>
                {ACTION_LABELS[it.action] ?? it.action}
              </span>
              <span className="text-secondary ellipsis" style={{ flex: 1 }}>{it.target}</span>
              {it.result !== 'ok' && <span className="badge error">{it.result}</span>}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
