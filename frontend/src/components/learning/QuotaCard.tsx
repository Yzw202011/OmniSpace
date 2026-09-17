/**
 * QuotaCard 学习配额卡片（/learn/quota 前端接线，2026-09-17）
 * --------------------------------------------------------------------------
 * 显示当前知识库容量/主题数/流量配额使用情况；接近上限时黄色告警。
 */
import { useEffect, useState } from 'react';
import { Gauge } from 'lucide-react';
import { get } from '../../services/api';

interface QuotaData {
  knowledge?: { used?: number; limit?: number };
  topics?: { used?: number; limit?: number };
  traffic?: { today_mb?: number; daily_limit_mb?: number };
  near_capacity?: boolean;
}

export default function QuotaCard() {
  const [quota, setQuota] = useState<QuotaData | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    get<unknown>('/learn/quota')
      .then((res) => { if (alive) { setQuota(res as QuotaData); setFailed(false); } })
      .catch(() => { /* silent-intent: 后端不可达保持空卡片 */ if (alive) setFailed(true); });
    return () => { alive = false; };
  }, []);

  if (!quota) {
    if (failed) return null; // 后端不可达时静默隐藏
    return (
      <div className="card" aria-label="学习配额">
        <div className="flex items-center gap-2 text-secondary text-sm">
          <Gauge size={14} /> 配额加载中…
        </div>
      </div>
    );
  }

  const kn = quota.knowledge;
  const tp = quota.topics;
  const tr = quota.traffic;
  const pct = (used?: number, limit?: number) =>
    limit && limit > 0 ? Math.round((used ?? 0) / limit * 100) : null;

  const knPct = pct(kn?.used, kn?.limit);
  const tpPct = pct(tp?.used, tp?.limit);

  return (
    <div className="card" aria-label="学习配额">
      <div className="flex items-center justify-between mb-2">
        <h3 className="card-title" style={{ marginBottom: 0 }}>
          <Gauge size={16} aria-hidden="true" /> 学习配额
        </h3>
        {quota.near_capacity && (
          <span className="badge warning">接近上限</span>
        )}
      </div>
      <div className="flex flex-wrap gap-4 text-secondary" style={{ fontSize: 'var(--font-size-sm)' }}>
        {kn && (
          <span>
            知识库：{kn.used ?? 0} / {kn.limit ?? '∞'} 条
            {knPct !== null && `（${knPct}%）`}
          </span>
        )}
        {tp && (
          <span>
            主题：{tp.used ?? 0} / {tp.limit ?? '∞'} 个
            {tpPct !== null && `（${tpPct}%）`}
          </span>
        )}
        {tr && (
          <span>
            今日流量：{tr.today_mb ?? 0} / {tr.daily_limit_mb ?? '∞'} MB
          </span>
        )}
      </div>
    </div>
  );
}
