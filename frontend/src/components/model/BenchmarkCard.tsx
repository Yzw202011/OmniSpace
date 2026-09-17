/**
 * BenchmarkCard 模型性能基准卡片（MODEL-038 前端接线，2026-09-17）
 * --------------------------------------------------------------------------
 * 跑分（POST /models/benchmark，对已加载对话模型 N 次真实推理）+
 * 历史对比（GET /models/benchmark/history）+ 模型导出（POST /models/export，
 * tar.gz + SHA256 侧车）。未加载模型跑分 → 后端 20012 如实透出。
 */
import { useCallback, useEffect, useState } from 'react';
import { Gauge, Loader2, Package, RefreshCw } from 'lucide-react';
import {
  exportModel,
  getBenchmarkHistory,
  listModels,
  runBenchmark,
  type BenchmarkHistoryItem,
  type BenchmarkResult,
} from '../../services/modelApi';
import type { ModelInfo } from '@/types';
import { useAppStore } from '../../stores/useAppStore';
import { isApiError } from '../../services/api';
import { reportBgError } from '../../utils/errors';

export default function BenchmarkCard() {
  const showToast = useAppStore((s) => s.showToast);
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [modelsFailed, setModelsFailed] = useState(false);
  const [historyFailed, setHistoryFailed] = useState(false);
  const [modelId, setModelId] = useState('');
  const [running, setRunning] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [result, setResult] = useState<BenchmarkResult | null>(null);
  const [history, setHistory] = useState<BenchmarkHistoryItem[]>([]);

  const refreshModels = useCallback(async () => {
    try {
      const list = await listModels();
      setModels(list);
      setModelsFailed(false);
      if (!modelId && list.length > 0) {
        const dialog = list.find((m) => m.category === 'dialog') ?? list[0];
        setModelId(dialog.id ?? '');
      }
    } catch (err) {
      // 失败如实上报（不伪装成空列表——用户无法区分「无模型」和「拉取失败」）
      reportBgError('BenchmarkCard.refreshModels', err);
      setModelsFailed(true);
    }
  }, [modelId]);

  const refreshHistory = useCallback(async (mid: string) => {
    try {
      setHistory(await getBenchmarkHistory(mid));
      setHistoryFailed(false);
    } catch (err) {
      reportBgError('BenchmarkCard.refreshHistory', err);
      setHistory([]);
      setHistoryFailed(true);
    }
  }, []);

  useEffect(() => {
    void refreshModels();
  }, [refreshModels]);

  useEffect(() => {
    if (modelId) void refreshHistory(modelId);
  }, [modelId, refreshHistory]);

  const onBenchmark = async () => {
    if (!modelId) return;
    setRunning(true);
    try {
      const res = await runBenchmark(modelId, 3);
      setResult(res);
      showToast(
        `跑分完成：${res.tokens_per_s} tok/s，首字 ${res.first_token_ms}ms` +
        (res.vram_peak_gb ? `，显存峰值 ${res.vram_peak_gb}GB` : ''),
        'success',
      );
      await refreshHistory(modelId);
    } catch (err) {
      showToast(
        isApiError(err) ? err.message : '跑分失败，请确认模型已加载',
        'error',
      );
    } finally {
      setRunning(false);
    }
  };

  const onExport = async () => {
    if (!modelId) return;
    setExporting(true);
    try {
      const res = await exportModel(modelId);
      showToast(`已导出：${res.export_path}`, 'success');
    } catch (err) {
      showToast(isApiError(err) ? err.message : '导出失败', 'error');
    } finally {
      setExporting(false);
    }
  };

  return (
    <section className="card" aria-label="性能基准">
      <div className="flex items-center justify-between">
        <h3 className="card-title"><Gauge size={16} aria-hidden="true" /> 性能基准</h3>
        <button
          type="button"
          className="btn btn-outline"
          style={{ padding: '2px 10px' }}
          disabled={running}
          onClick={() => (modelId ? void refreshHistory(modelId) : void refreshModels())}
        >
          {running ? <Loader2 size={14} className="animate-spin" /> : <RefreshCw size={14} />}
        </button>
      </div>
      <p className="text-secondary" style={{ fontSize: 12, margin: '4px 0 8px' }}>
        对<b>已加载的对话模型</b>跑 3 次真实推理测速（Tokens/s、首字延迟、显存峰值）；历史结果落库可对比。导出=模型目录打包 tar.gz（含 SHA256 校验文件）。
      </p>

      <div className="flex gap-2 flex-wrap items-center">
        <select
          className="input"
          style={{ minWidth: 220 }}
          value={modelId}
          onChange={(e) => setModelId(e.target.value)}
          aria-label="选择模型"
        >
          {models.length === 0 && <option value="">{modelsFailed ? '（模型列表拉取失败）' : '（模型列表为空）'}</option>}
          {models.map((m) => (
            <option key={m.id} value={m.id}>
              {m.name ?? m.id}（{m.category ?? '未知'}）
            </option>
          ))}
        </select>
        <button type="button" className="btn btn-primary" disabled={!modelId || running} onClick={() => void onBenchmark()}>
          {running ? <Loader2 size={14} className="animate-spin" /> : <Gauge size={14} />} 跑分
        </button>
        <button type="button" className="btn btn-outline" disabled={!modelId || exporting} onClick={() => void onExport()}>
          {exporting ? <Loader2 size={14} className="animate-spin" /> : <Package size={14} />} 导出模型包
        </button>
      </div>

      {result && (
        <div className="grid gap-2 mt-3" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(120px, 1fr))' }}>
          {[
            ['Tokens/s', `${result.tokens_per_s}`],
            ['首字延迟', `${result.first_token_ms} ms`],
            ['显存峰值', `${result.vram_peak_gb} GB`],
            ['引擎', result.engine || '—'],
          ].map(([label, value]) => (
            <div key={label} style={{ padding: 'var(--space-2)', borderRadius: 'var(--radius-md)', background: 'var(--color-input-bg)' }}>
              <div style={{ fontSize: 12, opacity: 0.7 }}>{label}</div>
              <div style={{ fontSize: 15, fontWeight: 600 }}>{value}</div>
            </div>
          ))}
        </div>
      )}

      <div className="mt-3">
        <div className="form-label">跑分历史（时间倒序）</div>
        {history.length === 0 ? (
          <div className="text-secondary" style={{ fontSize: 12 }}>{historyFailed ? '历史拉取失败（见控制台）。' : '暂无历史记录。'}</div>
        ) : (
          <table className="table" style={{ fontSize: 12 }}>
            <thead>
              <tr><th>时间</th><th>模型</th><th>Tokens/s</th><th>首字</th><th>显存峰值</th></tr>
            </thead>
            <tbody>
              {history.map((h, i) => (
                <tr key={i}>
                  <td>{h.created_at ? new Date(h.created_at * 1000).toLocaleString('zh-CN', { hour12: false }) : '—'}</td>
                  <td>{h.model_id}</td>
                  <td>{h.tokens_per_s}</td>
                  <td>{h.first_token_ms} ms</td>
                  <td>{h.vram_peak_gb} GB</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
}
