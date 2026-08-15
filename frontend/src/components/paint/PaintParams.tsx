/**
 * PaintParams 绘画参数面板
 * OmniSpace AI v2.1 — Sakura 主题
 * --------------------------------------------------------------------------
 * 正向 / 负向提示词、尺寸（512~2048）、步数（4~50）、引导强度（1~20）、
 * 种子（-1 随机）、批量（1~4）、ControlNet 选项与 LoRA 管理。
 * 模型选择支持自动 / 手动。规格 §9.3 绘画模块。
 */
import { Slider } from '../common/Slider';

/** 绘画参数值 */
export interface PaintParamsValue {
  /** 正向提示词 */
  prompt: string;
  /** 负向提示词 */
  negativePrompt: string;
  /** 宽度 512~2048 */
  width: number;
  /** 高度 512~2048 */
  height: number;
  /** 采样步数 4~50 */
  steps: number;
  /** 引导强度（CFG）1~20 */
  guidanceScale: number;
  /** 种子，-1 表示随机 */
  seed: number;
  /** 批量数量 1~4 */
  batchSize: number;
  /** 采样器（对齐后端 SAMPLER_MAP 值域） */
  sampler: string;
  /** ControlNet 配置 */
  controlnet?: ControlNetConfig | null;
  /** LoRA 列表 */
  loras?: LoraItem[];
}

/** ControlNet 配置 */
export interface ControlNetConfig {
  /** 是否启用 */
  enabled: boolean;
  /** ControlNet 模型名 */
  model: string;
  /** 权重 0~2 */
  weight: number;
}

/** LoRA 项 */
export interface LoraItem {
  /** LoRA 名称 */
  name: string;
  /** 权重 0~2 */
  weight: number;
  /** 是否启用 */
  enabled: boolean;
}

export interface PaintParamsProps {
  /** 当前参数值 */
  value: PaintParamsValue;
  /** 参数变更回调（返回完整的新参数） */
  onChange: (value: PaintParamsValue) => void;
  /** 可选 ControlNet 模型列表 */
  controlnetModels?: string[];
  /** 模型选择模式：auto 自动 / manual 手动 */
  modelMode?: 'auto' | 'manual';
  /** 模型模式变更回调 */
  onModelModeChange?: (mode: 'auto' | 'manual') => void;
  /** 手动模式下可选模型列表 */
  modelOptions?: string[];
  /** 手动模式下当前选中模型 */
  selectedModel?: string;
  /** 手动模型选择变更回调 */
  onModelChange?: (model: string) => void;
  /** 是否禁用（生成中） */
  disabled?: boolean;
}

/** 采样器选项（对齐后端 paint_engine.SAMPLER_MAP 值域；未知名后端回退默认 euler_a） */
export const SAMPLER_OPTIONS: Array<{ value: string; label: string }> = [
  { value: 'dpm++_2m_karras', label: 'DPM++ 2M Karras' },
  { value: 'dpm++_2m', label: 'DPM++ 2M' },
  { value: 'euler_a', label: 'Euler Ancestral' },
  { value: 'euler', label: 'Euler' },
  { value: 'ddim', label: 'DDIM' },
];

/** 分辨率预设 */
const RES_PRESETS = [
  { label: '512 × 512', w: 512, h: 512 },
  { label: '768 × 768', w: 768, h: 768 },
  { label: '1024 × 1024', w: 1024, h: 1024 },
  { label: '512 × 768', w: 512, h: 768 },
  { label: '768 × 512', w: 768, h: 512 },
];

export function PaintParams({
  value,
  onChange,
  modelMode = 'auto',
  onModelModeChange,
  modelOptions = [],
  selectedModel,
  onModelChange,
  disabled = false,
}: PaintParamsProps) {
  /** 更新部分参数 */
  function patch(p: Partial<PaintParamsValue>) {
    onChange({ ...value, ...p });
  }

  /** 更新某个 LoRA 项 */
  function patchLora(index: number, p: Partial<LoraItem>) {
    const loras = (value.loras || []).map((l, i) => (i === index ? { ...l, ...p } : l));
    onChange({ ...value, loras });
  }

  return (
    <div className="flex flex-col gap-4 p-4 overflow-y-auto">
      {/* 模型选择：自动 / 手动 */}
      <div>
        <div className="flex items-center gap-2 mb-2">
          <span className="text-sm font-medium text-[var(--color-text-primary)]">模型选择</span>
          <div className="ml-auto inline-flex rounded-lg border border-[var(--color-input-border)] overflow-hidden">
            {(['auto', 'manual'] as const).map((m) => (
              <button
                key={m}
                type="button"
                disabled={disabled}
                onClick={() => onModelModeChange?.(m)}
                className={[
                  'px-3 h-7 text-xs transition-colors',
                  modelMode === m
                    ? 'bg-sakura-500 text-white'
                    : 'bg-[var(--color-card)] text-[var(--color-text-secondary)] hover:bg-sakura-50',
                ].join(' ')}
              >
                {m === 'auto' ? '自动' : '手动'}
              </button>
            ))}
          </div>
        </div>
        {modelMode === 'manual' ? (
          <select
            value={selectedModel || ''}
            disabled={disabled}
            onChange={(e) => onModelChange?.(e.target.value)}
            className="w-full h-10 px-3 rounded-lg border border-[var(--color-input-border)] bg-[var(--color-input-bg)] text-sm text-[var(--color-text-primary)] focus:outline-none focus:ring-2 focus:ring-sakura-300"
          >
            <option value="">请选择模型</option>
            {modelOptions.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
        ) : (
          <p className="text-xs text-[var(--color-text-tertiary)]">自动模式：由系统根据显存与任务智能选择最优模型。</p>
        )}
      </div>

      {/* 正向提示词 */}
      <div>
        <label className="block text-sm font-medium text-[var(--color-text-primary)] mb-1.5">正向提示词</label>
        <textarea
          value={value.prompt}
          disabled={disabled}
          onChange={(e) => patch({ prompt: e.target.value })}
          rows={3}
          placeholder="一只坐在窗边的猫，阳光，照片级真实…"
          className="w-full px-3 py-2 rounded-lg border border-[var(--color-input-border)] bg-[var(--color-input-bg)] text-sm text-[var(--color-text-primary)] placeholder:text-[var(--color-text-tertiary)] resize-none focus:outline-none focus:ring-2 focus:ring-sakura-300 focus:border-sakura-400"
        />
      </div>

      {/* 负向提示词 */}
      <div>
        <label className="block text-sm font-medium text-[var(--color-text-primary)] mb-1.5">负向提示词</label>
        <textarea
          value={value.negativePrompt}
          disabled={disabled}
          onChange={(e) => patch({ negativePrompt: e.target.value })}
          rows={2}
          placeholder="低质量，模糊，畸形，多肢…"
          className="w-full px-3 py-2 rounded-lg border border-[var(--color-input-border)] bg-[var(--color-input-bg)] text-sm text-[var(--color-text-primary)] placeholder:text-[var(--color-text-tertiary)] resize-none focus:outline-none focus:ring-2 focus:ring-sakura-300 focus:border-sakura-400"
        />
        <p className="text-xs text-[var(--color-text-tertiary)] mt-1">留空时将自动填充默认负面词。</p>
      </div>

      {/* 分辨率预设 */}
      <div>
        <label className="block text-sm font-medium text-[var(--color-text-primary)] mb-1.5">尺寸预设</label>
        <div className="flex flex-wrap gap-2">
          {RES_PRESETS.map((p) => {
            const active = value.width === p.w && value.height === p.h;
            return (
              <button
                key={p.label}
                type="button"
                disabled={disabled}
                onClick={() => patch({ width: p.w, height: p.h })}
                className={[
                  'px-2.5 h-8 text-xs rounded-md border transition-colors',
                  active
                    ? 'border-sakura-500 bg-sakura-100 text-sakura-700'
                    : 'border-[var(--color-input-border)] text-[var(--color-text-secondary)] hover:bg-sakura-50',
                ].join(' ')}
              >
                {p.label}
              </button>
            );
          })}
        </div>
      </div>

      {/* 宽高滑块（512~2048） */}
      <Slider
        label="宽度"
        min={512}
        max={2048}
        step={64}
        value={value.width}
        disabled={disabled}
        onChange={(v) => patch({ width: v })}
      />
      <Slider
        label="高度"
        min={512}
        max={2048}
        step={64}
        value={value.height}
        disabled={disabled}
        onChange={(v) => patch({ height: v })}
      />

      {/* 采样步数（4~50） */}
      <Slider
        label="采样步数"
        min={4}
        max={50}
        step={1}
        value={value.steps}
        disabled={disabled}
        onChange={(v) => patch({ steps: v })}
      />

      {/* 采样器（写回 sampler，后端未知值回退默认 euler_a） */}
      <div>
        <label className="block text-sm font-medium text-[var(--color-text-primary)] mb-1.5">采样器</label>
        <select
          value={value.sampler}
          disabled={disabled}
          onChange={(e) => patch({ sampler: e.target.value })}
          className="w-full h-10 px-3 rounded-lg border border-[var(--color-input-border)] bg-[var(--color-input-bg)] text-sm text-[var(--color-text-primary)] focus:outline-none focus:ring-2 focus:ring-sakura-300"
        >
          {SAMPLER_OPTIONS.map((s) => (
            <option key={s.value} value={s.value}>
              {s.label}
            </option>
          ))}
        </select>
      </div>

      {/* 引导强度（1~20） */}
      <Slider
        label="引导强度（CFG）"
        min={1}
        max={20}
        step={0.5}
        value={value.guidanceScale}
        disabled={disabled}
        onChange={(v) => patch({ guidanceScale: v })}
      />

      {/* 种子（-1 随机） */}
      <div>
        <div className="flex items-center justify-between mb-1.5">
          <label className="text-sm font-medium text-[var(--color-text-primary)]">种子（-1 随机）</label>
          <button
            type="button"
            disabled={disabled}
            onClick={() => patch({ seed: -1 })}
            className="text-xs text-sakura-600 hover:text-sakura-700"
          >
            🎲 随机
          </button>
        </div>
        <input
          type="number"
          value={value.seed}
          disabled={disabled}
          onChange={(e) => patch({ seed: parseInt(e.target.value, 10) || 0 })}
          className="w-full h-10 px-3 rounded-lg border border-[var(--color-input-border)] bg-[var(--color-input-bg)] text-sm text-[var(--color-text-primary)] focus:outline-none focus:ring-2 focus:ring-sakura-300 focus:border-sakura-400"
        />
      </div>

      {/* 批量数量（1~4） */}
      <div>
        <label className="block text-sm font-medium text-[var(--color-text-primary)] mb-1.5">生成数量</label>
        <div className="inline-flex rounded-lg border border-[var(--color-input-border)] overflow-hidden">
          {[1, 2, 3, 4].map((n) => (
            <button
              key={n}
              type="button"
              disabled={disabled}
              onClick={() => patch({ batchSize: n })}
              className={[
                'w-10 h-9 text-sm transition-colors',
                value.batchSize === n
                  ? 'bg-sakura-500 text-white'
                  : 'bg-[var(--color-card)] text-[var(--color-text-secondary)] hover:bg-sakura-50',
              ].join(' ')}
            >
              {n}
            </button>
          ))}
        </div>
      </div>

      {/* ControlNet 选项（审计修复 R2-F09：后端 /draw/generate 不消费 controlnet 字段、
          /draw/controlnet/preview 恒返回 degraded，生成链路未就绪 → 整体禁用并如实说明） */}
      <div className="p-3 rounded-lg border border-[var(--color-border-light)] bg-[var(--color-primary-50)] opacity-60">
        <div className="flex items-center justify-between mb-2">
          <span className="text-sm font-medium text-[var(--color-text-primary)]">ControlNet</span>
          <span className="text-xs text-[var(--color-text-tertiary)]">未就绪</span>
        </div>
        <p className="text-xs text-[var(--color-text-tertiary)] leading-relaxed">
          ControlNet 生成链路未就绪：后端生成接口暂不消费 ControlNet 配置，
          预览接口处于降级状态（模型未随包安装）。该能力将在后续版本开放，当前配置不会生效。
        </p>
      </div>

      {/* LoRA 管理 */}
      {value.loras && value.loras.length > 0 ? (
        <div>
          <label className="block text-sm font-medium text-[var(--color-text-primary)] mb-1.5">LoRA 管理</label>
          <div className="flex flex-col gap-2">
            {value.loras.map((l, i) => (
              <div key={l.name} className="flex items-center gap-2 p-2 rounded-lg border border-[var(--color-border-light)]">
                <input
                  type="checkbox"
                  checked={l.enabled}
                  disabled={disabled}
                  onChange={(e) => patchLora(i, { enabled: e.target.checked })}
                  className="accent-[var(--color-primary-300)]"
                  aria-label={`启用 LoRA ${l.name}`}
                />
                <span className="flex-1 text-sm text-[var(--color-text-primary)] truncate" title={l.name}>
                  {l.name}
                </span>
                <input
                  type="number"
                  min={0}
                  max={2}
                  step={0.1}
                  value={l.weight}
                  disabled={disabled || !l.enabled}
                  onChange={(e) => patchLora(i, { weight: parseFloat(e.target.value) || 0 })}
                  className="w-14 h-8 px-1.5 text-xs rounded border border-[var(--color-input-border)] bg-[var(--color-input-bg)] text-[var(--color-text-primary)] disabled:opacity-50"
                  aria-label={`LoRA ${l.name} 权重`}
                />
              </div>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}

export default PaintParams;
