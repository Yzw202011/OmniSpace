/**
 * WebSearchSettings 联网搜索设置卡片
 * OmniSpace AI v2.3.1 — 架构升级计划 B-阶段一（2026-09-06）
 * --------------------------------------------------------------------------
 * 免费双通道（2026-09-05 用户拍板）：无头浏览器自搜（零配置主力）/
 * SearXNG 自建（推荐，对程序友好无反爬）/ 博查（用户自带 Key）。
 * 产品纪律：默认关闭；只上传查询词不上传聊天历史。
 */
import { useCallback, useEffect, useState } from 'react';
import {
  Globe,
  Loader2,
  Save,
} from 'lucide-react';
import {
  getWebSearchSettings,
  updateWebSearchSettings,
  type WebSearchSettings,
} from '../services/systemApi';
import { useAppStore } from '../stores/useAppStore';

const PROVIDER_LABELS: Record<WebSearchSettings['provider'], string> = {
  browser: '浏览器自搜（零配置，实测受反爬影响可能无结果）',
  searxng: 'SearXNG 自建（推荐：填实例地址即用）',
  bocha: '博查 API（需自带 Key，按量计费）',
};

const TRIGGER_LABELS: Record<WebSearchSettings['trigger'], string> = {
  auto: '智能判定（含"今天/最新/价格"等时效词时自动联网）',
  always: '每问必搜',
  off: '仅"搜索:"前缀触发',
};

export default function WebSearchSettings() {
  const [cfg, setCfg] = useState<WebSearchSettings | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [saving, setSaving] = useState(false);

  // 2026-09-15 审计修复：读配置失败明示错误并提供重试——旧实现
  // catch 后 cfg 恒 null，界面永远停在「加载中…」，用户无从得知
  // 后端不可用（静默降级不诚实）
  const load = useCallback(() => {
    getWebSearchSettings()
      .then((c) => {
        setCfg(c);
        setLoadFailed(false);
      })
      .catch(() => setLoadFailed(true));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function handleSave() {
    if (!cfg) return;
    setSaving(true);
    try {
      const next = await updateWebSearchSettings(cfg);
      setCfg(next);
      useAppStore.getState().showToast('联网搜索配置已保存', 'success');
    } catch {
      useAppStore.getState().showToast('保存失败，请检查后端服务', 'error');
    } finally {
      setSaving(false);
    }
  }

  function patch(next: Partial<WebSearchSettings>) {
    setCfg((prev) => (prev ? { ...prev, ...next } : prev));
  }

  return (
    <div className="settings-section card">
      <div className="settings-section-header">
        <h2 className="settings-section-title flex items-center gap-2">
          <Globe size={16} aria-hidden="true" /> 联网搜索
        </h2>
        <button
          className="btn btn-secondary btn-sm flex items-center gap-1"
          disabled={!cfg || saving}
          onClick={handleSave}
        >
          {saving ? (
            <Loader2 size={14} className="animate-spin" aria-hidden="true" />
          ) : (
            <Save size={14} aria-hidden="true" />
          )}
          保存
        </button>
      </div>
      <p className="settings-section-desc">
        开启后，命中时效类问题时 AI 会先联网检索再回答，回复带【1】【2】引用
        标注与来源链接。默认关闭；只上传查询词，不上传聊天历史。
      </p>
      {loadFailed && !cfg ? (
        <div className="settings-loading flex items-center gap-2">
          <span className="text-[var(--color-text-secondary)]">
            配置加载失败（后端不可用）
          </span>
          <button
            className="btn btn-secondary btn-sm"
            onClick={load}
          >
            重试
          </button>
        </div>
      ) : !cfg ? (
        <div className="settings-loading">加载中…</div>
      ) : (
        <div className="flex flex-col gap-3">
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={cfg.enabled}
              onChange={(e) => patch({ enabled: e.target.checked })}
            />
            启用联网搜索
          </label>

          <label className="flex flex-col gap-1 text-sm">
            <span className="text-[var(--color-text-secondary)]">搜索通道</span>
            <select
              className="input settings-input"
              value={cfg.provider}
              onChange={(e) =>
                patch({ provider: e.target.value as WebSearchSettings['provider'] })
              }
            >
              {(Object.keys(PROVIDER_LABELS) as Array<WebSearchSettings['provider']>).map(
                (p) => (
                  <option key={p} value={p}>
                    {PROVIDER_LABELS[p]}
                  </option>
                ),
              )}
            </select>
          </label>

          {cfg.provider === 'searxng' && (
            <label className="flex flex-col gap-1 text-sm">
              <span className="text-[var(--color-text-secondary)]">
                SearXNG 实例地址（如 http://127.0.0.1:8888）
              </span>
              <input
                className="input settings-input"
                value={cfg.searxng_url}
                placeholder="http://127.0.0.1:8888"
                onChange={(e) => patch({ searxng_url: e.target.value })}
              />
            </label>
          )}

          {cfg.provider === 'bocha' && (
            <label className="flex flex-col gap-1 text-sm">
              <span className="text-[var(--color-text-secondary)]">
                博查 API Key（仅本机保存，按量计费）
              </span>
              <input
                className="input settings-input"
                type="password"
                value={cfg.bocha_key}
                placeholder="粘贴你的博查 API Key"
                onChange={(e) => patch({ bocha_key: e.target.value })}
              />
            </label>
          )}

          <label className="flex flex-col gap-1 text-sm">
            <span className="text-[var(--color-text-secondary)]">触发时机</span>
            <select
              className="input settings-input"
              value={cfg.trigger}
              onChange={(e) =>
                patch({ trigger: e.target.value as WebSearchSettings['trigger'] })
              }
            >
              {(Object.keys(TRIGGER_LABELS) as Array<WebSearchSettings['trigger']>).map(
                (t) => (
                  <option key={t} value={t}>
                    {TRIGGER_LABELS[t]}
                  </option>
                ),
              )}
            </select>
          </label>
        </div>
      )}
    </div>
  );
}
// 本项目仅供学习使用，商业授权请+Q 3559331368
