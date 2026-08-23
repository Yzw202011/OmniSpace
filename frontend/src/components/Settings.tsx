/* ==========================================================================
 * OmniSpace AI v2.1 —— 设置页
 * --------------------------------------------------------------------------
 * 展示与操作：
 *   - 字号档位（sm / md / lg，COM-016 全中文）
 *   - 主题信息（Sakura 唯一主题，COM-009）
 *   - 全局设置键值表（来自后端 /system/settings，只读展示 + 可编辑项）
 *   - 功能互斥规则说明（规格 §6.1）
 *   - 保存设置到后端（useAppStore.saveSettings）
 * 覆盖测试用例：COM-009（Sakura 唯一主题）、COM-016（全中文界面）
 * ========================================================================== */

import { useState, useEffect, type CSSProperties } from 'react';
import { Settings as SettingsIcon } from 'lucide-react';
import { useAppStore, type FontSize, type Theme } from '@/stores/useAppStore';
import { useHardwareStore } from '@/stores/useHardwareStore';
import { FEATURE_SWITCH_RULES, FEATURE_LABELS } from '@/types';
import type { ActiveFeature } from '@/types';

/** 主题四态配置（双主题体系 × 亮暗双模式，2026-08-20 用户裁定脱离 COM-009） */
const THEME_OPTIONS: Array<{
  value: Theme;
  name: string;
  desc: string;
  /** 色板：[主色, 辅助色, 背景色, 顶部辉光] */
  colors: [string, string, string, string];
}> = [
  {
    value: 'sakura',
    name: 'Sakura · 夜樱',
    desc: '暗色樱粉 × 薄荷绿',
    colors: ['#FF6B9D', '#4ECDC4', '#1A1A2E', 'rgba(255,107,157,0.45)'],
  },
  {
    value: 'light',
    name: 'Sakura · 拂晓',
    desc: '亮色樱粉 × 柔白',
    colors: ['#FF6B9D', '#4ECDC4', '#F8F9FC', 'rgba(255,107,157,0.35)'],
  },
  {
    value: 'tech',
    name: 'Nebula · 深空',
    desc: '高科技电光青 × 星云紫',
    colors: ['#22D3EE', '#8B7CF8', '#060B18', 'rgba(34,211,238,0.45)'],
  },
  {
    value: 'tech-light',
    name: 'Nebula · 晨辉',
    desc: '高科技冰蓝 × 淡紫',
    colors: ['#0891B2', '#7C6BE8', '#EEF4FB', 'rgba(8,145,178,0.35)'],
  },
];

/** 字号档位配置（实际生效值见 sakura.css [data-font-size] 覆盖：基准 --font-size-base） */
const FONT_SIZE_OPTIONS: Array<{ value: FontSize; label: string; desc: string }> = [
  { value: 'sm', label: '小', desc: '13px' },
  { value: 'md', label: '中', desc: '14px（默认）' },
  { value: 'lg', label: '大', desc: '16px' },
];

export default function Settings() {
  const fontSize = useAppStore((s) => s.fontSize);
  const setFontSize = useAppStore((s) => s.setFontSize);
  const theme = useAppStore((s) => s.theme);
  const setTheme = useAppStore((s) => s.setTheme);
  const settings = useAppStore((s) => s.settings);
  const settingsLoaded = useAppStore((s) => s.settingsLoaded);
  const loadSettings = useAppStore((s) => s.loadSettings);
  const saveSettings = useAppStore((s) => s.saveSettings);
  const showToast = useAppStore((s) => s.showToast);

  const hardwareProfile = useHardwareStore((s) => s.hardwareProfile);
  const profileLoaded = useHardwareStore((s) => s.profileLoaded);
  const refreshProfile = useHardwareStore((s) => s.refreshProfile);

  // 本地编辑态：允许用户修改文本类设置项后再保存
  const [editableSettings, setEditableSettings] = useState<Record<string, unknown>>({});

  useEffect(() => {
    if (!settingsLoaded) {
      loadSettings();
    }
  }, [settingsLoaded, loadSettings]);

  // 后端设置加载后同步到本地编辑态
  useEffect(() => {
    setEditableSettings(settings);
  }, [settings]);

  /** 保存全部设置 */
  async function handleSave() {
    await saveSettings(editableSettings);
    showToast('设置已保存', 'success');
  }

  /** 重置为已加载的后端值 */
  function handleReset() {
    setEditableSettings(settings);
    showToast('已重置为服务端值', 'info');
  }

  // 将设置对象转为可展示的键值对
  const settingEntries = Object.entries(editableSettings);
  const knownKeys = new Set(['language', 'auto_save_interval', 'default_model', 'ws_reconnect_interval']);

  return (
    <div className="page settings-page">
      <h1 className="page-title"><SettingsIcon size={20} aria-hidden="true" /> 设置</h1>
      <p className="page-subtitle">外观 · 全局参数 · 功能开关规则 · 硬件信息</p>

      {/* ============ 外观设置 ============ */}
      <div className="settings-section card">
        <h2 className="settings-section-title">外观</h2>

        {/* 主题（双主题体系 × 亮暗双模式） */}
        <div className="settings-row" style={{ alignItems: 'stretch', flexDirection: 'column', gap: 'var(--space-3)' }}>
          <div className="settings-row-label">
            <span className="settings-row-name">主题</span>
            <span className="settings-row-desc">双主题体系：Sakura 樱花系列 / Nebula 星云科技系列，各含亮暗双模式</span>
          </div>
          <div className="theme-grid" role="radiogroup" aria-label="主题选择">
            {THEME_OPTIONS.map((opt) => (
              <button
                key={opt.value}
                type="button"
                role="radio"
                aria-checked={theme === opt.value}
                className={`theme-option ${theme === opt.value ? 'active' : ''}`}
                onClick={() => setTheme(opt.value)}
                title={opt.desc}
              >
                <div
                  className="theme-option-swatch"
                  style={{ background: opt.colors[2], '--swatch-glow': opt.colors[3] } as CSSProperties}
                >
                  <span className="theme-dot" style={{ background: opt.colors[0] }} aria-hidden="true" />
                  <span className="theme-dot" style={{ background: opt.colors[1] }} aria-hidden="true" />
                </div>
                <span className="theme-option-name">{opt.name}</span>
                <span className="theme-option-desc">{opt.desc}</span>
              </button>
            ))}
          </div>
        </div>

        {/* 字号 */}
        <div className="settings-row">
          <div className="settings-row-label">
            <span className="settings-row-name">字号</span>
            <span className="settings-row-desc">影响全局界面文字大小</span>
          </div>
          <div className="settings-row-control">
            <div className="settings-font-size-group">
              {FONT_SIZE_OPTIONS.map((opt) => (
                <button
                  key={opt.value}
                  className={`settings-font-size-btn ${fontSize === opt.value ? 'active' : ''}`}
                  onClick={() => setFontSize(opt.value)}
                  title={opt.desc}
                >
                  {opt.label}
                </button>
              ))}
            </div>
          </div>
        </div>
      </div>

      {/* ============ 全局设置（后端 /system/settings） ============ */}
      <div className="settings-section card">
        <div className="settings-section-header">
          <h2 className="settings-section-title">全局设置</h2>
          <div className="settings-section-actions">
            <button className="btn btn-secondary btn-sm" onClick={handleReset}>
              重置
            </button>
            <button className="btn btn-primary btn-sm" onClick={handleSave}>
              保存
            </button>
          </div>
        </div>

        {!settingsLoaded ? (
          <div className="settings-loading">加载中…</div>
        ) : settingEntries.length === 0 ? (
          <div className="settings-empty">
            <p>暂无可配置项，后端服务可能未就绪。</p>
            <button className="btn btn-secondary btn-sm" onClick={() => loadSettings()}>
              重新加载
            </button>
          </div>
        ) : (
          <div className="settings-list">
            {settingEntries.map(([key, value]) => {
              const name = formatSettingKey(key);
              return (
              <div key={key} className="settings-row">
                <div className="settings-row-label">
                  <span className="settings-row-name">{name}</span>
                  {name !== key && <span className="settings-row-desc">{key}</span>}
                </div>
                <div className="settings-row-control">
                  {knownKeys.has(key) ? (
                    <input
                      className="input settings-input"
                      value={String(value ?? '')}
                      onChange={(e) =>
                        setEditableSettings((prev) => ({
                          ...prev,
                          [key]: e.target.value,
                        }))
                      }
                    />
                  ) : (
                    <span className="settings-static-value">
                      {formatSettingValue(value)}
                    </span>
                  )}
                </div>
              </div>
              );
            })}
          </div>
        )}
      </div>

      {/* ============ 功能互斥规则（规格 §6.1） ============ */}
      <div className="settings-section card">
        <h2 className="settings-section-title">功能互斥规则</h2>
        <p className="settings-section-desc">
          四种重量级 AI 功能同一时刻仅允许一类运行，避免显存溢出（规格 §6.1）。
        </p>
        <div className="settings-feature-rules">
          {(Object.keys(FEATURE_LABELS) as Array<Exclude<ActiveFeature, null>>).map((feature) => {
            const rule = FEATURE_SWITCH_RULES[feature];
            return (
              <div key={feature} className="settings-feature-rule-row">
                <span className="settings-feature-name">{FEATURE_LABELS[feature]}</span>
                <span className="settings-feature-blocked">
                  {rule.blocked.length > 0
                    ? rule.blocked.map((b) => FEATURE_LABELS[b]).join('、')
                    : '无阻断'}
                </span>
              </div>
            );
          })}
        </div>
      </div>

      {/* ============ 硬件信息 ============ */}
      <div className="settings-section card">
        <div className="settings-section-header">
          <h2 className="settings-section-title">硬件信息</h2>
          <button
            className="btn btn-secondary btn-sm"
            onClick={() => refreshProfile()}
          >
            刷新
          </button>
        </div>
        {!profileLoaded ? (
          <div className="settings-loading">加载中…</div>
        ) : !hardwareProfile ? (
          <div className="settings-empty">
            <p>无法获取硬件信息，后端服务可能未就绪。</p>
          </div>
        ) : (
          <>
          <div className="settings-hardware-grid">
            <div className="settings-hw-item">
              <span className="settings-hw-label">GPU</span>
              <span className="settings-hw-value">{hardwareProfile.gpu.name}</span>
            </div>
            <div className="settings-hw-item">
              <span className="settings-hw-label">显存</span>
              <span className="settings-hw-value">
                {hardwareProfile.gpu.vram_total_mb >= 1024
                  ? `${(hardwareProfile.gpu.vram_total_mb / 1024).toFixed(1)} GB`
                  : `${hardwareProfile.gpu.vram_total_mb} MB`}
              </span>
            </div>
            <div className="settings-hw-item">
              <span className="settings-hw-label">CPU</span>
              <span className="settings-hw-value">{hardwareProfile.cpu.name}</span>
            </div>
            <div className="settings-hw-item">
              <span className="settings-hw-label">核心/线程</span>
              <span className="settings-hw-value">
                {hardwareProfile.cpu.cores} / {hardwareProfile.cpu.threads}
              </span>
            </div>
            <div className="settings-hw-item">
              <span className="settings-hw-label">内存</span>
              <span className="settings-hw-value">
                {hardwareProfile.ram.total_mb >= 1024
                  ? `${(hardwareProfile.ram.total_mb / 1024).toFixed(1)} GB`
                  : `${hardwareProfile.ram.total_mb} MB`}
              </span>
            </div>
            <div className="settings-hw-item">
              <span className="settings-hw-label">磁盘</span>
              <span className="settings-hw-value">
                {hardwareProfile.disk.total_gb.toFixed(0)} GB
              </span>
            </div>
          </div>
          {hardwareProfile.degradation?.degraded && (
            <div className="settings-degradation" role="alert">
              <span className="settings-degradation-dot" aria-hidden="true" />
              <span>
                {hardwareProfile.degradation.reason ||
                  '当前为 DirectML/CPU 降级档，生成速度可能较慢。'}
              </span>
            </div>
          )}
          </>
        )}
      </div>
    </div>
  );
}

/* ============================== 辅助函数 ============================== */

/** 设置键中文映射 */
const SETTING_KEY_LABELS: Record<string, string> = {
  language: '界面语言',
  auto_save_interval: '自动保存间隔',
  default_model: '默认模型',
  ws_reconnect_interval: 'WS 重连间隔',
};

function formatSettingKey(key: string): string {
  return SETTING_KEY_LABELS[key] || key;
}

function formatSettingValue(value: unknown): string {
  if (value === null || value === undefined) return '--';
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}
