/**
 * AnalysisReport 学习分析报表（LEARN-049~052 前端接线，2026-09-17）
 * --------------------------------------------------------------------------
 * 四件套：效率 / 来源 / 趋势 / 主题对比 + 行为训练对预览。
 * 后端：GET /learn/analysis/{efficiency,sources,trend,topic-compare}
 *      GET /behavior/training-pairs（LoRA 微调数据预览）。
 * 纯只读报表（无副作用），天数切换 7/30/90；数据为空时诚实空态。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { BarChart3, Loader2, RefreshCw } from 'lucide-react';
import {
  getBehaviorTrainingPairs,
  getLearnEfficiency,
  getLearnSources,
  getLearnTopicCompare,
  getLearnTrend,
  type LearnEfficiency,
  type LearnSourceItem,
  type LearnTopicCompareItem,
  type LearnTrendItem,
  type TrainingPair,
} from '../../services/learningApi';
import { useAppStore } from '../../stores/useAppStore';

const DAY_OPTIONS = [7, 30, 90] as const;

function formatTime(ts: number): string {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false });
}

export default function AnalysisReport() {
  const showToast = useAppStore((s) => s.showToast);
  const [days, setDays] = useState<number>(30);
  /** 请求序号守卫：见 refresh 内注释 */
  const refreshSeq = useRef(0);
  const [loading, setLoading] = useState(false);
  const [efficiency, setEfficiency] = useState<LearnEfficiency | null>(null);
  const [sources, setSources] = useState<LearnSourceItem[]>([]);
  const [trend, setTrend] = useState<LearnTrendItem[]>([]);
  const [topics, setTopics] = useState<LearnTopicCompareItem[]>([]);
  const [pairs, setPairs] = useState<{ items: TrainingPair[]; total: number; should: boolean } | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    // 请求序号守卫（09-17 审计竞态②）：快速切 7/30/90 天时防止
    // 先发的旧响应覆盖后发的新选择（last-resolved 胜出改为 last-issued 胜出）
    const seq = ++refreshSeq.current;
    try {
      const [eff, src, tr, tp, pr] = await Promise.all([
        getLearnEfficiency(days),
        getLearnSources(10),
        getLearnTrend(days),
        getLearnTopicCompare(),
        getBehaviorTrainingPairs(3),
      ]);
      if (seq !== refreshSeq.current) {
        return;
      }
      setEfficiency(eff);
      setSources(src);
      setTrend(tr);
      setTopics(tp);
      setPairs({ items: pr.items, total: pr.total, should: pr.should_trigger_finetune });
    } catch {
      if (seq === refreshSeq.current) {
        showToast('学习分析加载失败，请重试', 'error');
      }
    } finally {
      if (seq === refreshSeq.current) {
        setLoading(false);
      }
    }
  }, [days, showToast]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const maxTrend = Math.max(1, ...trend.map((t) => t.count));

  return (
    <section className="card" aria-label="学习分析">
      <div className="flex items-center justify-between">
        <h3 className="card-title">
          <BarChart3 size={16} aria-hidden="true" /> 学习分析
        </h3>
        <div className="flex gap-2 items-center">
          {DAY_OPTIONS.map((d) => (
            <button
              key={d}
              type="button"
              className={`btn ${days === d ? 'btn-primary' : 'btn-outline'}`}
              style={{ padding: '2px 10px', fontSize: 12 }}
              onClick={() => setDays(d)}
            >
              近{d}天
            </button>
          ))}
          <button
            type="button"
            className="btn btn-outline"
            style={{ padding: '2px 10px' }}
            disabled={loading}
            onClick={() => void refresh()}
          >
            {loading ? <Loader2 size={14} className="animate-spin" /> : <RefreshCw size={14} />}
          </button>
        </div>
      </div>

      {/* 效率总览 */}
      {efficiency && (
        <div className="grid gap-2 mt-3" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(130px, 1fr))' }}>
          {[
            ['学习会话', `${efficiency.sessions} 次`],
            ['浏览页数', `${efficiency.pages_visited} 页`],
            ['提取知识', `${efficiency.knowledge_extracted} 条`],
            ['提取率', `${efficiency.extraction_rate} 条/页`],
            ['每小时产出', `${efficiency.per_hour} 条/h`],
            ['投入时长', `${efficiency.hours} 小时`],
          ].map(([label, value]) => (
            <div key={label} className="stat-block" style={{ padding: 'var(--space-2)' }}>
              <div style={{ fontSize: 12, opacity: 0.7 }}>{label}</div>
              <div style={{ fontSize: 16, fontWeight: 600 }}>{value}</div>
            </div>
          ))}
        </div>
      )}

      <div className="grid gap-4 mt-3" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(300px, 1fr))' }}>
        {/* 来源可信度 */}
        <div>
          <div className="form-label">知识来源 Top10（平均质量分 = 可信度）</div>
          {sources.length === 0 ? (
            <div className="text-secondary" style={{ fontSize: 12 }}>暂无带来源的知识条目。</div>
          ) : (
            <table className="table" style={{ fontSize: 12 }}>
              <thead>
                <tr><th>来源域名</th><th>条数</th><th>平均分</th></tr>
              </thead>
              <tbody>
                {sources.map((s) => (
                  <tr key={s.domain}>
                    <td title={s.domain}>{s.domain.length > 28 ? `${s.domain.slice(0, 26)}…` : s.domain}</td>
                    <td>{s.count}</td>
                    <td>{s.avg_quality_score}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        {/* 主题对比 */}
        <div>
          <div className="form-label">主题对比（条数 / 均分 / 最近学习）</div>
          {topics.length === 0 ? (
            <div className="text-secondary" style={{ fontSize: 12 }}>暂无主题数据。</div>
          ) : (
            <table className="table" style={{ fontSize: 12 }}>
              <thead>
                <tr><th>主题</th><th>条数</th><th>均分</th><th>最近</th></tr>
              </thead>
              <tbody>
                {topics.map((t) => (
                  <tr key={t.topic}>
                    <td>{t.topic}</td>
                    <td>{t.knowledge_count}</td>
                    <td>{t.avg_quality_score}</td>
                    <td title={formatTime(t.latest_at)}>{formatTime(t.latest_at).slice(5, 16)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      {/* 趋势条形 */}
      <div className="mt-3">
        <div className="form-label">每日新增知识趋势</div>
        {trend.length === 0 ? (
          <div className="text-secondary" style={{ fontSize: 12 }}>近{days}天暂无新增知识。</div>
        ) : (
          <div className="flex items-end gap-1" style={{ height: 64, overflowX: 'auto' }}>
            {trend.map((t) => (
              <div key={t.date} title={`${t.date}：${t.count} 条，均分 ${t.avg_quality_score}`}
                   style={{ flex: `0 0 ${Math.max(10, 900 / Math.max(trend.length, 1))}px` }}>
                <div style={{
                  height: `${Math.round((t.count / maxTrend) * 52) + 2}px`,
                  background: 'var(--color-accent)', borderRadius: 2, opacity: 0.85,
                }} />
                <div style={{ fontSize: 9, opacity: 0.6, textAlign: 'center' }}>{t.date.slice(5)}</div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* 行为训练对 */}
      {pairs && (
        <div className="mt-3" style={{ borderTop: '1px solid var(--color-border, rgba(128,128,128,.25))', paddingTop: 'var(--space-2)' }}>
          <div className="form-label">
            行为训练数据（共 {pairs.total} 对{pairs.should ? ' · 已达微调触发线' : ''}）
          </div>
          {pairs.items.length === 0 ? (
            <div className="text-secondary" style={{ fontSize: 12 }}>暂无行为训练数据——日常使用中会自动积累。</div>
          ) : (
            pairs.items.map((p, i) => (
              <div key={i} className="text-secondary" style={{ fontSize: 11, marginBottom: 2, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                {i + 1}. 指令「{p.instruction}」→「{p.output}」
              </div>
            ))
          )}
        </div>
      )}
    </section>
  );
}
