/* ==========================================================================
 * LearningSettings.tsx —— 学习设置（TASK-036）
 * --------------------------------------------------------------------------
 * 联网开关 / 时长页数上限 / 搜索引擎 / 黑白名单 / 广告过滤 / 流量限制 /
 * 显示浏览器 / 自动微调频率 / 行为学习开关
 * 修改即保存：PUT /v1/learn/settings
 * ========================================================================== */

import React, { useEffect } from 'react';
import { Settings } from 'lucide-react';
import { useLearningStore } from '@/stores/useLearningStore';
import type { LearnSettings } from '@/services/learningApi';

/** 开关行 */
function ToggleRow({
  label,
  hint,
  checked,
  onChange,
}: {
  label: string;
  hint?: string;
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <div className="flex items-center justify-between" style={{ padding: 'var(--space-2) 0' }}>
      <div>
        <div className="text-sm">{label}</div>
        {hint && <div className="form-hint">{hint}</div>}
      </div>
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        style={{ width: 18, height: 18, accentColor: 'var(--color-primary)', cursor: 'pointer' }}
      />
    </div>
  );
}

/** 数字输入行 */
function NumberRow({
  label,
  hint,
  value,
  min,
  max,
  unit,
  onChange,
}: {
  label: string;
  hint?: string;
  value: number;
  min: number;
  max: number;
  unit?: string;
  onChange: (v: number) => void;
}) {
  return (
    <div className="flex items-center justify-between gap-3" style={{ padding: 'var(--space-2) 0' }}>
      <div>
        <div className="text-sm">{label}</div>
        {hint && <div className="form-hint">{hint}</div>}
      </div>
      <div className="flex items-center gap-2">
        <input
          className="input"
          type="number"
          min={min}
          max={max}
          value={value}
          style={{ width: 96 }}
          onChange={(e) => {
            const v = Number(e.target.value);
            if (!isNaN(v)) onChange(Math.min(max, Math.max(min, v)));
          }}
        />
        {unit && <span className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)' }}>{unit}</span>}
      </div>
    </div>
  );
}

export const LearningSettingsPanel: React.FC = () => {
  const settings = useLearningStore((s) => s.settings);
  const settingsLoaded = useLearningStore((s) => s.settingsLoaded);
  const fetchSettings = useLearningStore((s) => s.fetchSettings);
  const updateSettings = useLearningStore((s) => s.updateSettings);

  useEffect(() => {
    fetchSettings();
  }, [fetchSettings]);

  /** 文本域 ↔ 域名数组 */
  const listToText = (list: string[]) => list.join('\n');
  const textToList = (text: string) =>
    text.split(/[\n,，]/).map((s) => s.trim()).filter(Boolean);

  if (!settingsLoaded) {
    return (
      <section className="card" aria-label="学习设置">
        <h3 className="card-title"><Settings size={16} aria-hidden="true" /> 学习设置</h3>
        <div className="loading-block"><div className="spinner" /></div>
      </section>
    );
  }

  return (
    <section className="card hoverable" aria-label="学习设置">
      <h3 className="card-title"><Settings size={16} aria-hidden="true" /> 学习设置</h3>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
        {/* 左列：开关组 */}
        <div>
          <ToggleRow
            label="联网学习"
            hint="关闭后仅使用本地知识库"
            checked={settings.online_enabled}
            onChange={(v) => updateSettings({ online_enabled: v })}
          />
          <ToggleRow
            label="广告过滤"
            hint="学习浏览时拦截广告与追踪脚本"
            checked={settings.ad_filter}
            onChange={(v) => updateSettings({ ad_filter: v })}
          />
          <ToggleRow
            label="学习时显示浏览器窗口"
            hint="弹出内置浏览器实时查看 AI 操作"
            checked={settings.show_browser}
            onChange={(v) => updateSettings({ show_browser: v })}
          />
          <ToggleRow
            label="行为学习"
            hint="记录操作习惯，用于偏好微调"
            checked={settings.behavior_learning}
            onChange={(v) => updateSettings({ behavior_learning: v })}
          />
          <div style={{ padding: 'var(--space-2) 0' }}>
            <div className="text-sm mb-2">搜索引擎</div>
            <select
              className="input"
              value={settings.search_engine}
              onChange={(e) =>
                updateSettings({ search_engine: e.target.value as LearnSettings['search_engine'] })}
            >
              <option value="bing">必应 Bing</option>
              <option value="google">Google</option>
              <option value="baidu">百度</option>
              <option value="duckduckgo">DuckDuckGo</option>
            </select>
          </div>
        </div>

        {/* 右列：数值与名单 */}
        <div>
          <NumberRow
            label="单次时长上限"
            value={settings.max_duration_min}
            min={5}
            max={240}
            unit="分钟"
            onChange={(v) => updateSettings({ max_duration_min: v })}
          />
          <NumberRow
            label="单次页数上限"
            value={settings.max_pages}
            min={1}
            max={200}
            unit="页"
            onChange={(v) => updateSettings({ max_pages: v })}
          />
          <NumberRow
            label="流量限制"
            hint="0 表示不限制"
            value={settings.bandwidth_limit_kbps}
            min={0}
            max={10240}
            unit="KB/s"
            onChange={(v) => updateSettings({ bandwidth_limit_kbps: v })}
          />
          <NumberRow
            label="自动微调频率"
            hint="0 表示关闭自动微调"
            value={settings.auto_finetune_days}
            min={0}
            max={30}
            unit="天"
            onChange={(v) => updateSettings({ auto_finetune_days: v })}
          />
        </div>
      </div>

      {/* 域名黑白名单 */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-5 mt-3">
        <div>
          <div className="form-label">域名白名单（每行一个，留空表示不限制）</div>
          <textarea
            className="input"
            rows={3}
            defaultValue={listToText(settings.domain_whitelist)}
            placeholder="example.com"
            onBlur={(e) => updateSettings({ domain_whitelist: textToList(e.target.value) })}
          />
        </div>
        <div>
          <div className="form-label">域名黑名单（每行一个）</div>
          <textarea
            className="input"
            rows={3}
            defaultValue={listToText(settings.domain_blacklist)}
            placeholder="ads.example.com"
            onBlur={(e) => updateSettings({ domain_blacklist: textToList(e.target.value) })}
          />
        </div>
      </div>

      <div className="form-hint mt-3">所有修改将立即保存并生效。</div>
    </section>
  );
};

export default LearningSettingsPanel;
// 本项目仅供学习使用，商业授权请+Q 3559331368
