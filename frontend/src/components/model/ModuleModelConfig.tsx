/* ==========================================================================
 * ModuleModelConfig.tsx —— 功能模块模型配置（2026-08-23；2026-08-27 增管控）
 * --------------------------------------------------------------------------
 * 模型管理页顶部 section：集中展示 / 设置三大功能模块各自使用的大模型，
 * 并提供模型管理层面的「模块级选型配置」（可选范围白名单 + 默认模型）。
 *
 * 各模块"真源"（设置必须写入功能真实消费的链路，不做摆设）：
 *   AI 对话  → useDialogStore.modelId（localStorage 持久化）：
 *              DialogPage prewarm + 发消息 WS 均消费此值，切换即全局生效；
 *              可选清单来自 GET /dialog/models（含本机显存可承载判定）。
 *   AI 绘画  → localStorage「omnispace.paint.modelPreference」共享偏好：
 *              auto = 后端三级智能路由（qwen-image → flux2 → SDXL 兜底），
 *              manual = 显式点名模型（后端尊重用户意图不路由）；
 *              PaintView 挂载读初值、切换写回，与本页双向同步。
 *   漫剧创作 → 三模型流水线，同源读写「omnispace.manga.modelConfig」
 *              （漫剧编辑器「模型配置」弹窗同一 localStorage，双向同步）：
 *              文字部分（剧本/分镜文案）+ 图片部分（角色/分镜生图）
 *              + 视频生成（默认 Wan2.2-TI2V-5B 统一权重）；
 *              候选清单来自 GET /manga/models/available?task_type=…，
 *              留空 = 系统默认（后端自动选择）。
 *
 * 模块级选型配置（2026-08-27，服务端真源）：
 *   GET/PUT /models/module-config（system_settings kv 持久化）：
 *   - allowed 白名单：业务侧清单 API（/dialog/models、/draw/models、
 *     /manga/models/available）按此过滤——模型选择 UI 只见范围内模型，
 *     生成链路点名范围外模型被如实拒绝；空 = 全量开放（兼容存量）。
 *   - default 默认模型：模块「系统默认」时实际调用的模型；对话 WS/
 *     prewarm 与绘画生成链路在未显式指定时优先采用。
 * ========================================================================== */

import { useEffect, useState } from 'react';
import {
  Check,
  Clapperboard,
  Image as ImageIcon,
  Loader2,
  MessageSquare,
  RotateCcw,
  Settings2,
  SlidersHorizontal,
} from 'lucide-react';
import { useDialogStore } from '@/stores/useDialogStore';
import { useAppStore } from '@/stores/useAppStore';
import {
  listModels,
  type DrawModelItem,
} from '@/services/paintApi';
import { reportActionError } from '@/utils/errors';
import {
  getDialogModels,
  getDialogEngineStatus,
  prewarmModel,
  type DialogModelInfo,
} from '@/services/dialogApi';
import { listAvailableModels } from '@/services/mangaApi';
import {
  getModuleModelConfig,
  saveModuleModelConfig,
  type ModuleModelSlotInfo,
  type ModuleSlotConfig,
} from '@/services/modelApi';
import { MODEL_AVAILABLE_STATUS_LABELS } from '@/constants/statusLabels';
import {
  loadModelConfig,
  writeModelConfig,
  type MangaModelConfig,
} from '@/constants/modelConfig';
import type { AvailableModel } from '@/types';

/* ------------------------------ 绘画共享偏好 ------------------------------ */

/* B7 步1a（2026-09-14）：原 localStorage「omnispace.paint.modelPreference」
 * 死键及读写器整体删除——PaintView 清退后该键零外部消费（写了自己存的
 * 摆设），绘画选型真源收敛为 module-config 的 paint 槽 default
 * （''=智能路由），与本页对话/漫剧槽位同型。 */

/* ------------------------------ 常量 ------------------------------ */

/** select 36px 表单控件高度（4px 栅格） */
const SELECT_STYLE: React.CSSProperties = {
  height: 36,
  padding: '0 28px 0 10px',
  borderRadius: 'var(--radius-sm)',
  border: '1px solid var(--color-input-border)',
  background: 'var(--color-input-bg)',
  color: 'var(--color-text-primary)',
  fontSize: 'var(--font-size-sm)',
};

/* ------------------------------ 组件 ------------------------------ */

/** 行布局：图标 + 模块名 + 当前模型信息 + 右侧选择器 */
function moduleRowStyle(): React.CSSProperties {
  return {
    display: 'flex',
    alignItems: 'center',
    gap: 'var(--space-3)',
    padding: 'var(--space-3) 0',
  };
}

/** 行 wrapper（行 + 可选的展开配置面板；分隔线在 wrapper 上） */
function moduleRowWrapStyle(): React.CSSProperties {
  return {
    borderBottom: '1px solid var(--color-divider)',
  };
}

/** 范围配置入口按钮（icon-only，32px 与 select 同高） */
const SCOPE_BTN_STYLE: React.CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  justifyContent: 'center',
  width: 36,
  height: 36,
  borderRadius: 'var(--radius-sm)',
  border: '1px solid var(--color-input-border)',
  background: 'var(--color-input-bg)',
  color: 'var(--color-text-secondary)',
  cursor: 'pointer',
  flexShrink: 0,
};

/* ------------------------ 模块级选型配置面板 ------------------------ */

/** 面板内部：候选勾选项（checkbox + 名称 + 状态标签） */
function ScopeCandidateItem({
  id,
  name,
  downloaded,
  checked,
  unknown,
  onToggle,
}: {
  id: string;
  name: string;
  downloaded: boolean;
  checked: boolean;
  unknown?: boolean;
  onToggle: (id: string) => void;
}) {
  return (
    <label
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 'var(--space-2)',
        padding: '6px 8px',
        borderRadius: 'var(--radius-sm)',
        border: `1px solid ${checked ? 'var(--color-primary)' : 'var(--color-divider)'}`,
        background: checked ? 'var(--color-primary-bg, rgba(59, 130, 246, 0.10))' : 'transparent',
        cursor: 'pointer',
        fontSize: 'var(--font-size-xs)',
        color: 'var(--color-text-primary)',
        userSelect: 'none',
      }}
    >
      <input
        type="checkbox"
        checked={checked}
        onChange={() => onToggle(id)}
        aria-label={`勾选 ${name}`}
        style={{ accentColor: 'var(--color-primary)', margin: 0 }}
      />
      <span style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
        {name}
      </span>
      {unknown ? (
        <span style={{ color: 'var(--color-text-tertiary)', flexShrink: 0 }}>未知</span>
      ) : downloaded ? (
        <span style={{ color: 'var(--color-text-tertiary)', flexShrink: 0 }}>已下载</span>
      ) : (
        <span style={{ color: 'var(--color-text-tertiary)', opacity: 0.6, flexShrink: 0 }}>未安装</span>
      )}
    </label>
  );
}

/** 模块级选型配置面板：候选白名单勾选 + 默认模型选择 + 保存 */
const ModuleScopePanel: React.FC<{
  slotInfo: ModuleModelSlotInfo;
  draft: ModuleSlotConfig;
  saving: boolean;
  onDraftChange: (draft: ModuleSlotConfig) => void;
  onSave: () => void;
  onCancel: () => void;
}> = ({ slotInfo, draft, saving, onDraftChange, onSave, onCancel }) => {
  const toggle = (id: string) => {
    const set = new Set(draft.allowed);
    if (set.has(id)) set.delete(id);
    else set.add(id);
    const allowed = slotInfo.candidates.map((c) => c.id).filter((cid) => set.has(cid));
    // 未知项（已保存但不在候选集）保留在尾部
    const unknowns = slotInfo.unknown_allowed.filter((u) => set.has(u));
    const next: ModuleSlotConfig = {
      allowed: [...allowed, ...unknowns],
      default: draft.default && set.has(draft.default) ? draft.default : '',
    };
    onDraftChange(next);
  };

  const allIds = [...slotInfo.candidates.map((c) => c.id), ...slotInfo.unknown_allowed];
  const allChecked = allIds.length > 0 && allIds.every((id) => draft.allowed.includes(id));

  return (
    <div
      style={{
        margin: 'var(--space-2) 0 var(--space-3)',
        marginLeft: 44,
        padding: 'var(--space-3)',
        borderRadius: 'var(--radius-md)',
        border: '1px solid var(--color-divider)',
        background: 'var(--color-input-bg)',
      }}
      aria-label={`${slotInfo.label} 可选范围配置`}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', marginBottom: 'var(--space-2)' }}>
        <span style={{ fontSize: 'var(--font-size-xs)', color: 'var(--color-text-secondary)', flex: 1 }}>
          勾选该模块可选用的模型（不勾 = 全量开放）；已限制时业务页下拉只显示范围内模型
        </span>
        <button
          type="button"
          onClick={() =>
            onDraftChange(
              allChecked
                ? { allowed: [], default: '' }
                : { allowed: allIds, default: draft.default },
            )
          }
          style={{
            ...SCOPE_BTN_STYLE,
            width: 'auto',
            padding: '0 10px',
            fontSize: 'var(--font-size-xs)',
            color: 'var(--color-text-secondary)',
          }}
        >
          {allChecked ? '清空（全量开放）' : '全选'}
        </button>
      </div>
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fill, minmax(240px, 1fr))',
          gap: 'var(--space-2)',
          maxHeight: 216,
          overflowY: 'auto',
          padding: '2px',
        }}
      >
        {slotInfo.candidates.map((c) => (
          <ScopeCandidateItem
            key={c.id}
            id={c.id}
            name={c.name}
            downloaded={c.downloaded}
            checked={draft.allowed.includes(c.id)}
            onToggle={toggle}
          />
        ))}
        {slotInfo.unknown_allowed.map((u) => (
          <ScopeCandidateItem
            key={u}
            id={u}
            name={u}
            downloaded={false}
            unknown
            checked={draft.allowed.includes(u)}
            onToggle={toggle}
          />
        ))}
        {slotInfo.candidates.length === 0 && slotInfo.unknown_allowed.length === 0 && (
          <span style={{ fontSize: 'var(--font-size-xs)', color: 'var(--color-text-tertiary)' }}>
            暂无候选模型（该类别模型均未注册/未下载）
          </span>
        )}
      </div>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 'var(--space-3)',
          marginTop: 'var(--space-3)',
          flexWrap: 'wrap',
        }}
      >
        <span style={{ fontSize: 'var(--font-size-xs)', color: 'var(--color-text-secondary)' }}>
          默认模型（该模块「系统默认」时实际调用）
        </span>
        <select
          aria-label={`${slotInfo.label}默认模型`}
          value={draft.default}
          onChange={(e) => onDraftChange({ ...draft, default: e.target.value })}
          style={{ ...SELECT_STYLE, flex: 1, maxWidth: 320 }}
        >
          <option value="">系统默认（后端自动选择）</option>
          {draft.allowed.map((id) => (
            <option key={id} value={id}>
              {slotInfo.candidates.find((c) => c.id === id)?.name || id}
            </option>
          ))}
        </select>
        <div style={{ display: 'flex', gap: 'var(--space-2)', marginLeft: 'auto' }}>
          <button
            type="button"
            onClick={onCancel}
            disabled={saving}
            style={{ ...SCOPE_BTN_STYLE, width: 'auto', padding: '0 12px', fontSize: 'var(--font-size-xs)' }}
          >
            <RotateCcw size={12} aria-hidden="true" /> 取消
          </button>
          <button
            type="button"
            onClick={onSave}
            disabled={saving || draft.default === slotInfo.default && arraysEqual(draft.allowed, slotInfo.allowed)}
            style={{
              ...SCOPE_BTN_STYLE,
              width: 'auto',
              padding: '0 12px',
              fontSize: 'var(--font-size-xs)',
              border: '1px solid var(--color-primary)',
              background: 'var(--color-primary)',
              color: 'var(--color-on-primary)',
              opacity: saving ? 0.6 : 1,
            }}
          >
            {saving ? <Loader2 size={12} className="animate-spin" aria-hidden="true" /> : <Check size={12} aria-hidden="true" />}
            保存配置
          </button>
        </div>
      </div>
    </div>
  );
};

/** 浅比较两个字符串数组（保序） */
function arraysEqual(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((v, i) => v === b[i]);
}

export const ModuleModelConfig: React.FC = () => {
  const showToast = useAppStore((s) => s.showToast);

  /* ---------- AI 对话 ---------- */
  const dialogModelId = useDialogStore((s) => s.modelId);
  const setModelId = useDialogStore((s) => s.setModelId);
  const [dialogModels, setDialogModels] = useState<DialogModelInfo[]>([]);
  const [dialogEngineModel, setDialogEngineModel] = useState<string>('');
  const [dialogEngineReady, setDialogEngineReady] = useState(false);
  const [dialogSwitching, setDialogSwitching] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const res = await getDialogModels();
        if (!cancelled) setDialogModels(res.models || []);
      } catch {
        /* silent-intent: 后端未就绪：清单空，下拉仅显示当前值 */
      }
      try {
        const st = await getDialogEngineStatus();
        if (!cancelled) {
          setDialogEngineModel(String(st?.model || ''));
          setDialogEngineReady(st?.state === 'ready');
        }
      } catch {
        /* silent-intent: 引擎状态可选 */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  /** 切换对话模型：写真源 + 立即后台预热新模型（冷启动弹窗由 App 兜底） */
  const handleDialogChange = async (modelId: string) => {
    if (modelId === dialogModelId) return;
    const target = dialogModels.find((m) => m.model_id === modelId);
    setModelId(modelId);
    showToast(`对话模型已切换为「${target?.name || modelId}」，正在后台预热`, 'success');
    if (!target?.loaded) {
      setDialogSwitching(true);
      try {
        await prewarmModel(modelId);
      } catch {
        /* silent-intent: 预热失败静默：进入对话页时 prewarm 链路会重试 */
      } finally {
        setDialogSwitching(false);
      }
    }
  };

  /* ---------- AI 绘画 ---------- */
  // W3-C 步5：去 usePaintStore 化（旧绘画 store 已清退）——模型列表
  // 本地直调 /draw/models。B7 步1a：选型真源=module-config paint 槽
  // default（''=智能路由），原 localStorage 死键已删。
  const [paintModels, setPaintModels] = useState<DrawModelItem[]>([]);

  useEffect(() => {
    let cancelled = false;
    void listModels()
      .then((res) => {
        if (!cancelled) setPaintModels(res.items || []);
      })
      .catch((err: unknown) => reportActionError(err, '读取绘画模型列表'));
    return () => {
      cancelled = true;
    };
  }, []);

  /** 本地就绪的可选绘画模型（/draw/models status === ready） */
  const readyPaintModels = paintModels.filter((m) => m.status === 'ready');
  // B7 步1a：paint 选型逻辑移至 scopeSlots 段之后（依赖 slotBy，TDZ）

  /* ---------- 漫剧创作（文字/图片/视频 三模型流水线） ---------- */
  // 口径标注（B7 步1b）：mangaCfg（localStorage 用户最近一次会话选择，
  // 含画幅/时长）与 scopeSlots（module-config 管理级范围/默认）是**两层
  // 合法语义**非双写——前者=用户偏好，后者=管理员配置；三模型槽值超出
  // 范围时由后端 /manga/models/available 过滤兜底。
  const [mangaCfg, setMangaCfg] = useState<MangaModelConfig>(() => loadModelConfig());
  const [mangaOptions, setMangaOptions] = useState<
    Record<'dialog' | 'paint' | 'video', AvailableModel[]>
  >({ dialog: [], paint: [], video: [] });

  // 候选清单：与漫剧编辑器「模型配置」弹窗同接口（/manga/models/available）
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const [d, p, v] = await Promise.all([
          listAvailableModels('dialog'),
          listAvailableModels('paint'),
          listAvailableModels('video'),
        ]);
        if (!cancelled) {
          setMangaOptions({
            dialog: d.items || [],
            paint: p.items || [],
            video: v.items || [],
          });
        }
      } catch {
        /* silent-intent: 后端未就绪：下拉仅显示当前保存值 */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  /** 漫剧某环节换模型：同源写 localStorage（编辑器弹窗/生成确认弹窗即时生效） */
  const handleMangaChange = (
    field: 'dialogModel' | 'paintModel' | 'videoModel',
    value: string,
  ) => {
    const next = { ...mangaCfg, [field]: value };
    setMangaCfg(next);
    writeModelConfig(next);
    const label = field === 'dialogModel' ? '文字' : field === 'paintModel' ? '图片' : '视频';
    if (!value) {
      showToast(`漫剧${label}模型已切回系统默认（自动选择）`, 'success');
      return;
    }
    const task = field === 'dialogModel' ? 'dialog' : field === 'paintModel' ? 'paint' : 'video';
    const name = mangaOptions[task].find((m) => m.id === value)?.name || value;
    showToast(`漫剧${label}模型已切换为「${name}」`, 'success');
  };

  /* ---------- 模块级选型配置（可选范围白名单 + 默认模型） ---------- */
  const [scopeSlots, setScopeSlots] = useState<ModuleModelSlotInfo[]>([]);
  const [scopeDrafts, setScopeDrafts] = useState<Record<string, ModuleSlotConfig>>({});
  const [expandedSlot, setExpandedSlot] = useState<string | null>(null);
  const [savingSlot, setSavingSlot] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const res = await getModuleModelConfig();
        if (!cancelled) setScopeSlots(res.slots || []);
      } catch {
        /* silent-intent: 后端未就绪：范围按钮不渲染（行内现状不受影响） */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const slotBy = (slot: string) => scopeSlots.find((s) => s.slot === slot);

  const openScope = (slot: string) => {
    const info = slotBy(slot);
    if (!info) return;
    setScopeDrafts((prev) => ({
      ...prev,
      [slot]: { allowed: [...info.allowed], default: info.default },
    }));
    setExpandedSlot((prev) => (prev === slot ? null : slot));
  };

  const saveScope = async (slot: string) => {
    const draft = scopeDrafts[slot];
    if (!draft) return;
    setSavingSlot(slot);
    try {
      const res = await saveModuleModelConfig({ [slot]: draft });
      const info = (res.slots || []).find((s) => s.slot === slot);
      if (info) {
        setScopeSlots((prev) => prev.map((s) => (s.slot === slot ? info : s)));
      }
      if (res.warnings && res.warnings.length > 0) {
        showToast(`已保存（提示：${res.warnings[0]}）`, 'info');
      } else {
        showToast(`${slotBy(slot)?.label || '模块'}选型配置已保存`, 'success');
      }
      setExpandedSlot(null);
    } catch (err) {
      showToast(
        `保存失败：${err instanceof Error ? err.message : String(err)}`,
        'error',
      );
    } finally {
      setSavingSlot(null);
    }
  };

  /* ---------- AI 绘画选型（B7 步1a：真源=module-config paint 槽）---------- */
  const paintSlot = slotBy('paint');
  const paintDefault = paintSlot?.default || '';
  const [paintSaving, setPaintSaving] = useState(false);

  /** 切换绘画选型：写 module-config paint 槽（服务端真源，''=智能路由） */
  const handlePaintChange = async (value: string) => {
    const info = slotBy('paint');
    if (!info || paintSaving) return;
    setPaintSaving(true);
    try {
      const nextDefault = value === '__auto__' ? '' : value;
      const res = await saveModuleModelConfig({
        paint: { allowed: [...info.allowed], default: nextDefault },
      });
      const info2 = (res.slots || []).find((s) => s.slot === 'paint');
      if (info2) {
        setScopeSlots((prev) =>
          prev.map((s) => (s.slot === 'paint' ? info2 : s)));
      }
      showToast(
        nextDefault === ''
          ? '绘画已切回智能路由（自动挑选最合适的模型）'
          : `绘画模型已指定为「${readyPaintModels.find((m) => m.id === nextDefault)?.name || nextDefault}」`,
        'success',
      );
    } catch (err: unknown) {
      reportActionError(err, '保存绘画选型');
    } finally {
      setPaintSaving(false);
    }
  };

  /** 范围入口按钮（slot 未就绪时返回 null）——行内 flex 容器使用 */
  const renderScopeBtn = (slot: string, compact = false): React.ReactNode => {
    const info = slotBy(slot);
    if (!info) return null;
    return (
      <button
        type="button"
        aria-label={`配置${info.label}可选范围`}
        title={`可选范围${info.restricted ? `（已限制 ${info.allowed.length} 个）` : '（全量开放）'}与默认模型`}
        onClick={() => openScope(slot)}
        style={{
          ...SCOPE_BTN_STYLE,
          ...(compact ? { width: 28, height: 28 } : null),
          ...(info.restricted
            ? { color: 'var(--color-primary)', borderColor: 'var(--color-primary)' }
            : null),
        }}
      >
        <SlidersHorizontal size={compact ? 13 : 15} aria-hidden="true" />
      </button>
    );
  };

  /** 展开的配置面板（未展开/草稿缺失时返回 null）——行外 wrapper 使用 */
  const renderScopePanel = (slot: string): React.ReactNode => {
    const info = slotBy(slot);
    const draft = scopeDrafts[slot];
    if (!info || expandedSlot !== slot || !draft) return null;
    return (
      <ModuleScopePanel
        slotInfo={info}
        draft={draft}
        saving={savingSlot === slot}
        onDraftChange={(d) => setScopeDrafts((prev) => ({ ...prev, [slot]: d }))}
        onSave={() => void saveScope(slot)}
        onCancel={() => setExpandedSlot(null)}
      />
    );
  };

  /* ------------------------------ 渲染 ------------------------------ */

  return (
    <section className="mm-reco" aria-label="功能模块模型配置">
      <div className="mm-reco-head">
        <span className="mm-reco-title">
          <Settings2 size={15} aria-hidden="true" /> 功能模块模型配置
        </span>
        <span className="mm-reco-gpu">
          各模块使用的大模型在此集中查看与切换；「范围」按钮配置各模块可选模型与默认模型
        </span>
      </div>

      {/* ---------- AI 对话 ---------- */}
      <div style={moduleRowWrapStyle()}>
        <div style={moduleRowStyle()}>
        <span
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            justifyContent: 'center',
            width: 32,
            height: 32,
            borderRadius: 'var(--radius-sm)',
            background: 'var(--color-primary-bg, rgba(59, 130, 246, 0.12))',
            color: 'var(--color-primary)',
            flexShrink: 0,
          }}
        >
          <MessageSquare size={16} aria-hidden="true" />
        </span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 'var(--font-size-sm)', fontWeight: 600 }}>
            AI 对话
            <span style={{ fontWeight: 400, color: 'var(--color-text-tertiary)', marginLeft: 'var(--space-2)' }}>
              文字 + 图片理解、深度思考问答
            </span>
          </div>
          <div style={{ fontSize: 'var(--font-size-xs)', color: 'var(--color-text-secondary)', marginTop: 2, display: 'flex', alignItems: 'center', gap: 'var(--space-2)', flexWrap: 'wrap' }}>
            <span>
              已选择：{dialogModels.find((m) => m.model_id === dialogModelId)?.name || dialogModelId || '—'}
            </span>
            {dialogEngineReady ? (
              <span className="model-status-badge ready" title={`引擎当前加载：${dialogEngineModel}`}>
                <Check size={10} aria-hidden="true" style={{ verticalAlign: -1 }} /> 引擎已加载
                {dialogEngineModel && dialogEngineModel !== dialogModelId
                  ? `（${dialogEngineModel}）`
                  : ''}
              </span>
            ) : (
              <span className="model-status-badge" style={{ background: 'var(--color-input-bg)', color: 'var(--color-text-secondary)' }}>
                未加载
              </span>
            )}
          </div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', flexShrink: 0 }}>
          {dialogSwitching && <Loader2 size={14} className="animate-spin" aria-hidden="true" style={{ color: 'var(--color-primary)' }} />}
          <select
            aria-label="选择对话模型"
            value={dialogModelId}
            onChange={(e) => void handleDialogChange(e.target.value)}
            style={SELECT_STYLE}
          >
            {/* 当前值不在清单（后端未就绪）时兜底显示 */}
            {dialogModelId && !dialogModels.some((m) => m.model_id === dialogModelId) && (
              <option value={dialogModelId}>{dialogModelId}</option>
            )}
            {dialogModels.map((m) => (
              <option key={m.model_id} value={m.model_id} disabled={!m.fits_local}>
                {m.name}
                {m.loaded ? '（已加载）' : ''}
                {!m.fits_local ? '（超本机显存）' : ''}
              </option>
            ))}
          </select>
          {renderScopeBtn('dialog')}
        </div>
        </div>
        {renderScopePanel('dialog')}
      </div>

      {/* ---------- AI 绘画 ---------- */}
      <div style={moduleRowWrapStyle()}>
        <div style={moduleRowStyle()}>
        <span
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            justifyContent: 'center',
            width: 32,
            height: 32,
            borderRadius: 'var(--radius-sm)',
            background: 'var(--color-primary-bg, rgba(59, 130, 246, 0.12))',
            color: 'var(--color-primary)',
            flexShrink: 0,
          }}
        >
          <ImageIcon size={16} aria-hidden="true" />
        </span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 'var(--font-size-sm)', fontWeight: 600 }}>
            AI 绘画
            <span style={{ fontWeight: 400, color: 'var(--color-text-tertiary)', marginLeft: 'var(--space-2)' }}>
              文生图 / 图生图
            </span>
          </div>
          <div style={{ fontSize: 'var(--font-size-xs)', color: 'var(--color-text-secondary)', marginTop: 2 }}>
            {paintDefault === '' ? (
              <>智能路由：qwen-image（中文最优）→ FLUX.2 → SDXL 兜底，按资源自动挑选</>
            ) : (
              <>已指定：{readyPaintModels.find((m) => m.id === paintDefault)?.name || paintDefault}</>
            )}
          </div>
        </div>
        <select
          aria-label="选择绘画模型"
          value={paintDefault === '' ? '__auto__' : paintDefault}
          onChange={(e) => void handlePaintChange(e.target.value)}
          disabled={paintSaving}
          style={SELECT_STYLE}
        >
          <option value="__auto__">智能路由（推荐）</option>
          {readyPaintModels.map((m) => (
            <option key={m.id} value={m.id}>
              {m.name}
            </option>
          ))}
        </select>
        {renderScopeBtn('paint')}
        </div>
        {renderScopePanel('paint')}
      </div>

      {/* ---------- 漫剧创作（文字/图片/视频 三模型流水线） ---------- */}
      <div style={{ padding: 'var(--space-3) 0' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-3)' }}>
          <span
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              justifyContent: 'center',
              width: 32,
              height: 32,
              borderRadius: 'var(--radius-sm)',
              background: 'var(--color-primary-bg, rgba(59, 130, 246, 0.12))',
              color: 'var(--color-primary)',
              flexShrink: 0,
            }}
          >
            <Clapperboard size={16} aria-hidden="true" />
          </span>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontSize: 'var(--font-size-sm)', fontWeight: 600 }}>
              漫剧创作
              <span style={{ fontWeight: 400, color: 'var(--color-text-tertiary)', marginLeft: 'var(--space-2)' }}>
                文字 → 图片 → 视频生成，三模型流水线
              </span>
            </div>
            <div style={{ fontSize: 'var(--font-size-xs)', color: 'var(--color-text-secondary)', marginTop: 2 }}>
              与漫剧编辑器「模型配置」弹窗同源同步；留空 = 系统默认（自动选择）
            </div>
          </div>
        </div>
        <div
          style={{
            display: 'flex',
            flexDirection: 'column',
            gap: 'var(--space-2)',
            marginTop: 'var(--space-3)',
            marginLeft: 44,
          }}
        >
          {([
            { field: 'dialogModel', task: 'dialog', slot: 'manga-dialog', label: '文字部分', hint: '剧本 / 分镜文案生成' },
            { field: 'paintModel', task: 'paint', slot: 'manga-paint', label: '图片部分', hint: '角色 / 分镜生图' },
            { field: 'videoModel', task: 'video', slot: 'manga-video', label: '视频生成', hint: '图生视频 · 系统默认 Wan2.2 TI2V 5B' },
          ] as const).map(({ field, task, slot, label, hint }) => {
            const models = mangaOptions[task];
            const value = mangaCfg[field];
            const savedMissing = value !== '' && !models.some((m) => m.id === value);
            return (
              <div key={field}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-3)' }}>
                <span
                  style={{
                    width: 132,
                    flexShrink: 0,
                    fontSize: 'var(--font-size-sm)',
                    color: 'var(--color-text-primary)',
                  }}
                >
                  {label}
                  <span
                    style={{
                      display: 'block',
                      fontSize: 'var(--font-size-xs)',
                      color: 'var(--color-text-tertiary)',
                      marginTop: 1,
                    }}
                  >
                    {hint}
                  </span>
                </span>
                <select
                  aria-label={`漫剧${label}模型`}
                  value={value}
                  onChange={(e) => handleMangaChange(field, e.target.value)}
                  style={{ ...SELECT_STYLE, flex: 1, maxWidth: 360 }}
                >
                  <option value="">系统默认（自动选择）</option>
                  {models.map((m) => (
                    <option key={m.id} value={m.id} disabled={m.status !== 'ready' && m.status !== 'downloaded'}>
                      {m.name}
                      {m.status !== 'ready' ? `（${MODEL_AVAILABLE_STATUS_LABELS[m.status] || m.status}）` : ''}
                    </option>
                  ))}
                  {savedMissing && <option value={value}>{value}（已保存，当前不在清单）</option>}
                </select>
                {renderScopeBtn(slot, true)}
                </div>
                {renderScopePanel(slot)}
              </div>
            );
          })}
        </div>
      </div>
    </section>
  );
};

export default ModuleModelConfig;
// 本项目仅供学习使用，商业授权请+Q 3559331368
