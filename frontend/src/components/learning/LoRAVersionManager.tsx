/**
 * LoRAVersionManager LoRA 版本管理（LEARN-035 前端接线，2026-09-17）
 * --------------------------------------------------------------------------
 * learn 训练域的版本三件：列表（质量分/样本数/状态）+ 版本对比
 * （GET /learn/lora/versions/compare，字段级差异表）+ 回滚。
 * 注意：与风格页（style 域）的版本库是两套独立存储。
 */
import { useCallback, useEffect, useState } from 'react';
import { GitCompare, Loader2, History, RefreshCw } from 'lucide-react';
import {
  compareLoraVersions,
  listLoraVersions,
  rollbackLoraVersion,
  type LoraVersion,
} from '../../services/learnApi';
import { useAppStore } from '../../stores/useAppStore';
import { isApiError } from '../../services/api';

type CompareField = { a: unknown; b: unknown; diff?: unknown };

export default function LoRAVersionManager() {
  const showToast = useAppStore((s) => s.showToast);
  const [versions, setVersions] = useState<LoraVersion[]>([]);
  const [current, setCurrent] = useState('');
  const [loading, setLoading] = useState(false);
  const [compareA, setCompareA] = useState('');
  const [compareB, setCompareB] = useState('');
  const [comparing, setComparing] = useState(false);
  const [fields, setFields] = useState<Record<string, CompareField> | null>(null);
  const [rollbackBusy, setRollbackBusy] = useState('');

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const res = await listLoraVersions();
      setVersions(res.items);
      setCurrent(res.current);
      if (res.items.length >= 2 && !compareA && !compareB) {
        setCompareA(res.items[res.items.length - 2].version);
        setCompareB(res.items[res.items.length - 1].version);
      }
    } catch {
      showToast('LoRA 版本列表加载失败', 'error');
    } finally {
      setLoading(false);
    }
  }, [showToast]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const onCompare = async () => {
    if (!compareA || !compareB || compareA === compareB) {
      showToast('请选择两个不同版本', 'warning');
      return;
    }
    setComparing(true);
    try {
      const res = await compareLoraVersions(compareA, compareB);
      setFields(res.fields);
    } catch (err) {
      showToast(isApiError(err) ? err.message : '对比失败', 'error');
    } finally {
      setComparing(false);
    }
  };

  const onRollback = async (version: string) => {
    if (!window.confirm(`确定回滚到 ${version}？当前生效版本将被替换。`)) return;
    setRollbackBusy(version);
    try {
      await rollbackLoraVersion(version);
      showToast(`已回滚到 ${version}`, 'success');
      await refresh();
    } catch (err) {
      showToast(isApiError(err) ? err.message : '回滚失败', 'error');
    } finally {
      setRollbackBusy('');
    }
  };

  return (
    <section className="card" aria-label="LoRA 版本管理">
      <div className="flex items-center justify-between">
        <h3 className="card-title"><History size={16} aria-hidden="true" /> LoRA 版本</h3>
        <button type="button" className="btn btn-outline" style={{ padding: '2px 10px' }} disabled={loading} onClick={() => void refresh()}>
          {loading ? <Loader2 size={14} className="animate-spin" /> : <RefreshCw size={14} />}
        </button>
      </div>
      <p className="text-secondary" style={{ fontSize: 12, margin: '4px 0 8px' }}>
        训练域版本库（与风格页版本相互独立）：质量分 / 样本数 / 字段级对比 / 回滚。
      </p>

      {versions.length === 0 ? (
        <div className="text-secondary" style={{ fontSize: 12 }}>
          {loading ? '加载中…' : '暂无版本——完成一次微调训练后生成。'}
        </div>
      ) : (
        <table className="table" style={{ fontSize: 12 }}>
          <thead>
            <tr><th>版本</th><th>质量分</th><th>样本数</th><th>状态</th><th>操作</th></tr>
          </thead>
          <tbody>
            {versions.map((v) => (
              <tr key={v.version}>
                <td>
                  {v.version}
                  {v.version === current && (
                    <span className="badge success" style={{ marginLeft: 6 }}>当前</span>
                  )}
                </td>
                <td>{v.quality_score != null ? v.quality_score : '—'}</td>
                <td>{v.data_count ?? '—'}</td>
                <td>{v.status ?? '—'}</td>
                <td>
                  <button
                    type="button"
                    className="btn btn-outline"
                    style={{ padding: '1px 8px', fontSize: 12 }}
                    disabled={v.version === current || rollbackBusy === v.version}
                    onClick={() => void onRollback(v.version)}
                  >
                    {rollbackBusy === v.version ? '…' : '回滚'}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {/* 版本对比 */}
      {versions.length >= 2 && (
        <div className="mt-3" style={{ borderTop: '1px solid var(--color-border, rgba(128,128,128,.25))', paddingTop: 'var(--space-2)' }}>
          <div className="flex gap-2 items-center flex-wrap">
            <span className="form-label" style={{ margin: 0 }}>版本对比</span>
            <select className="form-select" style={{ width: 110 }} value={compareA} onChange={(e) => setCompareA(e.target.value)} aria-label="版本 A">
              {versions.map((v) => <option key={v.version} value={v.version}>{v.version}</option>)}
            </select>
            <span>vs</span>
            <select className="form-select" style={{ width: 110 }} value={compareB} onChange={(e) => setCompareB(e.target.value)} aria-label="版本 B">
              {versions.map((v) => <option key={v.version} value={v.version}>{v.version}</option>)}
            </select>
            <button type="button" className="btn btn-primary" style={{ padding: '2px 10px' }} disabled={comparing} onClick={() => void onCompare()}>
              {comparing ? <Loader2 size={14} className="animate-spin" /> : <GitCompare size={14} />} 对比
            </button>
          </div>
          {fields && (
            <table className="table mt-2" style={{ fontSize: 12 }}>
              <thead>
                <tr><th>字段</th><th>{compareA}</th><th>{compareB}</th><th>差值</th></tr>
              </thead>
              <tbody>
                {Object.entries(fields).map(([k, f]) => (
                  <tr key={k}>
                    <td>{k}</td>
                    <td>{String(f.a ?? '—')}</td>
                    <td>{String(f.b ?? '—')}</td>
                    <td>{f.diff != null ? String(f.diff) : '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </section>
  );
}
