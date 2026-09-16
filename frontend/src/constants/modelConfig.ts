import { mirrorPref } from '@/services/uiPrefs';
/* ==========================================================================
 * OmniSpace AI v2.5.0 —— 漫剧模型配置持久化（TASK-P2-07 迁出 ModelConfigModal）
 * --------------------------------------------------------------------------
 * localStorage 键「omnispace.manga.modelConfig」：
 * 结构 {dialogModel,paintModel,videoModel,aspect,duration}；
 * ModelConfigModal 写入，VideoConfirmModal 读取回填。
 * ========================================================================== */

/** 模型配置 localStorage 键 */
export const MODEL_CFG_KEY = 'omnispace.manga.modelConfig';

/** 漫剧模型配置 */
export interface MangaModelConfig {
  /** 推理模型 ID（'' = 系统默认，跟随后端自动选择） */
  dialogModel: string;
  /** 绘画模型 ID（'' = 系统默认） */
  paintModel: string;
  /** 视频模型 ID（'' = 系统默认） */
  videoModel: string;
  /** 视频默认画幅 */
  aspect: '16:9' | '9:16';
  /** 视频默认时长（秒） */
  duration: number;
}

/** 默认配置（模型空串 = 系统默认；横屏 16:9；4 秒）——恢复默认入口共用 */
export const DEFAULT_MODEL_CONFIG: MangaModelConfig = {
  dialogModel: '',
  paintModel: '',
  videoModel: '',
  aspect: '16:9',
  duration: 4,
};

/** 可选视频时长（秒，竞品对齐 4/8/11/15） */
export const VIDEO_DURATION_OPTIONS: readonly number[] = [4, 8, 11, 15];

/** 读取模型配置（localStorage 损坏/缺字段时逐项回退默认） */
export function loadModelConfig(): MangaModelConfig {
  try {
    const raw = localStorage.getItem(MODEL_CFG_KEY);
    if (!raw) return { ...DEFAULT_MODEL_CONFIG };
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') return { ...DEFAULT_MODEL_CONFIG };
    const obj = parsed as Record<string, unknown>;
    return {
      dialogModel: typeof obj.dialogModel === 'string' ? obj.dialogModel : DEFAULT_MODEL_CONFIG.dialogModel,
      paintModel: typeof obj.paintModel === 'string' ? obj.paintModel : DEFAULT_MODEL_CONFIG.paintModel,
      videoModel: typeof obj.videoModel === 'string' ? obj.videoModel : DEFAULT_MODEL_CONFIG.videoModel,
      aspect: obj.aspect === '9:16' ? '9:16' : '16:9',
      duration:
        typeof obj.duration === 'number' && VIDEO_DURATION_OPTIONS.includes(obj.duration)
          ? obj.duration
          : DEFAULT_MODEL_CONFIG.duration,
    };
  } catch {
    return { ...DEFAULT_MODEL_CONFIG };
  }
}

/** 写入模型配置（隐私模式写入失败静默，本次会话内状态仍生效） */
export function writeModelConfig(cfg: MangaModelConfig): void {
  try {
    localStorage.setItem(MODEL_CFG_KEY, JSON.stringify(cfg));
    mirrorPref(MODEL_CFG_KEY, cfg); // 界面偏好镜像（2026-09-12）
  } catch {
    /* 静默：与 readPromptCfg 同策略 */
  }
}
// 本项目仅供学习使用，商业授权请+Q 3559331368
