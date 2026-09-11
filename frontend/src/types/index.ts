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
  /** 硬件档位（文档B §4.2，如 rtx4070ti / rx6600 / cpu） */
  tier?: { tier: string; label: string; matched_by?: string; [k: string]: unknown };
  /** 手动档位覆盖（"auto" 表示自动探测） */
  tier_override?: string;
  /** P3 精度×量化兼容矩阵规格（见 engines.gpu_backend.resolve_precision） */
  precision_spec?: {
    compute_class?: string;
    label?: string;
    requested?: string;
    resolved?: string;
    quant_supported?: boolean;
    quant?: boolean;
    warned?: boolean;
    supported_precisions?: string[];
    recommended_backend?: string;
    [k: string]: unknown;
  };
  /** P3 降级提示（实际运行设备 / 是否降级 / 中文诚实说明） */
  degradation?: {
    device?: string;
    backend?: string;
    degraded?: boolean;
    directml_available?: boolean;
    reason?: string;
    compute_class?: string;
    [k: string]: unknown;
  };
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

/** 实时遥测（GET /hardware/realtime 与 WS 推送）。
 *  P1-05：null = 后端探测失败（前端显示 --，不渲染编造数据）。 */
export interface HardwareRealtime {
  cpu_percent: number | null;
  ram_percent: number | null;
  ram_used_mb?: number;
  ram_total_mb?: number;
  gpu_util_pct?: number | null;
  vram_used_mb?: number;
  vram_total_mb?: number;
  vram_percent?: number | null;
  gpu_temp_celsius?: number | null;
  /** 时间戳（秒） */
  ts?: number;
}

/* ------------------------------ 模型相关 ------------------------------ */

/** 模型类别（规格 §3.1 + omni 视觉语音全模态 2026-08-21） */
export type ModelCategory =
  | 'dialog'
  | 'video'
  | 'voice'
  | 'vision'
  | 'language'
  | '3d'
  | 'auxiliary'
  | 'omni';

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
/** 联网搜索来源条目（web_refs 事件，架构升级计划 B-阶段一） */
export interface WebRef {
  title: string;
  url: string;
  snippet?: string;
  source?: string;
}

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
  /** 深度思考过程（<think> 双通道 reasoning 帧；与 content 分离存储/渲染） */
  reasoning?: string;
  /** 思考耗时（毫秒；store 在首个 token 帧定格——正文开始即思考结束。
   *  仅实时流式消息携带，历史回放无此字段（显示字数兜底） */
  reasoning_ms?: number;
  /** 关联图片（多模态上传，DIALOG-031/038） */
  images?: string[];
  /** 令牌数估算 */
  tokens?: number;
  /** 联网搜索来源（web_refs 事件；仅实时流式消息携带，回答带【n】引用标注） */
  web_refs?: WebRef[];
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

/** 单条台词气泡（多角色场景，2026-09-08）：每角色一条+旁白（asset_id 空） */
export interface PanelBubble {
  /** 台词文本 */
  text: string;
  /** 左上角横坐标（0~1 相对格宽；缺省=默认左上） */
  x?: number | null;
  /** 左上角纵坐标（0~1 相对格高） */
  y?: number | null;
  /** 宽度（0~1 相对格宽；null=自动贴合内容） */
  w?: number | null;
  /** 关联角色资产 id（空=旁白） */
  asset_id?: string | null;
}

/** 保存状态（顶栏状态点；前端本地状态机，无后端对应） */
export type ShotSaveStatus = 'idle' | 'dirty' | 'saving' | 'saved' | 'error';

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
  /** 台词气泡横向位置（0~1 相对格宽，null=默认左上；拖拽定位 C4 尾巴 2026-09-08） */
  bubble_x?: number | null;
  /** 台词气泡纵向位置（0~1 相对格高，null=默认左上） */
  bubble_y?: number | null;
  /** 台词气泡宽度（0~1 相对格宽，null=自动贴合内容；拉伸把手 2026-09-08） */
  bubble_w?: number | null;
  /** 多角色台词气泡（2026-09-08）：每角色一条+旁白，各带独立位置/宽度；
   *  空数组=回退旧单气泡字段（original_dialogue+bubble_x/y/w） */
  bubbles?: PanelBubble[];
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

/** 漫剧/漫画项目（/comic/project/list item；两产品面共用生成底座） */
export interface ComicProject {
  project_id: string;
  name: string;
  created_at: number;
  updated_at: number;
  /** 作品类型：regular=普通漫剧(5步) | narrative=解说漫剧(6步) */
  work_mode: 'regular' | 'narrative';
  /** 作品画风 key（预置 11 种见 ART_STYLES；custom:{id}=自定义风格；空=未选择；
   * 已下线预设（healing/manga/vintage/live/cartoon）的历史项目仍可正常回显） */
  art_style?: string;
  /** 产品面（2026-09-07 漫画模块 M1）：manga=漫剧库 / comic=漫画页；
   * 旧数据缺省按 manga 回显（后端列默认值同口径） */
  project_type?: 'manga' | 'comic';
}

/** 预置作品画风（新建弹窗选择，key 落库 art_style）
 * 2026-08-29 市场调研裁定（漫剧风格网络调研）：剔除市场无受众/形态不符/
 * 与既有重叠的 5 种（healing/manga/vintage/live/cartoon，后端映射保留兼容），
 * 新增爆款主力（韩漫半写实/厚涂CG玄幻），强化国风两向标签（国风2D/3D国风）。
 * prompt=生图提示词基调（后续 AI 生图注入），hot=热门主推角标 */
export interface ArtStyleDef {
  key: string;
  label: string;
  desc: string;
  prompt: string;
  thumb: string;
  hot?: boolean;
}

/** 卡片缩略图（2026-08-31 本地化：原外链 trae 文生图 API 加载不可靠，
 *  改用本地引擎按各卡风格词实拍，静态资源 frontend/public/style-thumbs；
 *  兜底：文件缺失时由渲染层回退调色板图标） */
const _styleImg = (key: string) => `/style-thumbs/${key}.png`;

export const ART_STYLES: ArtStyleDef[] = [
  {
    key: 'pixar', label: '3D卡通', desc: '皮克斯·茶啊二中质感', hot: true,
    prompt: '3D cartoon style, Pixar style, clay material rendering, soft cinematic lighting, round cute face, clean and bright, high quality',
    thumb: _styleImg('pixar'),
  },
  {
    key: 'anime', label: '日系动漫', desc: '清新通用 · 量产稳定', hot: true,
    prompt: 'japanese anime style, vibrant colors, clean lineart, detailed character art, smooth lines',
    thumb: _styleImg('anime'),
  },
  {
    key: 'chibi', label: 'Q版卡通', desc: '2-3头身 · 萌系极速', hot: true,
    prompt: 'chibi style, 2-3 head-body ratio, cute exaggerated features, high saturation colors, adorable',
    thumb: _styleImg('chibi'),
  },
  {
    key: 'guofeng', label: '国风2D', desc: '工笔重彩 · 东方美学', hot: true,
    prompt: 'chinese guofeng 2D anime style, gongbi meticulous line art, elegant oriental aesthetics, ink-wash tinted colors, graceful traditional costume design, semi-3D soft shading',
    thumb: _styleImg('guofeng'),
  },
  {
    key: 'xuanhuan', label: '3D国风', desc: '玄幻仙侠 · 华丽特效', hot: true,
    prompt: 'chinese fantasy 3D animation render, xianxia aesthetic, gorgeous ancient costumes, glowing magic effects, grand celestial architecture, epic cinematic lighting',
    thumb: _styleImg('xuanhuan'),
  },
  {
    key: 'manhwa', label: '韩漫半写实', desc: '半写实 · 分层上色 · 爆款主力', hot: true,
    prompt: 'korean manhwa webtoon style, semi-realistic proportions, layered soft shading with subtle gradients, clean polished faces, trendy cinematic color grading',
    thumb: _styleImg('manhwa'),
  },
  {
    key: 'thickpaint', label: '厚涂玄幻', desc: '厚涂CG · 史诗质感',
    prompt: 'digital thick paint CG illustration, impasto brush strokes, rich material textures, dramatic volumetric lighting, epic fantasy atmosphere, layered depth',
    thumb: _styleImg('thickpaint'),
  },
  {
    key: 'inkwash', label: '水墨国风', desc: '水墨写意 · 意境留白',
    prompt: 'chinese ink wash painting, shui-mo style, brush strokes, misty atmosphere, negative space',
    thumb: _styleImg('inkwash'),
  },
  {
    key: 'cyberpunk', label: '赛博朋克', desc: '科幻3D · 霓虹未来',
    prompt: 'cyberpunk style, neon lights, futuristic sci-fi city, moody atmosphere, high tech low life',
    thumb: _styleImg('cyberpunk'),
  },
  {
    key: 'real3d', label: '3D写实', desc: '电影级CG · 质感细腻',
    prompt: '3D render, realistic CGI, cinematic lighting, detailed textures, movie quality',
    thumb: _styleImg('real3d'),
  },
  {
    key: 'battle', label: '热血战斗', desc: '高对比度 · 燃系张力',
    prompt: 'shonen battle anime style, high contrast, dynamic action poses, dramatic lighting, intense energy effects',
    thumb: _styleImg('battle'),
  },
  // ── 2026-08-31 新增 12 主流风格包卡（引擎级真实切换底座/后处理）──
  {
    key: 'comic_en', label: '美式漫画', desc: '超英分镜 · 网点硬线条',
    prompt: 'american comic book art, bold ink outlines, halftone dot shading, dynamic action panels, high saturation primary colors',
    thumb: _styleImg('comic_en'),
  },
  {
    key: 'manga_bw', label: '日式黑白漫画', desc: '网点纸 · 高对比墨线',
    prompt: 'black and white japanese manga, screentone halftone shading, crisp ink linework, dramatic paneling, monochrome',
    thumb: _styleImg('manga_bw'),
  },
  {
    key: 'ghibli', label: '吉卜力手绘', desc: '水彩背景 · 温暖自然光',
    prompt: 'ghibli style hand-painted animation, soft watercolor backgrounds, gentle natural sunlight, warm nostalgic atmosphere',
    thumb: _styleImg('ghibli'),
  },
  {
    key: 'pixel', label: '像素风', desc: '16位复古游戏 · 抖动上色',
    prompt: 'pixel art, 16-bit retro game style, crisp pixels, limited color palette, dithering',
    thumb: _styleImg('pixel'),
  },
  {
    key: 'uscartoon', label: '美式卡通', desc: '扁平夸张 · 粗描边明快',
    prompt: 'american cartoon style, flat bold shapes, exaggerated expressions, thick outlines, bright playful colors',
    thumb: _styleImg('uscartoon'),
  },
  {
    key: 'steampunk', label: '蒸汽朋克', desc: '黄铜齿轮 · 维多利亚复古',
    prompt: 'steampunk, victorian brass machinery, gears and clockwork, copper steam pipes, sepia warm tones',
    thumb: _styleImg('steampunk'),
  },
  {
    key: 'flat', label: '扁平插画', desc: '几何色块 · 商业设计向',
    prompt: 'flat design illustration, clean geometric shapes, minimal vector style, bold color blocks, no gradients',
    thumb: _styleImg('flat'),
  },
  {
    key: 'claymation', label: '黏土定格', desc: '手工黏土 · 定格动画质感',
    prompt: 'claymation, stop-motion clay puppet, fingerprint texture, handcrafted plasticine surfaces, soft studio lighting',
    thumb: _styleImg('claymation'),
  },
  {
    key: 'popart', label: '波普复古', desc: '复古印刷 · 双色撞色',
    prompt: 'pop art style, retro print poster, halftone dots, bold duotone colors, screenprint grain',
    thumb: _styleImg('popart'),
  },
  {
    key: 'storybook', label: '童话绘本', desc: '水粉蜡笔 · 暖粉彩亲子向',
    prompt: 'children storybook illustration, soft gouache and crayon texture, warm pastel palette, cute rounded shapes',
    thumb: _styleImg('storybook'),
  },
  {
    key: 'gothic', label: '哥特暗黑', desc: '巴洛克阴影 · 深红炭黑',
    prompt: 'gothic dark fantasy, baroque shadows, ornate dark architecture, deep crimson and charcoal palette, chiaroscuro',
    thumb: _styleImg('gothic'),
  },
  {
    key: 'lowpoly', label: '低多边形3D', desc: '几何切面 · 风格化简约',
    prompt: 'low poly 3D render, faceted geometry, flat shaded polygons, stylized minimal shapes, gradient color blocking',
    thumb: _styleImg('lowpoly'),
  },
  // ── 2026-09-02 新增 4 包（市场调研爆款赛道：3D国漫/悬疑/次世代二次元；
  //    key=gen_router 包 sid，中文定族行走后端 _ART_STYLE_ZH）──
  {
    key: 'donghua3d', label: '3D国漫写实', desc: '凡人修仙传派 · UE5渲染',
    prompt: '3D Chinese donghua animation style, realistic grounded facial modeling, unreal engine 5 cinematic render, subsurface scattering skin, film-grade muted color grading',
    thumb: _styleImg('donghua3d'),
  },
  {
    key: 'mystery3d', label: '3D国漫悬疑暗黑', desc: '冷灰蓝 · 光影切割 · 推理',
    prompt: '3D donghua characters, murder mystery atmosphere, desaturated cold grey-blue palette, chiaroscuro lighting, single warm light accent, film noir grading',
    thumb: _styleImg('mystery3d'),
  },
  {
    key: 'xianxia_cg', label: '次世代二次元仙侠CG', desc: '卡通脸+PBR实景 · 二游CG',
    prompt: 'next-gen anime game CG, cel-shaded anime face with rim light, NPR character over PBR realistic environment, xianxia fantasy, sea of clouds, immortal palaces',
    thumb: _styleImg('xianxia_cg'),
  },
  {
    key: 'guofeng_hist', label: '3D古风历史', desc: '纯历史质感 · 汉服宫殿 · 无光效',
    prompt: 'ancient Chinese historical 3D render, hanfu with realistic fabric texture, palace halls, warm candlelight, muted ivory vermilion ink palette, UE5 cinematic',
    thumb: _styleImg('guofeng_hist'),
  },
];

/** 自定义作品风格（GET /comic/art-style/list item；key=custom:{id} 落库 projects.art_style） */
export interface CustomArtStyle {
  style_id: string;
  key: string;
  name: string;
  prompt: string;
  created_at: number;
  /** 归属风格包 id（2026-08-31 卡片↔包显式绑定；custom:* 为导入包） */
  pack?: string;
  /** 是否带导入的风格包定义（旧数据为 false，路由回落嗅探） */
  has_pack_def?: boolean;
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
  status: 'ready' | 'loading' | 'not_installed' | 'downloaded' | 'offload';
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
  /** project=项目资产 / global=全局资产（跨项目复用，删除项目时保留） */
  scope?: 'project' | 'global';
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
  /** V37：逐镜实际种子 JSON（如 "[12345,12346]"），重生成沿用复现 */
  shot_seeds?: string;
  /** V37：VLM 一致性评分 JSON（shots/min/retried/picked 等） */
  consistency?: string;
  /** 画面来源（2026-09-03 方案A）：describe=按描述词；fallback=无描述词原文直出；
   *  空串=旧数据未知（不标注） */
  source_mode?: string;
  created_at: number;
}

/** 漫剧项目 */
export interface Storyboard {
  id: string;
  name: string;
  /** 分镜行（≤200 行 STORYBOARD_MAX_ROWS，2026-08-23 50 → 200） */
  rows: StoryboardRow[];
  /** 场景资产 */
  scenes?: unknown[];
  /** 道具资产 */
  props?: unknown[];
  created_at: string | number;
  updated_at: string | number;
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
  /** 排队位次（1 起；status=pending 且后端视频/图像队列在队时携带） */
  queue_position?: number;
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
// 本项目仅供学习使用，商业授权请+Q 3559331368
