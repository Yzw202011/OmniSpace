/* ==========================================================================
 * ModuleModelConfig.tsx —— 功能模块模型配置（2026-08-23）
 * --------------------------------------------------------------------------
 * 模型管理页顶部 section：集中展示 / 设置三大功能模块各自使用的大模型。
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
 * ========================================================================== */

import { useEffect, useState } from 'react';
import {
  Check,
  Clapperboard,
  Image as ImageIcon,
  Loader2,
  MessageSquare,
  Settings2,
} from 'lucide-react';
import { useDialogStore } from '@/stores/useDialogStore';
import { usePaintStore } from '@/stores/usePaintStore';
import { useAppStore } from '@/stores/useAppStore';
import {
  getDialogModels,
  getDialogEngineStatus,
  prewarmModel,
  type DialogModelInfo,
} from '@/services/dialogApi';
import { listAvailableModels } from '@/services/mangaApi';
import { MODEL_AVAILABLE_STATUS_LABELS } from '@/constants/statusLabels';
import {
  loadModelConfig,
  writeModelConfig,
  type MangaModelConfig,
} from '@/constants/modelConfig';
import type { AvailableModel } from '@/types';

/* ------------------------------ 绘画共享偏好 ------------------------------ */

/** 绘画模型偏好 localStorage 键（ModuleModelConfig 与 PaintView 双向同步） */
export const PAINT_MODEL_PREF_KEY = 'omnispace.paint.modelPreference';

/** 绘画模型偏好：auto = 智能路由；manual = 显式点名模型 id */
export interface PaintModelPref {
  mode: 'auto' | 'manual';
  modelId: string;
}

/** 读取绘画偏好（损坏 / 缺失回退 auto；绝不抛错） */
export function loadPaintModelPref(): PaintModelPref {
  try {
    const raw = localStorage.getItem(PAINT_MODEL_PREF_KEY);
    if (!raw) return { mode: 'auto', modelId: '' };
    const parsed = JSON.parse(raw) as Partial<PaintModelPref>;
    if (parsed.mode === 'manual' && typeof parsed.modelId === 'string' && parsed.modelId) {
      return { mode: 'manual', modelId: parsed.modelId };
    }
    return { mode: 'auto', modelId: '' };
  } catch {
    return { mode: 'auto', modelId: '' };
  }
}

/** 写入绘画偏好（失败静默） */
export function savePaintModelPref(pref: PaintModelPref): void {
  try {
    localStorage.setItem(PAINT_MODEL_PREF_KEY, JSON.stringify(pref));
  } catch {
    // 存储满 / 隐私模式：不影响交互
  }
}

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
    borderBottom: '1px solid var(--color-divider)',
  };
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
        // 后端未就绪：清单空，下拉仅显示当前值
      }
      try {
        const st = await getDialogEngineStatus();
        if (!cancelled) {
          setDialogEngineModel(String(st?.model || ''));
          setDialogEngineReady(st?.state === 'ready');
        }
      } catch {
        // 引擎状态可选
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
        // 预热失败静默：进入对话页时 prewarm 链路会重试
      } finally {
        setDialogSwitching(false);
      }
    }
  };

  /* ---------- AI 绘画 ---------- */
  const paintModels = usePaintStore((s) => s.models);
  const fetchPaintModels = usePaintStore((s) => s.fetchModels);
  const [paintPref, setPaintPref] = useState<PaintModelPref>(() => loadPaintModelPref());

  useEffect(() => {
    if (paintModels.length === 0) {
      void fetchPaintModels();
    }
  }, [paintModels.length, fetchPaintModels]);

  /** 本地就绪的可选绘画模型（/draw/models status === ready） */
  const readyPaintModels = paintModels.filter((m) => m.status === 'ready');

  /** 切换绘画偏好：写共享 localStorage，PaintView 下次挂载生效 */
  const handlePaintChange = (value: string) => {
    const pref: PaintModelPref =
      value === '__auto__' ? { mode: 'auto', modelId: '' } : { mode: 'manual', modelId: value };
    setPaintPref(pref);
    savePaintModelPref(pref);
    showToast(
      pref.mode === 'auto'
        ? '绘画已切回智能路由（自动挑选最合适的模型）'
        : `绘画模型已指定为「${readyPaintModels.find((m) => m.id === pref.modelId)?.name || pref.modelId}」`,
      'success',
    );
  };

  /* ---------- 漫剧创作（文字/图片/视频 三模型流水线） ---------- */
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
        // 后端未就绪：下拉仅显示当前保存值
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

  /* ------------------------------ 渲染 ------------------------------ */

  return (
    <section className="mm-reco" aria-label="功能模块模型配置">
      <div className="mm-reco-head">
        <span className="mm-reco-title">
          <Settings2 size={15} aria-hidden="true" /> 功能模块模型配置
        </span>
        <span className="mm-reco-gpu">各模块使用的大模型在此集中查看与切换</span>
      </div>

      {/* ---------- AI 对话 ---------- */}
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
        </div>
      </div>

      {/* ---------- AI 绘画 ---------- */}
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
            {paintPref.mode === 'auto' ? (
              <>智能路由：qwen-image（中文最优）→ FLUX.2 → SDXL 兜底，按资源自动挑选</>
            ) : (
              <>已指定：{readyPaintModels.find((m) => m.id === paintPref.modelId)?.name || paintPref.modelId}</>
            )}
          </div>
        </div>
        <select
          aria-label="选择绘画模型"
          value={paintPref.mode === 'auto' ? '__auto__' : paintPref.modelId}
          onChange={(e) => handlePaintChange(e.target.value)}
          style={SELECT_STYLE}
        >
          <option value="__auto__">智能路由（推荐）</option>
          {readyPaintModels.map((m) => (
            <option key={m.id} value={m.id}>
              {m.name}
            </option>
          ))}
        </select>
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
            { field: 'dialogModel', task: 'dialog', label: '文字部分', hint: '剧本 / 分镜文案生成' },
            { field: 'paintModel', task: 'paint', label: '图片部分', hint: '角色 / 分镜生图' },
            { field: 'videoModel', task: 'video', label: '视频生成', hint: '图生视频 · 系统默认 Wan2.2 TI2V 5B' },
          ] as const).map(({ field, task, label, hint }) => {
            const models = mangaOptions[task];
            const value = mangaCfg[field];
            const savedMissing = value !== '' && !models.some((m) => m.id === value);
            return (
              <div key={field} style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-3)' }}>
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
                    <option key={m.id} value={m.id} disabled={m.status !== 'ready'}>
                      {m.name}
                      {m.status !== 'ready' ? `（${MODEL_AVAILABLE_STATUS_LABELS[m.status] || m.status}）` : ''}
                    </option>
                  ))}
                  {savedMissing && <option value={value}>{value}（已保存，当前不在清单）</option>}
                </select>
              </div>
            );
          })}
        </div>
      </div>
    </section>
  );
};

export default ModuleModelConfig;
