// 本项目仅供学习使用，商业授权请+Q 3559331368
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
import { useSearchParams } from 'react-router-dom';
import { Settings as SettingsIcon } from 'lucide-react';
import ModuleGuide from '@/components/common/ModuleGuide';
import { useAppStore, type FontSize, type Theme } from '@/stores/useAppStore';
import { useHardwareStore } from '@/stores/useHardwareStore';
import CloudApiSettings from '@/components/CloudApiSettings';
import WebSearchSettings from '@/components/WebSearchSettings';
import HealthCheckCard from '@/components/HealthCheckCard';
import UpgradeSection from '@/components/UpgradeSection';
import PluginSection from '@/components/PluginSection';
import DataManagementSection from '@/components/DataManagementSection';
import StorageCard from '@/components/system/StorageCard';
import HardwareHealthCard from '@/components/system/HardwareHealthCard';
import AuditCard from '@/components/system/AuditCard';
import { FEATURE_SWITCH_RULES, FEATURE_LABELS } from '@/types';
import type { ActiveFeature } from '@/types';
import * as systemApi from '@/services/systemApi';
import { setLanToken } from '@/services/api';
import { THEME_PALETTE } from '@/constants/themePalette';

/** 主题六态配置（三主题体系 × 亮暗双模式；2026-09-11 增补 Dali 治愈系）
 *  色板真源 = constants/themePalette.ts（W1 令牌化 2026-09-13，G-B13） */
const THEME_OPTIONS: Array<{
  value: Theme;
  name: string;
  desc: string;
  /** 色板：[主色, 辅助色, 背景色, 顶部辉光] */
  colors: [string, string, string, string];
}> = ([
  { value: 'sakura', name: 'Sakura · 夜樱', desc: '暗色樱粉 × 薄荷绿' },
  { value: 'light', name: 'Sakura · 拂晓', desc: '亮色樱粉 × 柔白' },
  { value: 'tech', name: 'Nebula · 深空', desc: '高科技电光青 × 星云紫' },
  { value: 'tech-light', name: 'Nebula · 晨辉', desc: '高科技冰蓝 × 淡紫' },
  { value: 'dali', name: 'Dali · 洱海月', desc: '治愈系靛海 · 我在风花雪月里等你' },
  { value: 'dali-light', name: 'Dali · 苍山雪', desc: '治愈系晨海 · 雪后初晴等你来' },
] as Array<{ value: Theme; name: string; desc: string }>).map((opt) => ({
  ...opt,
  colors: THEME_PALETTE[opt.value],
}));

/** 字号档位配置（实际生效值见 sakura.css [data-font-size] 覆盖：基准 --font-size-base） */
const FONT_SIZE_OPTIONS: Array<{ value: FontSize; label: string; desc: string }> = [
  { value: 'sm', label: '小', desc: '13px' },
  { value: 'md', label: '中', desc: '14px（默认）' },
  { value: 'lg', label: '大', desc: '16px' },
];

const SETTINGS_TABS = [
  { key: 'general', label: '通用' },
  { key: 'appearance', label: '外观' },
  { key: 'ai', label: 'AI 服务' },
  { key: 'data', label: '数据' },
  { key: 'plugins', label: '插件' },
  { key: 'maintenance', label: '维护' },
] as const;
type SettingsTab = (typeof SETTINGS_TABS)[number]['key'];

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

  // 本地算力 · 省钱账本（2026-09-08 用户拍板：保守口径只算 AI 输出侧）
  // 局域网访问令牌（批2-1 2026-09-18）
  const [lanInfo, setLanInfo] = useState<{ loading: boolean; enabled: boolean; token: string }>(
    { loading: true, enabled: false, token: '' });
  const [lanTokenDraft, setLanTokenDraft] = useState('');
  const [lanSaved, setLanSaved] = useState(false);
  useEffect(() => {
    let alive = true;
    systemApi.getLanTokenInfo().then((info) => {
      if (alive) setLanInfo({ loading: false, enabled: info.enabled, token: info.token });
    }).catch(() => { if (alive) setLanInfo({ loading: false, enabled: false, token: '' }); });
    return () => { alive = false; };
  }, []);
  // P2 设置页 tab 化（2026-09-17 用户拍板 4A）：11 区块归 6 签终结长滚动；
  // 未选中的区块不挂载（其数据拉取随挂载发生）
  // 批3 P5（2026-09-19）：签支持 URL 深链（?tab=ai 直达 AI 服务签——
  // 创作页「云端」徽标跳转用）；切签回写 URL 防刷新丢位
  const [searchParams, setSearchParams] = useSearchParams();
  const initialTab = searchParams.get('tab');
  const [settingsTab, setSettingsTab] = useState<SettingsTab>(
    SETTINGS_TABS.some((t) => t.key === initialTab) ? (initialTab as SettingsTab) : 'general');
  const [savings, setSavings] = useState<systemApi.LocalSavings | null>(null);
  const [savingsLoaded, setSavingsLoaded] = useState(false);

  useEffect(() => {
    let alive = true;
    systemApi.getLocalSavings()
      .then((res) => { if (alive) setSavings(res); })
      .catch(() => { /* 后端旧版本无此端点：账本区显示待更新提示 */ })
      .finally(() => { if (alive) setSavingsLoaded(true); });
    return () => { alive = false; };
  }, []);

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

  // 将设置对象转为可展示的键值对（远程推理旧键已迁入「云端 API 服务」卡片，不进通用表）
  const REMOTE_SETTING_KEYS = new Set([
    'remote_dialog_enabled',
    'remote_dialog_base_url',
    'remote_dialog_api_key',
    'remote_dialog_model',
    'remote_dialog_timeout_s',
  ]);
  const settingEntries = Object.entries(editableSettings)
    .filter(([key]) => !REMOTE_SETTING_KEYS.has(key));
  // B2（2026-09-13）：删除幽灵键 knownKeys（language/auto_save_interval/
  // default_model/ws_reconnect_interval——后端无这些字段，PUT 被 Pydantic
  // 丢弃，纯前端死码）；后端三个白写设置字段已同步删除。

  return (
    <div className="page settings-page">
      <ModuleGuide moduleKey="settings" />
      <h1 className="page-title"><SettingsIcon size={20} aria-hidden="true" /> 设置</h1>
      <p className="page-subtitle">通用 · 外观 · AI 服务 · 数据 · 插件 · 维护</p>

      <div className="settings-tabs" role="tablist" aria-label="设置分区">
        {SETTINGS_TABS.map((t) => (
          <button
            key={t.key}
            type="button"
            role="tab"
            aria-selected={settingsTab === t.key}
            className={`training-tab${settingsTab === t.key ? ' active' : ''}`}
            onClick={() => {
              setSettingsTab(t.key);
              // 批3 P5：深链回写（?tab=xx）——刷新保持 + 创作页跳转锚点
              setSearchParams(t.key === 'general' ? {} : { tab: t.key },
                              { replace: true });
            }}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* P2 tab 化：各签独立挂载（通用=全局+互斥+硬件） */}
      {settingsTab === 'appearance' && (
        <>
      {/* ============ 外观设置 ============ */}
      <div className="settings-section card">
        <h2 className="settings-section-title">外观</h2>

        {/* 主题（双主题体系 × 亮暗双模式） */}
        <div className="settings-row" style={{ alignItems: 'stretch', flexDirection: 'column', gap: 'var(--space-3)' }}>
          <div className="settings-row-label">
            <span className="settings-row-name">主题</span>
            <span className="settings-row-desc">三主题体系：Sakura 樱花系列 / Nebula 星云科技系列 / Dali 风花雪月系列，各含亮暗双模式</span>
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
        </>
      )}

      {settingsTab === 'general' && (
        <>
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
                  {key === 'ui_performance' ? (
                    /* 界面效果（2026-09-08 UI 降载方案②）：性能模式关
                       动态粒子/流光/毛玻璃，即时生效（AI 生成期间更稳） */
                    <div className="flex items-center gap-3">
                      <label className="flex items-center gap-1.5 cursor-pointer">
                        <input
                          type="radio"
                          name="ui_performance"
                          checked={String(value ?? 'full') === 'full'}
                          onChange={() =>
                            setEditableSettings((prev) => ({
                              ...prev,
                              ui_performance: 'full',
                            }))}
                        />
                        完整效果
                      </label>
                      <label className="flex items-center gap-1.5 cursor-pointer">
                        <input
                          type="radio"
                          name="ui_performance"
                          checked={String(value ?? '') === 'lite'}
                          onChange={() =>
                            setEditableSettings((prev) => ({
                              ...prev,
                              ui_performance: 'lite',
                            }))}
                        />
                        性能模式（关动态特效）
                      </label>
                    </div>
                  ) : key === 'launch_mode' ? (
                    /* 界面打开方式（2026-09-08 桌面壳接线）：专用单选，
                       下次启动生效（本次会话窗口不热切） */
                    <div className="flex items-center gap-3">
                      <label className="flex items-center gap-1.5 cursor-pointer">
                        <input
                          type="radio"
                          name="launch_mode"
                          checked={String(value ?? 'shell') === 'shell'}
                          onChange={() =>
                            setEditableSettings((prev) => ({
                              ...prev,
                              launch_mode: 'shell',
                            }))}
                        />
                        桌面窗口（推荐）
                      </label>
                      <label className="flex items-center gap-1.5 cursor-pointer">
                        <input
                          type="radio"
                          name="launch_mode"
                          checked={String(value ?? '') === 'browser'}
                          onChange={() =>
                            setEditableSettings((prev) => ({
                              ...prev,
                              launch_mode: 'browser',
                            }))}
                        />
                        系统浏览器
                      </label>
                      <span className="text-xs text-[var(--color-text-tertiary)]">
                        下次启动生效
                      </span>
                    </div>
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

      {/* ============ 局域网访问（批2-1 2026-09-18）============ */}
      <div className="settings-section card">
        <div className="settings-section-header">
          <h2 className="settings-section-title">局域网访问</h2>
        </div>
        <p className="settings-section-desc">
          仅当后端以局域网模式（非 127.0.0.1 绑定）启动时生效：远程设备需
          输入访问令牌。本机访问不受影响。
        </p>
        {lanInfo.loading ? (
          <div className="settings-loading">读取中…</div>
        ) : !lanInfo.enabled ? (
          <p className="text-sm" style={{ color: 'var(--color-text-tertiary)' }}>
            当前为纯本机模式（回环绑定），无需令牌。
          </p>
        ) : (
          <>
            <div className="flex items-center gap-2 mb-2">
              <code
                className="text-sm"
                style={{
                  padding: '4px 10px',
                  background: 'var(--color-bg)',
                  borderRadius: 'var(--radius-sm)',
                  userSelect: 'all',
                }}
              >
                {lanInfo.token}
              </code>
              <button type="button" className="btn btn-secondary btn-sm"
                onClick={() => void navigator.clipboard?.writeText(lanInfo.token || '')}>
                复制
              </button>
            </div>
            <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
              把令牌输入到远程设备本应用的「设置 → 局域网访问」即完成配对。
            </p>
          </>
        )}
        <div className="flex items-center gap-2 mt-3">
          <input
            className="input"
            style={{ maxWidth: 340 }}
            placeholder="远程设备：在此粘贴服务器令牌"
            value={lanTokenDraft}
            maxLength={64}
            onChange={(e) => setLanTokenDraft(e.target.value)}
          />
          <button type="button" className="btn btn-primary btn-sm"
            onClick={() => { setLanToken(lanTokenDraft.trim()); setLanSaved(true); }}>
            保存到本机
          </button>
          {lanSaved && (
            <span className="text-xs" style={{ color: 'var(--color-success)' }}>已保存</span>
          )}
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
        </>
      )}

      {settingsTab === 'ai' && (
        <>
      {/* ============ 云端 API 服务（批1：用户自带 Key，多服务商） ============ */}
      <CloudApiSettings />

      {/* ============ 联网搜索（架构升级计划 B-阶段一，默认关） ============ */}
      <WebSearchSettings />
        </>
      )}

      {settingsTab === 'data' && (
        <>
      {/* ============ 批4 P11：存储清理中心（盘点+白名单清理） ============ */}
      <StorageCard />

      {/* ============ 数据管理（备份/恢复/项目导入导出，接线四项 2026-09-17） ============ */}
      <DataManagementSection />
      <div className="settings-section card">
        <div className="settings-section-header">
          <h2 className="settings-section-title">本地算力 · 省钱账本</h2>
        </div>
        <p className="settings-section-desc">
          使用本地 GPU 生成 = 不消耗云端 token = 直接省钱。
          以下为本地产出统计，金额按云端参考价估算（保守口径，宁少报不多报）。
        </p>
        {!savingsLoaded ? (
          <div className="settings-loading">统计中…</div>
        ) : !savings ? (
          <div className="settings-empty">
            <p>账本数据暂不可用：重启后端后此处会显示本地算力统计。</p>
          </div>
        ) : (
          <>
            <div className="settings-hardware-grid">
              <div className="settings-hw-item">
                <span className="settings-hw-label">本地生成文本</span>
                <span className="settings-hw-value">
                  {savings.text.tokens_est >= 10000
                    ? `${(savings.text.tokens_est / 10000).toFixed(1)} 万 tokens`
                    : `${savings.text.tokens_est} tokens`}
                </span>
              </div>
              <div className="settings-hw-item">
                <span className="settings-hw-label">文本回复</span>
                <span className="settings-hw-value">{savings.text.messages} 条</span>
              </div>
              <div className="settings-hw-item">
                <span className="settings-hw-label">本地生图</span>
                <span className="settings-hw-value">
                  {savings.images.count} 张
                  <span className="text-xs text-[var(--color-text-tertiary)]">
                    （关键帧 {savings.images.keyframes} / 漫画资产 {savings.images.comic_assets} / 绘画 {savings.images.paint}）
                  </span>
                </span>
              </div>
              <div className="settings-hw-item">
                <span className="settings-hw-label">本地视频</span>
                <span className="settings-hw-value">{savings.videos.count} 条</span>
              </div>
              <div className="settings-hw-item">
                <span className="settings-hw-label">累计省约</span>
                <span className="settings-hw-value" style={{ color: 'var(--color-success)' }}>
                  ¥{savings.money.cny_est.toFixed(2)}
                </span>
              </div>
            </div>
            <p className="text-xs text-[var(--color-text-tertiary)] mt-2">
              {savings.scope_note}；参考价：文本 ¥{savings.money.prices.text_cny_per_mtok}/百万 tokens、
              图 ¥{savings.money.prices.image_cny_each}/张、视频 ¥{savings.money.prices.video_cny_each}/条。
            </p>
          </>
        )}
      </div>

        </>
      )}

      {settingsTab === 'plugins' && (
        <>
      {/* ============ 插件（2026-09-16 拍板：用户导入/启停/删除） ============ */}
      <PluginSection />
        </>
      )}

      {settingsTab === 'maintenance' && (
        <>
      {/* ============ 批4 P19：硬件健康中心（四仪表+保护动作） ============ */}
      <HardwareHealthCard />

      {/* ============ 软件升级（升级机制批2） ============ */}
      <UpgradeSection />

      {/* ============ 批5 P18：操作审计（删除/导出/设置/云端留痕） ============ */}
      <AuditCard />

      {/* ============ 体检与修复（自愈批4） ============ */}
      <HealthCheckCard />
        </>
      )}

    </div>
  );
}

/* ============================== 辅助函数 ============================== */

/** 设置键中文映射（B2：清退四个幽灵键——后端无对应字段） */
const SETTING_KEY_LABELS: Record<string, string> = {
  launch_mode: '界面打开方式',
  ui_performance: '界面效果',
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
