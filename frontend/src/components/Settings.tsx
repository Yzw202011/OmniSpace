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

import { useState, useEffect } from 'react';
import { useAppStore, type FontSize } from '@/stores/useAppStore';
import { useHardwareStore } from '@/stores/useHardwareStore';
import { FEATURE_SWITCH_RULES, FEATURE_LABELS } from '@/types';
import type { ActiveFeature } from '@/types';

/** 字号档位配置（实际生效值见 sakura.css [data-font-size] 覆盖：基准 --font-size-base） */
const FONT_SIZE_OPTIONS: Array<{ value: FontSize; label: string; desc: string }> = [
  { value: 'sm', label: '小', desc: '13px' },
  { value: 'md', label: '中', desc: '14px（默认）' },
  { value: 'lg', label: '大', desc: '16px' },
];

export default function Settings() {
  const fontSize = useAppStore((s) => s.fontSize);
  const setFontSize = useAppStore((s) => s.setFontSize);
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
      {/* ============ 外观设置 ============ */}
      <div className="settings-section card">
        <h2 className="settings-section-title">外观</h2>

        {/* 主题 */}
        <div className="settings-row">
          <div className="settings-row-label">
            <span className="settings-row-name">主题</span>
            <span className="settings-row-desc">Sakura 唯一主题（COM-009）</span>
          </div>
          <div className="settings-row-control">
            <span className="settings-static-value">🌸 Sakura</span>
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
