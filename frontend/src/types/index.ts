/* ==========================================================================
 * OmniSpace AI v2.1 —— 前端类型定义（规格 §3.1 前端类型定义）
 * --------------------------------------------------------------------------
 * 严格 TypeScript 模式：所有跨模块共享的接口/类型集中于此。
 * 四种重量级 AI 功能互斥（dialog / paint / video_gen / training，规格 §6.1）。
 * 非大模型功能（分镜表编辑、资产浏览、项目设置、历史、模型管理、性能监控）始终可用。
 * ========================================================================== */

/* ------------------------------ 通用信封 ------------------------------ */

/**
 * 统一响应信封（文档B §9.1 / 文档D §4.2.5 修正条款，权威格式禁止偏移）：
 * 所有 REST 接口返回 { success, data, error, meta }
 */
export interface ApiResponse<T = unknown> {
  /** 业务成功标志 */
  success: boolean;
  /** 业务数据载荷（失败时为 null） */
  data: T | null;
  /** 错误对象（成功时为 null）；code 为语义化字符串（6 类体系） */
  error: {
    /** 语义化错误码，如 MODEL_NOT_LOADED / SYSTEM_PARAM_INVALID */
    code: string;
    /** 错误提示文案（全中文） */
    message: string;
    /** 错误明细（字符串；结构化信息由后端序列化为 JSON 字符串） */
    detail: string;
    /** 修复建议（可选） */
    suggestion?: string;
  } | null;
  /** 请求元数据 */
  meta: {
    /** 请求追踪 ID（后端 uuid4 下发，排障用） */
    request_id: string;
    /** ISO 8601 UTC 时间戳 */
    timestamp: string;
    /** 服务端处理耗时毫秒 */
    duration_ms: number;
  };
}

/** 统一错误对象：api 层在 success === false 或网络异常时抛出 */
export interface ApiError {
  /** 语义化错误码（如 MODEL_NOT_LOADED；前端自造码 FRONTEND_*） */
  code: string;
  /** 错误提示文案 */
  message: string;
  /** 错误明细 */
  detail?: string;
  /** 修复建议（后端可选下发） */
  suggestion?: string;
  /** 请求追踪 ID */
  request_id?: string;
}

/* ------------------------------ 硬件相关 ------------------------------ */

/** GPU 静态画像 */
export interface GpuInfo {
  /** 显卡名称 */
  name: string;
  /** 是否可用独立显卡 */
  available: boolean;
  /** 显存总量 MB */
  vram_total_mb: number;
  /** 显存空闲 MB */
  vram_free_mb: number;
  /** 显存已用 MB */
  vram_used_mb: number;
  /** GPU 利用率百分比 */
  gpu_util_pct: number;
  /** 硬件档位评分（H-4） */
  tier: string;
  /** 档位分数 */
  score: number;
  /** 计算能力版本（如 8.6） */
  compute_capability?: string;
}

/** CPU 静态画像 */
export interface CpuInfo {
  name: string;
  cores: number;
  threads: number;
  usage_percent: number;
  temp_celsius: number | null;
}

/** 内存静态画像 */
export interface RamInfo {
  total_mb: number;
  available_mb: number;
  used_mb: number;
  percent: number;
}

/** 磁盘静态画像 */
export interface DiskInfo {
  total_gb: number;
  used_gb: number;
  free_gb: number;
  percent: number;
}

/** 电源画像（笔记本电池，无则为 null） */
export interface PowerInfo {
  has_battery: boolean;
  plugged: boolean;
  percent: number | null;
}

/** 硬件画像（规格 §4.6 GET /hardware/info 返回） */
export interface HardwareProfile {
  gpu: GpuInfo;
  cpu: CpuInfo;
  ram: RamInfo;
  disk: DiskInfo;
  power: PowerInfo;
}

/**
 * 协同调度模式（规格 §5.1 GPU/CPU/内存协同调度状态机）。
 * - gpu_primary        GPU 主导（资源充足，全部走 GPU）
 * - cpu_assist         GPU 主导 + CPU 协助（GPU 偏紧，CPU 分担预处理）
 * - gpu_assist_cpu     CPU 主导 + GPU 协助（无独显或显存极小，GPU 仅加速部分算子）
 * - memory_pressure    内存压力（GPU/CPU 协助将显存溢出卸载至内存）
 * - all_tense          智能降级中（全局资源紧张，降低精度/批处理）
 * - all_idle           预加载中（全空闲，后台预热常驻模型）
 */
export type SynergyMode =
  | 'gpu_primary'
  | 'cpu_assist'
  | 'gpu_assist_cpu'
  | 'memory_pressure'
  | 'all_tense'
  | 'all_idle';

/** 调度器状态（规格 §5.1） */
export interface SchedulerState {
  /** 当前协同模式 */
  current_mode: SynergyMode;
  /** 上一周期模式（用于滞回判定） */
  previous_mode?: SynergyMode;
  /** 1 秒循环时间戳 */
  last_tick_at?: number;
  /** 30 秒滞回窗口剩余秒数 */
  hysteresis_remaining_s?: number;
  /** 调度器是否运行 */
  running?: boolean;
}

/** VRAM 状态（含常驻/缓存模型） */
export interface VramState {
  used_mb: number;
  total_mb: number;
  free_mb: number;
  percent: number;
  /** 常驻 GPU 显存的模型（不可被 LRU 淘汰） */
  resident_models: Array<string | { name: string; model?: string }>;
  /** 内存缓存中的模型（已从 GPU 卸载但保留内存） */
  cached_models: Array<string | { name: string; model?: string }>;
}

/** 协同调度聚合状态（GET /hardware/synergy 返回 data） */
export interface SynergyState {
  scheduler: SchedulerState;
  vram: VramState;
  /** 功能互斥锁快照（规格 §6.1） */
  feature_lock: FeatureLockSnapshot;
  /** GPU 温度保护状态机快照（规格 §4.1.3，第三轮审计补齐暴露） */
  thermal_guard?: ThermalGuardState;
}

/** GPU 温度保护状态（thermal_guard.get_status） */
export interface ThermalGuardState {
  /** normal / throttling(≥85°C) / paused(≥90°C) */
  state: 'normal' | 'throttling' | 'paused';
  /** 是否处于强制暂停态（≥90°C，拒绝新生成任务） */
  paused: boolean;
  /** 是否处于降频态（≥85°C） */
  throttling: boolean;
  last_temp_celsius: number;
  warning_threshold: number;
  critical_threshold: number;
  /** 连续高温事件计数（≥3 锁定利用率 80%） */
  consecutive_hot_events: number;
  /** 会话级 GPU 利用率上限（连续3次高温后锁 0.80，否则 null） */
  util_cap: number | null;
  session_pause_count: number;
  session_throttle_count: number;
  state_since: number;
}

/** 功能互斥锁快照 */
export interface FeatureLockSnapshot {
  /** 当前持有锁的功能（null 表示空闲） */
  active_feature: ActiveFeature;
  /** 持有者任务 ID */
  holder_task_id?: string | null;
  /** 持有时间戳（秒） */
  acquired_at?: number;
}

/** 实时遥测（GET /hardware/realtime 与 WS 推送） */
export interface HardwareRealtime {
  cpu_percent: number;
  ram_percent: number;
  ram_used_mb?: number;
  ram_total_mb?: number;
  gpu_util_pct?: number;
  vram_used_mb?: number;
  vram_total_mb?: number;
  vram_percent?: number;
  gpu_temp_celsius?: number | null;
  /** 时间戳（秒） */
  ts?: number;
}

/* ------------------------------ 模型相关 ------------------------------ */

/** 模型类别（规格 §3.1） */
export type ModelCategory =
  | 'dialog'
  | 'video'
  | 'voice'
  | 'vision'
  | 'language'
  | '3d'
  | 'auxiliary';

/** 模型就绪状态 */
export type ModelStatus = 'ready' | 'not_ready' | 'loading' | 'error';

/** 单个模型信息（对齐后端 GET /v1/models 返回项） */
export interface ModelInfo {
  /** 模型 ID */
  id: string;
  /** 模型名称 */
  name: string;
  /** 模型类别 */
  category: ModelCategory;
  /** 用途描述 */
  purpose: string;
  /** 就绪状态 */
  status: ModelStatus;
  /** 权重大小（GB） */
  size_gb: number;
  /** 参数量描述（如 "8B"，可为空串） */
  params: string;
  /** 最低显存要求（GB） */
  min_vram_gb: number;
  /** 权重目录路径（未下载为空串） */
  file_path?: string;
  /** 权重文件 sha256（可选） */
  sha256?: string;
  /** 是否已下载到本地 */
  downloaded: boolean;
  /** 是否已加载到 GPU 显存 */
  loaded: boolean;
  /** 关联功能列表 */
  associated_features?: string[];
  /** 展示名称（导入/详情接口可选下发） */
  display_name?: string;
  /** 权重目录实际大小 MB */
  size_mb?: number;
  /** 所需显存 MB */
  vram_mb?: number;
  /** 权重目录路径（导入接口字段） */
  weights_path?: string;
  /** 缺失文件列表（status=not_ready 时存在） */
  missing?: string[];
  /** 版本 */
  version?: string;
  /** 是否锁定内存缓存（MOD-004） */
  cache_locked?: boolean;
  /** 备注/描述 */
  description?: string;
}

/* ------------------------------ 对话相关 ------------------------------ */

/** 消息角色 */
export type MessageRole = 'user' | 'assistant' | 'system';

/** 对话消息 */
export interface DialogMessage {
  id: string;
  session_id: string;
  role: MessageRole;
  /** 文本内容（流式逐 token 追加） */
  content: string;
  /** 创建时间戳（ISO 字符串或秒） */
  created_at: string | number;
  /** 评分（1 赞 / -1 踩 / 0 未评，DIALOG-024） */
  rating?: number;
  /** 是否收藏（DIALOG-046） */
  favorite?: boolean;
  /** 引擎来源标注（如 qwen2vl） */
  engine?: string;
  /** 关联图片（多模态上传，DIALOG-031/038） */
  images?: string[];
  /** 令牌数估算 */
  tokens?: number;
}

/** 对话会话 */
export interface DialogSession {
  id: string;
  title: string;
  /** 是否置顶（DIALOG-010） */
  pinned?: boolean;
  /** 对话模式 */
  mode?: string;
  /** 最后一条消息预览 */
  last_message?: string;
  /** 消息数 */
  message_count?: number;
  created_at: string | number;
  updated_at: string | number;
}

/** 分页列表契约（后端统一 {items, total}） */
export interface Paginated<T> {
  items: T[];
  total: number;
  page?: number;
  page_size?: number;
}

/* ------------------------------ 绘画相关 ------------------------------ */

/** 绘画请求参数 */
export interface PaintRequest {
  /** 正向提示词 */
  prompt: string;
  /** 反向提示词 */
  negative_prompt?: string;
  /** 宽 */
  width?: number;
  /** 高 */
  height?: number;
  /** 采样步数 */
  steps?: number;
  /** 引导强度 */
  cfg_scale?: number;
  /** 采样器 */
  sampler?: string;
  /** 指定绘画模型（手动模式；缺省由后端自动调度） */
  model?: string;
  /** 种子（-1 随机） */
  seed?: number;
  /** 批次数量 */
  batch_size?: number;
  /** LoRA 列表 */
  loras?: Array<{ name: string; weight: number }>;
  /** 模式 quick/advanced */
  mode?: 'quick' | 'advanced';
  /** 图生图参考图 */
  init_image?: string;
  /** 图生图重绘强度 */
  denoising_strength?: number;
  /** ControlNet 条件 */
  controlnet?: unknown;
}

/** 绘画结果 */
export interface PaintResult {
  id: string;
  /** 生成图 URL */
  url: string;
  /** 缩略图 URL */
  thumbnail?: string;
  /** 生成参数回填 */
  params: PaintRequest;
  /** 种子（实际） */
  seed?: number;
  created_at: string | number;
  /** 是否收藏 */
  favorite?: boolean;
  /** 是否采纳 */
  adopted?: boolean;
  /** 是否丢弃 */
  discarded?: boolean;
}

/* ------------------------------ 漫剧相关 ------------------------------ */

/** 分镜行生成状态（对齐后端 _row_to_storyboard_row） */
export type StoryboardGenerationStatus = 'pending' | 'generating' | 'done' | 'error' | 'skipped';

/** 分镜行（对齐后端 manga.py _row_to_storyboard_row 返回格式） */
export interface StoryboardRow {
  /** 行 ID */
  id: string;
  /** 镜号 */
  shot_number: number;
  /** 原始台词 */
  original_dialogue: string;
  /** 画面描述 */
  description: string;
  /** 出场角色（数组） */
  characters: string[];
  /** 场景 */
  scene: string;
  /** 道具（数组） */
  props: string[];
  /** 配音音色 ID */
  voice_id: string;
  /** 配音情感标签 */
  voice_emotion: string;
  /** 配音语速 0.5~2.0（后端 storyboard_rows.speed，默认 1.0；COMIC-069） */
  speed?: number;
  /** 配音音量 dB -12~0（后端 storyboard_rows.volume，默认 0.0；COMIC-069） */
  volume?: number;
  /** 导演台是否完成 */
  director_stage_done: boolean;
  /** 生成状态 */
  generation_status: StoryboardGenerationStatus;
  /** 是否为 AI 生成内容（行级标记） */
  is_ai_generated: boolean;
  /** 绑定资产 ID（/comic/asset/bind 写入 storyboard_rows.asset_id，恒=最近绑定/剩余首元素） */
  asset_id?: string;
  /** 多资产绑定列表（PUT /comic/asset/bind 追加语义，2026-08-13 多资产契约） */
  asset_ids?: string[];
  /** 行锁定（锁定后批量操作自动跳过此行） */
  is_locked?: boolean;
  /** 排序索引（/storyboard/reorder 持久化） */
  sort_index?: number;
  /** 镜头类型/角度/运镜（导演台字段） */
  camera_type?: string;
  camera_angle?: string;
  camera_movement?: string;
  /** 时长（秒，0=未设定） */
  duration?: number;
  /** 转场 */
  transition?: string;
  /** 配乐路径 */
  music_path?: string;
}

/** 漫剧项目（/comic/project/list item） */
export interface ComicProject {
  project_id: string;
  name: string;
  created_at: number;
  updated_at: number;
  /** 作品类型：regular=普通漫剧(5步) | narrative=解说漫剧(6步) */
  work_mode: 'regular' | 'narrative';
}

/** 模型配置（工序弹窗 G2 复用） */
export interface ModelConfig {
  dialog_model?: string;
  paint_model?: string;
  video_model?: string;
  resolution?: string;
  duration_seconds?: number;
}

/** 可用模型信息（GET /manga/models/available） */
export interface AvailableModel {
  id: string;
  name: string;
  status: 'ready' | 'loading' | 'not_installed' | 'offload';
  vram_gb: number;
  speed_label: string;
  notes?: string;
}

/** 漫剧资产类型 */
export type ComicAssetKind = 'character' | 'scene' | 'prop';

/** 漫剧资产（/comic/asset/library item） */
export interface ComicAsset {
  asset_id: string;
  project_id: string;
  kind: ComicAssetKind;
  name: string;
  /** DATA_DIR 相对路径（经 /manga/media/{path} 回读） */
  file_path: string;
  prompt: string;
  meta: Record<string, unknown>;
  created_at: number;
}

/** 关键帧版本（/manga/keyframe/list item） */
export interface KeyframeItem {
  keyframe_id: string;
  row_id: string;
  project_id: string;
  version: number;
  /** DATA_DIR 相对路径（经 /manga/media/{path} 回读） */
  file_path: string;
  prompt: string;
  status: string;
  error: string;
  is_current: boolean;
  created_at: number;
}

/** 漫剧项目 */
export interface Storyboard {
  id: string;
  name: string;
  /** 分镜行（≤50 行 STORYBOARD_MAX_ROWS） */
  rows: StoryboardRow[];
  /** 角色 */
  characters?: DirectorCharacter[];
  /** 场景资产 */
  scenes?: unknown[];
  /** 道具资产 */
  props?: unknown[];
  created_at: string | number;
  updated_at: string | number;
}

/** 导演台状态 */
export interface DirectorStageState {
  project_id: string;
  /** 摄像机列表（默认 5 预设机位） */
  cameras: DirectorCamera[];
  /** 全景配置 */
  panorama?: PanoramaConfig;
  /** 当前选中机位 */
  active_camera?: string;
  /** 场景自定义模型路径 */
  custom_model_path?: string;
}

/** 导演台角色 */
export interface DirectorCharacter {
  id: string;
  name: string;
  /** 3D 模型路径（glb/gltf） */
  model_path?: string;
  /** 位置 [x, y, z] */
  position?: [number, number, number];
  /** 旋转 [x, y, z] */
  rotation?: [number, number, number];
  /** 缩放 */
  scale?: number;
  /** 绑定音色 ID */
  voice_id?: string;
}

/** 摄像机（§9.3 多摄像机系统，5 预设模板） */
export interface DirectorCamera {
  name: string;
  /** 摄像机位置 [x, y, z] */
  position: [number, number, number];
  /** 目标点 [x, y, z] */
  target: [number, number, number];
  /** 视场角 */
  fov: number;
}

/** 全景配置 */
export interface PanoramaConfig {
  /** 是否启用全景 */
  enabled: boolean;
  /** 全景图 URL */
  url?: string;
  /** 视场角 */
  fov?: number;
  /** 中心朝向 */
  heading?: number;
}

/* ------------------------------ 视频生成相关 ------------------------------ */

/** 视频生成请求（四工序：text2img → img2video → tts → package） */
export interface VideoGenRequest {
  project_id?: string;
  shot_id?: string;
  /** 分镜行 ID 列表（批量） */
  shot_ids?: string[];
  /** 起始图片 */
  init_image?: string;
  /** 提示词 */
  prompt?: string;
  /** 帧数（AnimateLCM 默认 16） */
  frames?: number;
  /** 帧率（默认 8） */
  fps?: number;
  /** 时长秒数 */
  duration?: number;
  /** 分辨率 */
  resolution?: string;
}

/** 视频生成结果 */
export interface VideoGenResult {
  task_id: string;
  /** 视频 URL */
  url?: string;
  /** 缩略图 */
  thumbnail?: string;
  /** 当前工序 */
  stage?: 'text2img' | 'img2video' | 'tts' | 'package';
  /** 进度 0-1 */
  progress?: number;
  status?: 'pending' | 'running' | 'done' | 'error';
  /** 错误信息 */
  error?: string;
  created_at?: string | number;
}

/* ------------------------------ 音色相关 ------------------------------ */

/** 音色情感标签 */
export interface VoiceEmotion {
  id?: string;
  /** 情感标签名（默认/愤怒/悲伤等） */
  label: string;
  /** 情感强度 0-1 */
  emotion_intensity: number;
  /** 语速 0.25-4.0 */
  speed: number;
  /** 音调 -1.0-1.0 */
  pitch: number;
}

/** 音色档案 */
export interface VoiceProfile {
  id: string;
  /** 音色名称 */
  name: string;
  /** 音色类型（内置/克隆） */
  voice_type: 'builtin' | 'cloned';
  /** 状态 */
  status: 'ready' | 'training' | 'error';
  /** 引擎来源（sovits / pyttsx3） */
  engine?: string;
  /** 参考音频路径（克隆音色） */
  ref_audio?: string;
  /** 情感标签列表 */
  emotions: VoiceEmotion[];
  /** 绑定角色数 */
  bound_characters?: number;
  created_at?: string | number;
}

/* ------------------------------ 训练相关 ------------------------------ */

/** 训练任务类型 */
export type TrainType =
  | 'knowledge_lora'
  | 'scene_lora'
  | 'style_lora'
  | 'motion_lora'
  | 'character_lora'
  | 'voice_clone';

/** 训练任务优先级 */
export type TrainPriority = 'auto' | 'manual';

/** 训练任务 */
export interface TrainTask {
  id: string;
  type: TrainType;
  /** 任务名称 */
  name: string;
  status: 'pending' | 'running' | 'done' | 'error' | 'cancelled';
  /** 进度 0-1 */
  progress: number;
  priority: TrainPriority;
  /** 关联模型 */
  target_model?: string;
  /** 训练数据集 */
  dataset?: string;
  /** 超参数 */
  hyperparams?: Record<string, unknown>;
  /** 错误信息 */
  error?: string;
  /** 输出权重路径 */
  output_path?: string;
  created_at: string | number;
  updated_at?: string | number;
}

/* ------------------------------ 功能互斥（规格 §6.1） ------------------------------ */

/**
 * 活跃功能（规格 §6.1 大模型功能互斥状态机）。
 * 四种重量级 AI 功能同一时刻仅允许一类运行，null 表示空闲。
 */
export type ActiveFeature = 'dialog' | 'paint' | 'video_gen' | 'training' | null;

/** 功能互斥规则项 */
export interface FeatureSwitchRule {
  /** 当前活跃功能下被阻断的功能列表 */
  blocked: Exclude<ActiveFeature, null>[];
  /** 阻断提示文案 */
  message: string;
}

/**
 * 功能互斥状态机规则（规格 §6.1）。
 * key 为当前活跃功能名（字符串），空闲态以 '__idle__' 表示。
 * 四种重量级 AI 功能同一时刻仅允许一类运行，其余被阻断并返回友好提示。
 * 通过 canSwitchFeature() 统一查询，避免直接用 null 作为对象键。
 */
export const FEATURE_SWITCH_RULES: Record<string, FeatureSwitchRule> = {
  dialog: {
    blocked: ['paint', 'video_gen', 'training'],
    message: '当前正在使用对话功能，请结束对话后再使用此功能',
  },
  paint: {
    blocked: ['dialog', 'video_gen', 'training'],
    message: '绘画进行中，其他AI功能暂不可用',
  },
  video_gen: {
    blocked: ['dialog', 'paint', 'training'],
    message: '视频生成中，请等待完成后再切换',
  },
  training: {
    blocked: ['dialog', 'paint', 'video_gen'],
    message: '训练进行中，其他AI功能暂不可用',
  },
  __idle__: {
    blocked: [],
    message: '',
  },
};

/** 空闲态在规则表中的键名 */
export const IDLE_FEATURE_KEY = '__idle__';

/**
 * 检查功能切换是否被允许（规格 §6.1）。
 * @param active   当前活跃功能（null 表示空闲）
 * @param target   目标功能
 * @returns null 表示放行；返回字符串为阻断提示文案
 */
export function canSwitchFeature(
  active: ActiveFeature,
  target: Exclude<ActiveFeature, null>,
): string | null {
  const key = active ?? IDLE_FEATURE_KEY;
  const rule = FEATURE_SWITCH_RULES[key];
  if (!rule) {
    return null;
  }
  // 空闲或同一功能可重入
  if (active === null || active === target) {
    return null;
  }
  if (rule.blocked.includes(target)) {
    return rule.message;
  }
  return null;
}

/** 功能中文名映射 */
export const FEATURE_LABELS: Record<Exclude<ActiveFeature, null>, string> = {
  dialog: 'AI 对话',
  paint: 'AI 绘画',
  video_gen: '视频生成',
  training: '训练',
};

/* ------------------------------ 任务相关 ------------------------------ */

/** 全局任务（队列统一表示） */
export interface OmniTask {
  id: string;
  /** 任务类型 */
  type: string;
  /** 任务名称 */
  name?: string;
  status: 'pending' | 'running' | 'done' | 'error' | 'cancelled';
  /** 进度 0-1 */
  progress: number;
  /** 优先级 */
  priority?: TrainPriority;
  /** 预览图 URL（实时预览） */
  preview_url?: string;
  /** 结果 URL */
  result_url?: string;
  /** 错误信息 */
  error?: string;
  /** 是否可暂停 */
  pausable?: boolean;
  /** 是否暂停中 */
  paused?: boolean;
  created_at?: string | number;
  updated_at?: string | number;
}

/** WebSocket 连接状态 */
export type WsStatus = 'connecting' | 'open' | 'closed' | 'reconnecting';

/** WebSocket 消息载荷 */
export interface WsMessage<T = unknown> {
  type: string;
  data: T;
}
