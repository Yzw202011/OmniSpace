// 本项目仅供学习使用，商业授权请+Q 3559331368
/* ==========================================================================
 * ModelManager.tsx —— 模型管理主视图
 * --------------------------------------------------------------------------
 * 分组体系（2026-08-20 用户裁定重构）：
 *   一级分组：显存档位（8G / 12G / 16G / 24G），按 min_vram_gb 落档；
 *   二级分组：模型类型（大语言模型 / 多模态大模型 / 视觉大模型 / 辅助模型）；
 *   仅显示 models/ 目录实际存在的模型（downloaded === true）。
 * 每个模型显示: 名称、用途、大小、参数量、最低显存、状态。
 * 导入/校验/卸载/选择/删除操作保留。
 * 手动选择模型（规格 §14 约束10）。
 * 数据源：useModelStore（GET /v1/models）；
 * VRAM 使用率：useHardwareStore（/v1/hardware/realtime 遥测）。
 * ========================================================================== */

import React, { useState, useEffect } from 'react';
import type { LucideIcon } from 'lucide-react';
import {
  PackageOpen,
  FolderOpen,
  MessageSquare,
  Layers,
  Image,
  Wrench,
  Boxes,
  AlertTriangle,
  Import,
  Check,
  MemoryStick,
  Sparkles,
  Cpu,
  ArrowRight,
  Type,
  FileVideo,
  AudioLines,
  VectorSquare,
  Zap,
  Power,
  Loader2,
} from 'lucide-react';
import { useModelStore } from '@/stores/useModelStore';
import { useHardwareStore } from '@/stores/useHardwareStore';
import { useAppStore } from '@/stores/useAppStore';
import {
  categoryToFeature,
  getVllmStatus,
  loadModel as loadModelApi,
  purgeModelFiles,
  stopVllm,
} from '@/services/modelApi';
import type { VllmStatus } from '@/services/modelApi';
import { formatDuration, truncate } from '@/utils/format';
import { MODEL_RUNTIME_STATUS_LABELS } from '@/constants/statusLabels';
import { Modal } from '@/components/common/Modal';
import BenchmarkCard from './BenchmarkCard';
import { ModuleModelConfig } from '@/components/model/ModuleModelConfig';
import { ReadinessBanner } from '@/components/model/ReadinessBanner';
import type { ModelInfo } from '@/types';

/* ------------------------------ 类型体系 ------------------------------ */

/** 模型类型（二级分组）：大语言 / 多模态 / 视觉语音 / 视觉生成 / 辅助 */
type ModelType = 'llm' | 'vlm' | 'omni' | 'visual' | 'aux';

/** 类型 → 中文标签 */
const TYPE_LABELS: Record<ModelType, string> = {
  llm: '大语言模型',
  vlm: '多模态大模型',
  omni: '视觉语音大模型',
  visual: '视觉大模型',
  aux: '辅助模型',
};

/** 类型 → 图标 */
const TYPE_ICONS: Record<ModelType, LucideIcon> = {
  llm: MessageSquare,
  vlm: Layers,
  omni: AudioLines,
  visual: Image,
  aux: Wrench,
};

/** 类型展示顺序（用户指定：大语言 → 多模态 → 视觉语音 → 视觉 → 辅助） */
const TYPE_ORDER: ModelType[] = ['llm', 'vlm', 'omni', 'visual', 'aux'];

/**
 * 模型类型推断（具体分类细则）：
 * - 视觉语音大模型：全模态 Omni 系（id 含 -omni，Qwen2.5-Omni 家族），
 *   文/图/视/音输入，文+语音输出（Thinker-Talker 架构）；
 * - 多模态大模型：视觉语言系列（id 含 -vl，Qwen-VL 家族）；
 * - 辅助模型：嵌入/语音识别/合成/声码器/分割/深度估计/安全审查；
 * - 视觉大模型：图像/视频/3D 生成（含 category vision/video/3d）；
 * - 大语言模型：其余文本生成模型（dialog/language）。
 */
function classifyModelType(m: ModelInfo): ModelType {
  const id = m.id.toLowerCase();
  if (/-omni|_omni/.test(id) || m.category === 'omni') return 'omni';
  if (/-vl\d|-vl-|-vl$/.test(id)) return 'vlm';
  if (
    id.includes('bge') ||
    id.includes('roberta') ||
    id.includes('whisper') ||
    id.includes('hubert') ||
    id.includes('bigvgan') ||
    id.includes('gsv') ||
    id.includes('-tts') ||
    id === 'tts' ||
    id === 'depth' ||
    id.startsWith('sam-') ||
    id.includes('safety_checker')
  ) {
    return 'aux';
  }
  if (m.category === 'vision' || m.category === 'video' || m.category === '3d') {
    return 'visual';
  }
  if (
    id.includes('sdxl') ||
    id.includes('sd15') ||
    id.includes('sd35') ||
    id.includes('flux') ||
    id.includes('animatelcm') ||
    id.includes('ltx') ||
    id.includes('tripo')
  ) {
    return 'visual';
  }
  return 'llm';
}

/* ------------------------------ 输入/输出能力体系（2026-08-20） ------------------------------ */

/** 能力模态 key */
type IOMode = 'text' | 'image' | 'video' | 'audio' | 'embedding';

/** 模态 → 中文标签 + 图标 */
const IO_MODE_META: Record<IOMode, { label: string; icon: LucideIcon }> = {
  text: { label: '文字', icon: Type },
  image: { label: '图片', icon: Image },
  video: { label: '视频', icon: FileVideo },
  audio: { label: '音频', icon: AudioLines },
  embedding: { label: '向量', icon: VectorSquare },
};

/** 输入/输出能力描述（模型 id → 支持的输入模态列表 + 输出模态列表） */
const IO_CAPABILITIES: Record<string, { inputs: IOMode[]; outputs: IOMode[] }> = {
  // 对话 / 多模态
  'qwen3-vl-8b': { inputs: ['text', 'image'], outputs: ['text'] },
  'qwen3-vl-4b': { inputs: ['text', 'image'], outputs: ['text'] },
  'qwen2-vl-2b': { inputs: ['text', 'image'], outputs: ['text'] },
  'qwen3-32b': { inputs: ['text'], outputs: ['text'] },
  'codestral-22b': { inputs: ['text'], outputs: ['text'] },
  // 视觉语音全模态（omni）：四模态输入，文字+语音双输出
  'qwen2.5-omni-7b': { inputs: ['text', 'image', 'video', 'audio'], outputs: ['text', 'audio'] },
  'qwen2.5-omni-7b-int4': { inputs: ['text', 'image', 'video', 'audio'], outputs: ['text', 'audio'] },
  // 绘画
  'flux2-klein-4b': { inputs: ['text'], outputs: ['image'] },
  'flux.1-dev': { inputs: ['text'], outputs: ['image'] },
  'sdxl-base-1.0': { inputs: ['text', 'image'], outputs: ['image'] },
  'sd15': { inputs: ['text', 'image'], outputs: ['image'] },
  'sd35-large': { inputs: ['text', 'image'], outputs: ['image'] },
  // 视频
  'animatelcm': { inputs: ['image'], outputs: ['video'] },
  'ltx-2.3': { inputs: ['text', 'image'], outputs: ['video'] },
  // 辅助
  'bge-large-zh': { inputs: ['text'], outputs: ['embedding'] },
  'whisper-tiny': { inputs: ['audio'], outputs: ['text'] },
  'sam-vit-h': { inputs: ['image'], outputs: ['image'] },
  'depth-anything': { inputs: ['image'], outputs: ['image'] },
  'safety-checker': { inputs: ['image'], outputs: ['text'] },
};

/**
 * 推断模型输入/输出能力：登记表精确匹配优先，未知模型按类型回退。
 * （omni = 文/图/视/音→文+音；vlm = 文+图→文；llm = 文→文；
 *  visual = 文/图→图或视频；aux = 特定）
 */
function modelIO(m: ModelInfo): { inputs: IOMode[]; outputs: IOMode[] } {
  const hit = IO_CAPABILITIES[m.id.toLowerCase()];
  if (hit) return hit;
  const id = m.id.toLowerCase();
  const type = classifyModelType(m);
  if (type === 'omni') {
    return { inputs: ['text', 'image', 'video', 'audio'], outputs: ['text', 'audio'] };
  }
  if (/-tts|tts/.test(id)) return { inputs: ['text'], outputs: ['audio'] };
  if (/whisper|asr|sense-?voice/.test(id)) {
    return { inputs: ['audio'], outputs: ['text'] };
  }
  if (/bge|embed|retriever/.test(id)) {
    return { inputs: ['text'], outputs: ['embedding'] };
  }
  if (type === 'vlm') return { inputs: ['text', 'image'], outputs: ['text'] };
  if (type === 'llm') return { inputs: ['text'], outputs: ['text'] };
  if (type === 'visual') {
    return /lcm|video|ltx|hunyuan/.test(id)
      ? { inputs: ['text', 'image'], outputs: ['video'] }
      : { inputs: ['text', 'image'], outputs: ['image'] };
  }
  return { inputs: ['text'], outputs: ['text'] };
}

/** 大白话功能说明（2026-08-20 用户裁定：不能只给符号，要说人话） */
const IO_PLAIN_SPEECH: Record<string, string> = {
  // 对话 / 多模态
  'qwen3-vl-8b': '你发文字＋图片，它能看懂图、陪你聊天、认东西、读图里的字，用文字回答你',
  'qwen3-vl-4b': '你发文字＋图片，它能看懂图、陪你聊天、认东西、读图里的字，用文字回答你（轻量均衡版）',
  'qwen2-vl-2b': '你发文字＋图片，它能看懂图并用文字回答，速度快、占显存小（轻量版）',
  'qwen3-32b': '只读文字不读图：长文写作、复杂推理、数学、写代码都是强项',
  'codestral-22b': '专职程序员：你用文字提需求，它写出代码并讲解',
  // 视觉语音全模态（omni）
  'qwen2.5-omni-7b': '全能选手：你发文字/图片/视频/录音它都懂，还能开口说话回答你（语音+文字双输出）',
  'qwen2.5-omni-7b-int4': '全能选手（压缩版）：文字/图片/视频/录音都懂，语音+文字回答，12G 显存就能跑',
  // 绘画
  'flux2-klein-4b': '你说一句话，它直接画出一张图（漫剧角色四视图设定图专用底座）',
  'flux.1-dev': '你说一句话，它画出一张高质量图，细节和文字理解都更强',
  'sdxl-base-1.0': 'AI绘画主力：你打一段描述它就出图，也能照着参考图改图',
  'sd15': '你打描述它出图，画幅小速度快，还是做动图视频的底子',
  'sd35-large': '你打描述它出图，比 SDXL 更新、画质更好',
  // 视频
  'animatelcm': '给它一张图，让图里的人物动起来，变成几秒钟短视频',
  'ltx-2.3': '文字或图片都行：描述画面或丢一张图进去，它生成一段短视频',
  // 辅助
  'bge-large-zh': '把中文变成"数字指纹"（向量），知识库搜索全靠它比对相似度',
  'whisper-tiny': '耳朵：听一段语音，把说的话转成文字',
  'sam-vit-h': '美工刀：沿人物轮廓精确抠图、把背景换成纯白',
  'depth-anything': '看一张图就能判断画面里物体的远近深浅（3D 深度）',
  'safety-checker': '安检员：检查生成的图片有没有违规内容',
};

/** 大白话回退：未登记模型按类型/关键词给通用解释 */
function plainSpeech(m: ModelInfo): string {
  const hit = IO_PLAIN_SPEECH[m.id.toLowerCase()];
  if (hit) return hit;
  const id = m.id.toLowerCase();
  const type = classifyModelType(m);
  if (type === 'omni') {
    return '文字/图片/视频/录音都懂的全能选手，用语音+文字回答你';
  }
  if (/-tts|tts/.test(id)) return '你给它一段文字，它把文字念成语音';
  if (/whisper|asr|sense-?voice/.test(id)) return '你给它一段录音，它转成文字';
  if (/bge|embed|retriever/.test(id)) return '把文字变成"数字指纹"，供知识库搜索比对';
  if (type === 'vlm') return '你发文字＋图片，它能看懂图并用文字回答';
  if (type === 'llm') return '你发文字，它用文字回答：写作、问答、推理';
  if (type === 'visual') {
    return /lcm|video|ltx|hunyuan/.test(id)
      ? '文字或图片进，出来一段短视频'
      : '你打描述（或给参考图），它画出一张图';
  }
  return '辅助其他模型工作的专项工具';
}

/* ------------------------------ 档位体系 ------------------------------ */

/** 显存档位 key */
type TierKey = 'all' | '8' | '12' | '16' | '24';

/** 档位定义（一级分组） */
const VRAM_TIERS: Array<{ key: '8' | '12' | '16' | '24'; label: string; desc: string; max: number }> = [
  { key: '8', label: '8G 显存档', desc: '入门显卡即可运行（需求 ≤ 8GB）', max: 8 },
  { key: '12', label: '12G 显存档', desc: '主流显卡（需求 8~12GB）', max: 12 },
  { key: '16', label: '16G 显存档', desc: '高性能显卡（需求 12~16GB）', max: 16 },
  { key: '24', label: '24G 显存档', desc: '旗舰显卡（需求 16GB 以上）', max: Infinity },
];

/** 按 min_vram_gb 落档 */
function vramTierOf(m: ModelInfo): '8' | '12' | '16' | '24' {
  const v = m.min_vram_gb || 0;
  for (const t of VRAM_TIERS) {
    if (v <= t.max) return t.key;
  }
  return '24';
}

/* ------------------------------ 展示名与用途 ------------------------------ */

/** 已知模型的友好显示名（未命中的沿用 name） */
const DISPLAY_NAMES: Record<string, string> = {
  'qwen3-vl-8b': 'Qwen3-VL 8B',
  'qwen3-vl-4b': 'Qwen3-VL 4B',
  'qwen2-vl-2b': 'Qwen2-VL 2B',
  'qwen3-32b': 'Qwen3 32B',
  'codestral-22b': 'Codestral 22B（代码）',
  'Qwen2-0.5B-Instruct-Q4_K_M': 'Qwen2 0.5B（Q4 量化）',
  'qwen2.5-omni-7b': 'Qwen2.5-Omni 7B（视觉语音）',
  'qwen2.5-omni-7b-int4': 'Qwen2.5-Omni 7B（int4 量化）',
  'sdxl-base-1.0': 'SDXL Base 1.0',
  sd15: 'Stable Diffusion 1.5',
  'flux2-klein-4b': 'FLUX.2 Klein 4B',
  AnimateLCM: 'AnimateLCM',
  'ltx-video-0.9.5': 'LTX Video 0.9.5',
  TripoSR: 'TripoSR',
  'bge-large-zh': 'BGE-large-zh',
  'bge-m3': 'BGE-M3',
  'chinese-roberta-wwm-ext-large': 'RoBERTa-wwm-ext-large',
  'whisper-tiny': 'Whisper tiny',
  'chinese-hubert-base': 'HuBERT-base（中文）',
  'qwen3-tts': 'Qwen3-TTS',
  'models--nvidia--bigvgan_v2_24khz_100band_256x': 'BigVGAN v2（24kHz 声码器）',
  'gsv-v2final-pretrained': 'GPT-SoVITS v2 底模',
  depth: '深度估计（Depth Anything）',
  'sam-vit-h': 'SAM 分割（ViT-H）',
};

/** 已知模型的用途说明（在项目中的具体落点：模块 + 干什么） */
const PURPOSE_OVERRIDES: Record<string, string> = {
  'qwen3-vl-8b': 'OmniChat 对话主力：多模态图文理解、截图问答与深度推理（16G 档首选，可常驻）',
  'qwen3-vl-4b': 'OmniChat 对话主力：图文理解与日常问答（8G 档均衡之选，响应更快）',
  'qwen2-vl-2b': 'OmniChat 轻量对话：低显存场景的图文问答回退模型',
  'qwen3-32b': 'OmniChat 深度推理：长文写作、复杂逻辑与数学任务（需 24G 显存）',
  'codestral-22b': 'OmniChat 代码助手：代码生成、补全与重构（22B 代码专精）',
  'Qwen2-0.5B-Instruct-Q4_K_M': 'OmniChat 兜底对话：GGUF Q4 量化，显存极紧张时的极轻量备胎',
  'qwen2.5-omni-7b': 'OmniChat 视觉语音对话：看图/看视频/听录音都能聊，还能开口说话回答（16G 档首选）',
  'qwen2.5-omni-7b-int4': 'OmniChat 视觉语音对话（int4 压缩版）：12G 显存即可语音+文字双输出对话',
  'sdxl-base-1.0': 'OmniDraw 绘画主力：文生图 / 图生图基座（1024 高清出图）',
  sd15: 'OmniDraw 与漫剧底模：SD1.5 基座，AnimateLCM 图生视频的前置依赖',
  'flux2-klein-4b': 'OmniDraw 轻量绘画：FLUX 架构快速出图（低显存友好）',
  AnimateLCM: '漫剧模块：SD1.5 图生视频动画（512×512、16fps、4s 短镜头）',
  'ltx-video-0.9.5': '漫剧模块：高质量视频生成（int8 量化档，实拍级运动一致性）',
  TripoSR: '单图 3D 资产重建（TripoSR）——3D 导演台已移除，当前无功能入口，模型在隔离区',
  'bge-large-zh': 'OmniLearn 知识库：中文语义嵌入，RAG 检索的向量底座',
  'bge-m3': 'OmniLearn 知识库：多语混合嵌入，长文档检索增强（8K 上下文）',
  'chinese-roberta-wwm-ext-large': '语音克隆组件：GPT-SoVITS 中文文本理解编码器',
  'whisper-tiny': '语音识别：音频转文字（导入音色素材时的转录前置）',
  'chinese-hubert-base': '语音克隆组件：GPT-SoVITS 说话人音频特征提取',
  'qwen3-tts': '语音合成：文字转语音（对话朗读 / 漫剧角色配音）',
  'models--nvidia--bigvgan_v2_24khz_100band_256x': '语音克隆组件：GPT-SoVITS 音频声码器（波形还原）',
  'gsv-v2final-pretrained': '语音克隆底模：GPT-SoVITS v2 声音克隆预训练基座',
  depth: '单目深度估计（MiDaS）——3D 导演台已移除，当前无功能入口，模型在隔离区',
  'sam-vit-h': '漫剧模块：图像分割抠图（角色 / 景物精准提取）',
};

/** 已知模型的参数量（未命中沿用后端 params，均无则显示 —） */
const PARAMS: Record<string, string> = {
  'qwen3-vl-8b': '8B',
  'qwen3-vl-4b': '4B',
  'qwen2-vl-2b': '2B',
  'qwen3-32b': '32B',
  'codestral-22b': '22B',
  'Qwen2-0.5B-Instruct-Q4_K_M': '0.5B',
  'qwen2.5-omni-7b': '7B（Thinker 3B + Talker 0.5B）',
  'qwen2.5-omni-7b-int4': '7B（int4 量化）',
  'sdxl-base-1.0': '3.5B',
  sd15: '0.9B',
  'flux2-klein-4b': '4B',
  AnimateLCM: '1.3B',
  'ltx-video-0.9.5': '2B',
  TripoSR: '约 0.8B',
  'bge-large-zh': '326M',
  'bge-m3': '568M',
  'chinese-roberta-wwm-ext-large': '330M',
  'whisper-tiny': '39M',
  'chinese-hubert-base': '95M',
  'qwen3-tts': '0.5B',
  'models--nvidia--bigvgan_v2_24khz_100band_256x': '112M',
  'gsv-v2final-pretrained': '约 0.15B',
  depth: '25M',
  'sam-vit-h': '636M',
};

/** 模型展示名 */
function displayName(m: ModelInfo): string {
  return DISPLAY_NAMES[m.id] || m.name;
}

/** 模型用途展示值 */
function displayPurpose(m: ModelInfo): string {
  return PURPOSE_OVERRIDES[m.id] || m.purpose || '—';
}

/** 模型参数量展示值（前端登记表优先，后端 params 兜底） */
function displayParams(m: ModelInfo): string {
  return PARAMS[m.id] || m.params || '—';
}

/* --------------------- vLLM 推理引擎（2026-08-21） --------------------- */

/** vLLM 托管模型：Qwen3-VL 8B AWQ 量化版 */
const VLLM_MODEL_ID = 'qwen3-vl-8b-awq';
/** 启动时传入的模型类别（POST /models/load 的 category） */
const VLLM_MODEL_CATEGORY = 'dialog';
/** vLLM 状态轮询间隔（ms）：刷新运行时长 / 健康状态 */
const VLLM_POLL_INTERVAL_MS = 15000;

/** vLLM 运行状态徽标（复用 model-status-badge 语义类，颜色全部来自主题 CSS 变量） */
interface VllmRunBadge {
  label: string;
  className: string;
  style?: React.CSSProperties;
}

/** 由状态快照派生运行状态徽标：运行中 / 运行异常 / 已停止 / 未安装 Runtime */
function vllmRunBadgeOf(s: VllmStatus | null): VllmRunBadge {
  if (s === null) {
    return {
      label: '检测中…',
      className: '',
      style: { background: 'var(--color-info-bg)', color: 'var(--color-info)' },
    };
  }
  if (!s.runtime_installed) return { label: '未安装 Runtime', className: 'not_ready' };
  if (!s.running) {
    // P3 常驻热备：后台预冷中 → 明确提示"模型加载中"而非"已停止"
    if (s.booting) {
      return {
        label: '加载中（后台预冷）',
        className: 'reco-over',
      };
    }
    return {
      label: '已停止',
      className: '',
      style: {
        background: 'var(--color-input-bg)',
        color: 'var(--color-text-secondary)',
      },
    };
  }
  return s.healthy
    ? { label: '运行中', className: 'ready' }
    : { label: '运行异常', className: 'reco-over' };
}

/**
 * 模型管理主视图组件
 * 按显存档位（一级）× 模型类型（二级）分组展示本地已有模型。
 */
export const ModelManager: React.FC = () => {
  const models = useModelStore((s) => s.models);
  const loading = useModelStore((s) => s.loading);
  const fetchModels = useModelStore((s) => s.fetchModels);
  const verifyModel = useModelStore((s) => s.verifyModel);
  const selectModel = useModelStore((s) => s.selectModel);
  const loadModel = useModelStore((s) => s.loadModel);
  const deleteModel = useModelStore((s) => s.deleteModel);
  const importModel = useModelStore((s) => s.importModel);

  const [activeTier, setActiveTier] = useState<TierKey>('all');
  const [selectedId, setSelectedId] = useState<string>('');
  const [busy, setBusy] = useState('');
  /** 搜索关键词（MODEL-004：名称/ID/用途模糊匹配） */
  const [searchQuery, setSearchQuery] = useState('');
  /** 排序键（MODEL-002：默认保持后端登记顺序） */
  const [sortKey, setSortKey] = useState<'default' | 'name' | 'size_desc' | 'size_asc' | 'vram'>('default');
  /** 导入模型弹窗 */
  const [importOpen, setImportOpen] = useState(false);
  const [importPath, setImportPath] = useState('');
  const [importBusy, setImportBusy] = useState(false);
  /** 删除确认弹窗目标 */
  const [deleteTarget, setDeleteTarget] = useState<ModelInfo | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  /** 卸载+删盘风险弹窗目标（2026-08-20 卸载按钮升级） */
  const [purgeTarget, setPurgeTarget] = useState<ModelInfo | null>(null);
  const [purgeBusy, setPurgeBusy] = useState(false);
  /** 风险确认倒计时（10s 内确定按钮禁用，0 = 可点击） */
  const [purgeCountdown, setPurgeCountdown] = useState(10);

  /** vLLM 推理引擎状态快照（null = 尚未取到） */
  const [vllmStatus, setVllmStatus] = useState<VllmStatus | null>(null);
  /** vLLM 启动中（POST /models/load 耗时 1-5 分钟） */
  const [vllmStarting, setVllmStarting] = useState(false);
  /** vLLM 停止二次确认弹窗 */
  const [vllmStopOpen, setVllmStopOpen] = useState(false);
  /** vLLM 停止请求进行中 */
  const [vllmStopping, setVllmStopping] = useState(false);

  // 风险弹窗打开期间逐秒倒计时；归零停止
  useEffect(() => {
    if (!purgeTarget || purgeCountdown <= 0) return;
    const t = window.setInterval(() => {
      setPurgeCountdown((c) => Math.max(0, c - 1));
    }, 1000);
    return () => window.clearInterval(t);
  }, [purgeTarget, purgeCountdown]);

  // VRAM 使用率：优先实时遥测（WS system_status），其次协同轮询快照
  const vramUsage = useHardwareStore(
    (s) => s.realtime?.vram_percent ?? s.synergy?.vram?.percent ?? null,
  );

  // 本机硬件画像（GPU 名称 + 显存总量），用于"本机配置推荐"
  const hardwareProfile = useHardwareStore((s) => s.hardwareProfile);
  const gpuInfo = hardwareProfile?.gpu;
  /** 本机显存总量 GB（无 GPU / 未加载为 0） */
  const localVramGb = gpuInfo && gpuInfo.vram_total_mb > 0 ? gpuInfo.vram_total_mb / 1024 : 0;
  /** 系统安全预留（驱动 + 桌面合成开销） */
  const SAFE_RESERVE_GB = 1;
  /** 本机可流畅运行判定（显存充足时） */
  const isRunnable = (m: ModelInfo): boolean =>
    localVramGb > 0 && (m.min_vram_gb || 0) <= localVramGb - SAFE_RESERVE_GB;

  // 首次挂载：拉取模型列表
  useEffect(() => {
    fetchModels();
  }, [fetchModels]);

  /** 仅显示 models/ 目录实际存在的模型（用户裁定） */
  const localModels = models.filter((m) => m.downloaded);

  /* ------------------ 本机配置推荐（硬件画像匹配） ------------------ */

  /** 本机可流畅运行的模型（按显存总量 - 安全预留判定） */
  const runnableModels = localVramGb > 0 ? localModels.filter(isRunnable) : [];
  /** 每类型最佳推荐：本机可运行中显存需求最高（规格最强）的那个 */
  const bestOfType: Partial<Record<ModelType, ModelInfo>> = {};
  for (const type of TYPE_ORDER) {
    const cands = runnableModels.filter((m) => classifyModelType(m) === type);
    if (cands.length > 0) {
      bestOfType[type] = cands.reduce((a, b) => ((b.min_vram_gb || 0) > (a.min_vram_gb || 0) ? b : a));
    }
  }
  /** 点击推荐芯片：切回全部档位并滚动定位到该模型卡片 */
  const jumpToModel = (m: ModelInfo) => {
    setActiveTier('all');
    setSearchQuery('');
    setSelectedId(m.id);
    requestAnimationFrame(() => {
      document.getElementById(`model-card-${m.id}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    });
  };

  /** 搜索过滤（MODEL-004）：名称/ID/用途不区分大小写匹配 */
  const kw = searchQuery.trim().toLowerCase();
  const filteredModels = kw
    ? localModels.filter(
        (m) =>
          displayName(m).toLowerCase().includes(kw) ||
          m.id.toLowerCase().includes(kw) ||
          displayPurpose(m).toLowerCase().includes(kw),
      )
    : localModels;

  /** 排序（MODEL-002）：default 保持后端登记顺序，其余为稳定拷贝排序 */
  const sortModels = (list: ModelInfo[]): ModelInfo[] => {
    if (sortKey === 'default') return list;
    const arr = [...list];
    switch (sortKey) {
      case 'name':
        return arr.sort((a, b) => displayName(a).localeCompare(displayName(b), 'zh-Hans-CN'));
      case 'size_desc':
        return arr.sort((a, b) => (b.size_gb || 0) - (a.size_gb || 0));
      case 'size_asc':
        return arr.sort((a, b) => (a.size_gb || 0) - (b.size_gb || 0));
      case 'vram':
        return arr.sort((a, b) => (a.min_vram_gb || 0) - (b.min_vram_gb || 0));
      default:
        return arr;
    }
  };

  /** 档位 → 类型 → 模型列表（两级分组，搜索过滤后） */
  const tierGroups = new Map<'8' | '12' | '16' | '24', Map<ModelType, ModelInfo[]>>();
  for (const m of filteredModels) {
    const tier = vramTierOf(m);
    const type = classifyModelType(m);
    if (!tierGroups.has(tier)) tierGroups.set(tier, new Map());
    const typeGroups = tierGroups.get(tier)!;
    if (!typeGroups.has(type)) typeGroups.set(type, []);
    typeGroups.get(type)!.push(m);
  }
  tierGroups.forEach((typeGroups) => {
    typeGroups.forEach((list, type) => {
      typeGroups.set(type, sortModels(list));
    });
  });

  /** 选择模型（规格 §14 约束10：手动选择；PUT /v1/models/select） */
  const handleSelect = async (model: ModelInfo) => {
    const { showToast } = useAppStore.getState();
    if (model.status !== 'ready') {
      showToast('模型未就绪，无法选择', 'warning');
      return;
    }
    // 模型类别映射为后端合法功能名（dialog/paint/video/voice）
    const feature = categoryToFeature(model.category);
    if (!feature) {
      showToast('该类别模型不支持手动选择', 'warning');
      return;
    }
    setBusy(model.id);
    try {
      await selectModel(model.id, feature);
      setSelectedId(model.id);
    } catch {
      showToast('选择模型失败', 'error');
    } finally {
      setBusy('');
    }
  };

  /** 校验模型（SHA256 指纹，POST /v1/models/{id}/verify） */
  const handleVerify = async (model: ModelInfo) => {
    setBusy(model.id);
    const { showToast } = useAppStore.getState();
    try {
      const res = await verifyModel(model.id);
      if (res.verified) {
        showToast(`模型校验通过（SHA256：${res.sha256.slice(0, 16)}…）`, 'success');
      } else {
        showToast('模型校验未通过', 'error');
      }
    } catch {
      showToast('校验请求失败', 'error');
    } finally {
      setBusy('');
    }
  };

  /** 卸载+彻底删除模型文件（DELETE /v1/models/{id}/files；10s 倒计时风险确认后执行） */
  const handlePurge = async () => {
    if (!purgeTarget || purgeCountdown > 0) return;
    setPurgeBusy(true);
    const { showToast } = useAppStore.getState();
    try {
      const res = await purgeModelFiles(purgeTarget.id);
      if (selectedId === purgeTarget.id) setSelectedId('');
      showToast(
        `「${displayName(purgeTarget)}」已卸载并彻底删除（释放 ${res.freed_gb}GB 磁盘）`,
        'success',
      );
      setPurgeTarget(null);
      await fetchModels();
    } catch (err) {
      const msg =
        err && typeof err === 'object' && 'message' in err
          ? (err as { message: string }).message
          : '删除失败';
      showToast(msg, 'error');
    } finally {
      setPurgeBusy(false);
    }
  };

  /** 加载模型到 GPU（POST /v1/models/load；错误码 20xxx 由 toast 如实透出） */
  const handleLoad = async (model: ModelInfo) => {
    setBusy(model.id);
    const { showToast } = useAppStore.getState();
    try {
      await loadModel(model.id);
      showToast(`模型「${displayName(model)}」已加载到显存`, 'success');
    } catch (err) {
      const msg =
        err && typeof err === 'object' && 'message' in err
          ? (err as { message: string }).message
          : '加载失败';
      showToast(msg, 'error');
    } finally {
      setBusy('');
    }
  };

  /** 删除模型（DELETE /v1/models/{id}：仅从注册表移除并自动卸载，不删磁盘权重） */
  const handleDelete = async () => {
    if (!deleteTarget) return;
    setDeleteBusy(true);
    const { showToast } = useAppStore.getState();
    try {
      await deleteModel(deleteTarget.id);
      if (selectedId === deleteTarget.id) setSelectedId('');
      showToast(`模型「${displayName(deleteTarget)}」已从注册表移除`, 'success');
      setDeleteTarget(null);
    } catch (err) {
      const msg =
        err && typeof err === 'object' && 'message' in err
          ? (err as { message: string }).message
          : '删除失败';
      showToast(msg, 'error');
    } finally {
      setDeleteBusy(false);
    }
  };

  /** 导入模型（POST /v1/models/import，body: {path}；路径不存在返回 30001） */
  const handleImport = async () => {
    const path = importPath.trim();
    const { showToast } = useAppStore.getState();
    if (!path) {
      showToast('请输入权重文件或目录路径', 'warning');
      return;
    }
    setImportBusy(true);
    try {
      const model = await importModel({ path });
      showToast(`模型「${model.name || model.id}」导入成功`, 'success');
      setImportOpen(false);
      setImportPath('');
      await fetchModels();
    } catch (err) {
      const msg =
        err && typeof err === 'object' && 'message' in err
          ? (err as { message: string }).message
          : '导入失败';
      showToast(msg, 'error');
    } finally {
      setImportBusy(false);
    }
  };

  /* ------------------ vLLM 推理引擎（2026-08-21） ------------------ */

  /** 手动刷新 vLLM 状态（启动 / 停止后调用；失败静默保留上次快照） */
  const refreshVllmStatus = async (): Promise<void> => {
    try {
      setVllmStatus(await getVllmStatus());
    } catch {
      /* silent-intent: 后端未就绪时静默，轮询会自动重试 */
    }
  };

  // vLLM 状态：首拉 + 定时轮询（运行时长 / 健康状态刷新；卸载时清理定时器）
  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      try {
        const s = await getVllmStatus();
        if (!cancelled) setVllmStatus(s);
      } catch {
        /* silent-intent: 后端未就绪时静默 */
      }
    };
    void poll();
    const t = window.setInterval(() => void poll(), VLLM_POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(t);
    };
  }, []);

  /** 启动 vLLM：复用 POST /models/load（qwen3-vl-8b-awq + dialog；模型加载约 1-5 分钟） */
  const handleVllmStart = async () => {
    setVllmStarting(true);
    const { showToast } = useAppStore.getState();
    try {
      await loadModelApi({ model_id: VLLM_MODEL_ID, category: VLLM_MODEL_CATEGORY });
      showToast('vLLM 推理引擎已启动（Qwen3-VL 8B AWQ）', 'success');
      await fetchModels();
    } catch (err) {
      const msg =
        err && typeof err === 'object' && 'message' in err
          ? (err as { message: string }).message
          : 'vLLM 启动失败';
      showToast(msg, 'error');
    } finally {
      setVllmStarting(false);
      void refreshVllmStatus();
    }
  };

  /** 停止 vLLM：POST /models/vllm/stop（二次确认后执行；终止子进程并回收显存） */
  const handleVllmStop = async () => {
    setVllmStopping(true);
    const { showToast } = useAppStore.getState();
    try {
      await stopVllm();
      showToast('vLLM 推理引擎已停止，显存已回收', 'success');
      setVllmStopOpen(false);
      await fetchModels();
    } catch (err) {
      const msg =
        err && typeof err === 'object' && 'message' in err
          ? (err as { message: string }).message
          : 'vLLM 停止失败';
      showToast(msg, 'error');
    } finally {
      setVllmStopping(false);
      void refreshVllmStatus();
    }
  };

  /** vLLM 卡片派生值：徽标 / 按钮可用性 */
  const vllmRunBadge = vllmRunBadgeOf(vllmStatus);
  const vllmBusy = vllmStarting || vllmStopping;
  const vllmStartDisabled =
    vllmBusy || vllmStatus === null || !vllmStatus.runtime_installed || vllmStatus.running;
  const vllmStopDisabled = vllmBusy || vllmStatus === null || !vllmStatus.running;

  /** VRAM 警告（>95% 强制线，规格 COM-012）；P1-05：探测失败为 null，不触发告警 */
  const vramWarning = vramUsage != null && vramUsage >= 95;
  const vramFillClass = vramUsage == null ? '' : vramUsage >= 95 ? 'critical' : vramUsage >= 85 ? 'danger' : '';

  /** 渲染类型小节（二级分组）：图标 + 标签 + 数量 + 卡片列表 */
  const renderTypeSection = (type: ModelType, list: ModelInfo[]): React.ReactNode => {
    if (list.length === 0) return null;
    const Icon = TYPE_ICONS[type];
    return (
      <div key={type} style={{ marginBottom: 'var(--space-3)' }}>
        <h4
          className="card-title"
          style={{
            marginBottom: 'var(--space-2)',
            display: 'flex',
            alignItems: 'center',
            gap: 'var(--space-2)',
            fontSize: 'var(--font-size-sm)',
            color: 'var(--color-text-secondary)',
          }}
        >
          <Icon size={14} aria-hidden="true" />
          {TYPE_LABELS[type]}
          <span style={{ color: 'var(--color-text-tertiary)', fontWeight: 400 }}>（{list.length}）</span>
        </h4>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-2)' }}>
          {list.map((model) => renderModelCard(model))}
        </div>
      </div>
    );
  };

  return (
    <div className="page model-manager">
      <div className="mm-header" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <h1 className="page-title" style={{ marginBottom: 0 }}><Boxes size={20} aria-hidden="true" /> 模型管理</h1>
        <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-4)', flex: 1, marginLeft: 'var(--space-6)' }}>
          {/* VRAM 监控 */}
          <span style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-text-secondary)' }}>VRAM</span>
          <div className="vram-usage-bar" style={{ flex: 1, maxWidth: 320 }}>
            <div className={`vram-usage-fill ${vramFillClass}`} style={{ width: `${vramUsage == null ? 0 : Math.min(100, vramUsage)}%` }} />
            <div className="vram-warning-line" style={{ left: '95%' }} />
          </div>
          <span style={{ fontSize: 'var(--font-size-sm)', minWidth: 40 }}>{vramUsage == null ? '--' : `${Math.round(vramUsage)}%`}</span>
          {vramWarning && (
            <span style={{ color: 'var(--color-error)', display: 'inline-flex', alignItems: 'center', gap: 4 }}>
              <AlertTriangle size={13} aria-hidden="true" /> 强制线
            </span>
          )}
        </div>
        <button className="btn btn-primary btn-sm" onClick={() => setImportOpen(true)}>
          <Import size={14} aria-hidden="true" /> 导入模型
        </button>
      </div>

      {/* 模型就绪总检（体验流 #3）：模块级绿/灰+缺件指路，置顶当验收门面 */}
      <ReadinessBanner />

      {/* 性能基准（MODEL-038，2026-09-17 接线）：跑分 + 历史 + 导出 */}
      <BenchmarkCard />

      {/* 本机配置推荐：按本机 GPU 显存筛选可流畅运行的模型，并给出各类型最佳型号 */}
      <section className="mm-reco" aria-label="本机配置推荐">
        <div className="mm-reco-head">
          <span className="mm-reco-title">
            <Sparkles size={15} aria-hidden="true" /> 本机配置推荐
          </span>
          {localVramGb > 0 ? (
            <span className="mm-reco-gpu">
              <Cpu size={13} aria-hidden="true" />
              {gpuInfo?.name || '本机 GPU'} · 显存 {Math.round(localVramGb)}GB（预留 {SAFE_RESERVE_GB}GB 系统开销）
            </span>
          ) : (
            <span className="mm-reco-gpu">正在读取本机硬件配置…</span>
          )}
        </div>
        {localVramGb > 0 && (
          <p className="mm-reco-desc">
            已为你筛选出 <strong>{runnableModels.length}</strong> / {localModels.length} 个可流畅运行的本地模型；
            点击芯片定位对应卡片，各类型推荐本机可承载的最强规格：
          </p>
        )}
        {localVramGb > 0 && Object.keys(bestOfType).length > 0 && (
          <div className="mm-reco-chips">
            {TYPE_ORDER.filter((t) => bestOfType[t]).map((t) => {
              const best = bestOfType[t]!;
              const Icon = TYPE_ICONS[t];
              return (
                <button
                  key={t}
                  type="button"
                  className="mm-reco-chip"
                  onClick={() => jumpToModel(best)}
                  title={`${TYPE_LABELS[t]} · 本机最佳（需求 ${best.min_vram_gb}GB）`}
                >
                  <Icon size={13} aria-hidden="true" />
                  <span className="mm-reco-chip-type">{TYPE_LABELS[t]}</span>
                  <span className="mm-reco-chip-name">{displayName(best)}</span>
                  <span className="mm-reco-chip-badge">最佳</span>
                </button>
              );
            })}
          </div>
        )}
      </section>

      {/* 功能模块模型配置：三模块（对话/绘画/漫剧视频）当前使用模型 + 集中切换 */}
      <ModuleModelConfig />

      {/* vLLM 推理引擎状态卡（GET /models/vllm/status；启动复用 /models/load，停止走 /models/vllm/stop） */}
      <section className="card" aria-label="vLLM 推理引擎状态" style={{ marginBottom: 'var(--space-3)' }}>
        <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 'var(--space-4)' }}>
          {/* 左：标题 + 运行/健康徽标 + 元信息 */}
          <div style={{ flex: 1, minWidth: 0 }}>
            <h3
              className="card-title"
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 'var(--space-2)',
                flexWrap: 'wrap',
                marginBottom: 'var(--space-2)',
              }}
            >
              <Zap size={16} aria-hidden="true" style={{ color: 'var(--color-primary)' }} />
              vLLM 推理引擎
              <span className={`model-status-badge ${vllmRunBadge.className}`} style={vllmRunBadge.style}>
                {vllmRunBadge.label}
              </span>
              {vllmStatus?.running && (
                <span className={`model-status-badge ${vllmStatus.healthy ? 'reco-ok' : 'reco-over'}`}>
                  {vllmStatus.healthy ? '健康' : '异常'}
                </span>
              )}
            </h3>
            <div
              style={{
                display: 'flex',
                flexWrap: 'wrap',
                gap: 'var(--space-1) var(--space-4)',
                fontSize: 'var(--font-size-sm)',
                color: 'var(--color-text-secondary)',
              }}
            >
              <span>PID {vllmStatus?.running && vllmStatus.pid != null ? String(vllmStatus.pid) : '—'}</span>
              <span>运行时长 {vllmStatus?.running ? formatDuration(vllmStatus.uptime_s) : '—'}</span>
              <span>健康检查 {vllmStatus?.running ? (vllmStatus.healthy ? '通过' : '未通过') : '—'}</span>
              <span title={vllmStatus?.model_dir || undefined}>
                模型目录 {vllmStatus?.model_dir ? truncate(vllmStatus.model_dir, 48) : '—'}
              </span>
              <span>服务端口 {vllmStatus?.port ? String(vllmStatus.port) : '—'}</span>
              <span>服务模型 {vllmStatus?.served_name ? truncate(vllmStatus.served_name, 32) : '—'}</span>
            </div>
            {/* 启动中提示：模型加载需 1-5 分钟 */}
            {vllmStarting && (
              <p
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 'var(--space-2)',
                  margin: 'var(--space-2) 0 0',
                  fontSize: 'var(--font-size-xs)',
                  color: 'var(--color-warning)',
                }}
              >
                <Loader2 size={13} className="animate-spin" aria-hidden="true" />
                正在加载 Qwen3-VL 8B（AWQ）到显存，约需 1-5 分钟，请耐心等待…
              </p>
            )}
            {/* 最近一次错误（title 查看全文） */}
            {vllmStatus?.last_error && (
              <p
                style={{
                  display: 'flex',
                  alignItems: 'flex-start',
                  gap: 4,
                  marginTop: 'var(--space-2)',
                  fontSize: 'var(--font-size-xs)',
                  color: 'var(--color-error)',
                }}
              >
                <AlertTriangle size={12} aria-hidden="true" style={{ flexShrink: 0, marginTop: 2 }} />
                <span title={vllmStatus.last_error}>{truncate(vllmStatus.last_error, 120)}</span>
              </p>
            )}
          </div>
          {/* 右：启动 / 停止（.btn 36px 高，4px 栅格） */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', flexShrink: 0 }}>
            <button
              className="btn btn-primary"
              onClick={handleVllmStart}
              disabled={vllmStartDisabled}
              title={
                vllmStatus != null && !vllmStatus.runtime_installed
                  ? 'vLLM Runtime 未安装，无法启动'
                  : vllmStatus?.running
                    ? '引擎运行中'
                    : `启动 vLLM 并加载 Qwen3-VL 8B（AWQ），约需 1-5 分钟`
              }
            >
              {vllmStarting ? (
                <span style={{ display: 'inline-flex', alignItems: 'center', gap: 'var(--space-2)' }}>
                  <Loader2 size={14} className="animate-spin" aria-hidden="true" /> 启动中…
                </span>
              ) : (
                <span style={{ display: 'inline-flex', alignItems: 'center', gap: 'var(--space-2)' }}>
                  <Zap size={14} aria-hidden="true" /> 启动
                </span>
              )}
            </button>
            <button
              className="btn btn-secondary"
              onClick={() => setVllmStopOpen(true)}
              disabled={vllmStopDisabled}
              title="停止 vLLM 子进程并回收显存（需二次确认）"
              style={{ color: 'var(--color-error)' }}
            >
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: 'var(--space-2)' }}>
                <Power size={14} aria-hidden="true" /> 停止
              </span>
            </button>
          </div>
        </div>
      </section>

      {/* 搜索 + 排序工具栏（MODEL-004 / MODEL-002） */}
      <div style={{ display: 'flex', gap: 'var(--space-3)', marginBottom: 'var(--space-3)', alignItems: 'center' }}>
        <input
          type="search"
          className="mm-search-input"
          placeholder="搜索模型名称 / ID / 用途…"
          aria-label="搜索模型"
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          style={{
            flex: 1,
            maxWidth: 360,
            padding: '6px 10px',
            borderRadius: 'var(--radius-sm)',
            border: '1px solid var(--color-input-border)',
            background: 'var(--color-input-bg)',
            color: 'var(--color-text-primary)',
            fontSize: 'var(--font-size-sm)',
          }}
        />
        <label style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-text-secondary)', display: 'flex', alignItems: 'center', gap: 6 }}>
          排序
          <select
            className="mm-sort-select"
            aria-label="模型排序"
            value={sortKey}
            onChange={(e) => setSortKey(e.target.value as typeof sortKey)}
            style={{
              padding: '6px 8px',
              borderRadius: 'var(--radius-sm)',
              border: '1px solid var(--color-input-border)',
              background: 'var(--color-input-bg)',
              color: 'var(--color-text-primary)',
              fontSize: 'var(--font-size-sm)',
            }}
          >
            <option value="default">默认（登记顺序）</option>
            <option value="name">名称</option>
            <option value="size_desc">大小（从大到小）</option>
            <option value="size_asc">大小（从小到大）</option>
            <option value="vram">最低显存需求</option>
          </select>
        </label>
        {kw && (
          <span style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-text-tertiary)' }}>
            匹配 {filteredModels.length}/{localModels.length}
          </span>
        )}
        <span style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-text-tertiary)', marginLeft: 'auto' }}>
          仅显示 models/ 目录已有模型（{localModels.length}）
        </span>
      </div>

      {/* 显存档位筛选（一级分组） */}
      <div className="model-category-tabs">
        <button
          className={`model-category-tab ${activeTier === 'all' ? 'active' : ''}`}
          onClick={() => setActiveTier('all')}
        >
          <FolderOpen size={14} aria-hidden="true" />
          <span>全部</span>
          <span>（{localModels.length}）</span>
        </button>
        {VRAM_TIERS.map((t) => {
          const count = tierGroups.get(t.key)?.size ?? 0;
          return (
            <button
              key={t.key}
              className={`model-category-tab ${activeTier === t.key ? 'active' : ''}`}
              onClick={() => setActiveTier(t.key)}
            >
              <MemoryStick size={14} aria-hidden="true" />
              <span>{t.label}</span>
              <span>（{count}）</span>
            </button>
          );
        })}
      </div>

      {/* 模型列表：档位（一级） × 类型（二级）两级分组 */}
      {loading && localModels.length === 0 ? (
        <p style={{ color: 'var(--color-text-secondary)' }}>模型列表加载中…</p>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-4)' }}>
          {VRAM_TIERS.filter((t) => activeTier === 'all' || activeTier === t.key).map((t) => {
            const typeGroups = tierGroups.get(t.key);
            if (!typeGroups) return null;
            const tierCount = Array.from(typeGroups.values()).reduce((n, l) => n + l.length, 0);
            return (
              <section key={t.key} className="card">
                <h3
                  className="card-title"
                  style={{
                    marginBottom: 'var(--space-3)',
                    display: 'flex',
                    alignItems: 'center',
                    gap: 'var(--space-2)',
                  }}
                >
                  <MemoryStick size={16} aria-hidden="true" />
                  {t.label}（{tierCount}）
                  <span
                    style={{
                      fontSize: 'var(--font-size-xs)',
                      fontWeight: 400,
                      color: 'var(--color-text-tertiary)',
                    }}
                  >
                    {t.desc}
                  </span>
                </h3>
                {TYPE_ORDER.map((type) => renderTypeSection(type, typeGroups.get(type) || []))}
              </section>
            );
          })}

          {filteredModels.length === 0 && (
            <div
              style={{
                textAlign: 'center',
                padding: 'var(--space-8)',
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                gap: 'var(--space-3)',
              }}
            >
              <span
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  width: 64,
                  height: 64,
                  borderRadius: 'var(--radius-lg)',
                  background: 'rgba(255, 107, 157, 0.1)',
                  color: 'var(--color-primary)',
                }}
              >
                <PackageOpen size={32} strokeWidth={1.5} aria-hidden="true" />
              </span>
              <p style={{ color: 'var(--color-text-secondary)', margin: 0 }}>
                {kw
                  ? `无匹配「${searchQuery.trim()}」的本地模型`
                  : activeTier !== 'all'
                    ? `该档位暂无本地模型`
                    : 'models/ 目录暂无可用模型'}
              </p>
              <p style={{ color: 'var(--color-text-tertiary)', fontSize: 'var(--font-size-xs)', margin: 0 }}>
                点击右上角「导入模型」将本地模型目录接入系统，或调整筛选与搜索条件
              </p>
            </div>
          )}
        </div>
      )}

      {/* VRAM 强制线警告 */}
      {vramWarning && (
        <div style={{ padding: 'var(--space-3)', borderRadius: 'var(--radius-sm)', background: 'var(--color-error)', color: 'var(--color-text-primary)', display: 'flex', alignItems: 'center', gap: 'var(--space-2)' }}>
          <AlertTriangle size={15} aria-hidden="true" />
          VRAM 使用率已达 {Math.round(vramUsage)}%，超过 95% 强制线！请卸载不使用的模型释放显存。
        </div>
      )}

      {/* 导入模型弹窗（POST /v1/models/import，后端仅消费 path 字段） */}
      {importOpen && (
        <Modal
          title="导入模型"
          onClose={() => !importBusy && setImportOpen(false)}
          footer={
            <>
              <button
                className="btn btn-secondary btn-sm"
                onClick={() => setImportOpen(false)}
                disabled={importBusy}
              >
                取消
              </button>
              <button
                className="btn btn-primary btn-sm"
                onClick={handleImport}
                disabled={importBusy || !importPath.trim()}
              >
                {importBusy ? '导入中…' : '导入'}
              </button>
            </>
          }
        >
          <div className="form-row">
            <label className="form-label" htmlFor="mm-import-path">权重文件或目录路径</label>
            {/* MODEL-013：拖拽区。浏览器安全沙箱不暴露被拖目录的绝对路径，
                故拖拽仅自动带入名称并保留用户已填的路径前缀，如实提示，不伪造路径 */}
            <div
              className="mm-dropzone"
              onDragOver={(e) => {
                e.preventDefault();
                e.dataTransfer.dropEffect = 'copy';
              }}
              onDrop={(e) => {
                e.preventDefault();
                let name = '';
                const item = e.dataTransfer.items?.[0];
                const entry = item?.webkitGetAsEntry?.();
                if (entry) name = entry.name;
                else if (e.dataTransfer.files.length > 0) name = e.dataTransfer.files[0].name;
                if (!name) return;
                setImportPath((prev) => {
                  const prefix = /^([A-Za-z]:[\\/].*[\\/]|models[\\/])/.exec(prev);
                  return prefix ? prefix[1] + name : `models\\${name}`;
                });
                useAppStore.getState().showToast(
                  '已带入拖入项名称；浏览器沙箱无法读取绝对路径，请确认路径前缀',
                  'info',
                );
              }}
              style={{
                border: '1px dashed var(--color-input-border)',
                borderRadius: 'var(--radius-sm)',
                padding: 'var(--space-3)',
                textAlign: 'center',
                fontSize: 'var(--font-size-sm)',
                color: 'var(--color-text-tertiary)',
                marginBottom: 'var(--space-2)',
              }}
            >
              将模型目录 / 权重文件拖到此处（自动带入名称，默认前缀 models\）
            </div>
            <input
              id="mm-import-path"
              className="input"
              placeholder="如 models/qwen3-vl-4b 或任意路径的 model.safetensors"
              value={importPath}
              disabled={importBusy}
              onChange={(e) => setImportPath(e.target.value)}
            />
            <div className="form-hint">
              相对路径以项目根目录解析；导入时自动识别类别、大小与显存需求，导入后立即出现在列表。对话类模型（GGUF / transformers / AWQ 量化）可直接加载使用；文件保留在原路径，不会复制占用磁盘。
            </div>
          </div>
        </Modal>
      )}

      {/* 删除确认弹窗（DELETE /v1/models/{id}：仅移除注册表记录，不删磁盘权重） */}
      {deleteTarget && (
        <Modal
          title="删除模型"
          onClose={() => !deleteBusy && setDeleteTarget(null)}
          footer={
            <>
              <button
                className="btn btn-secondary btn-sm"
                onClick={() => setDeleteTarget(null)}
                disabled={deleteBusy}
              >
                取消
              </button>
              <button
                className="btn btn-primary btn-sm"
                onClick={handleDelete}
                disabled={deleteBusy}
              >
                {deleteBusy ? '删除中…' : '确认删除'}
              </button>
            </>
          }
        >
          <p className="text-sm">
            确认从模型注册表移除「{displayName(deleteTarget)}」？
            {deleteTarget.loaded ? '该模型当前已加载，删除前将自动卸载。' : ''}
          </p>
          <p className="text-tertiary mt-2" style={{ fontSize: 'var(--font-size-xs)' }}>
            此操作仅移除注册表记录，不会删除磁盘上的权重文件。
          </p>
        </Modal>
      )}

      {/* 卸载+彻底删除风险弹窗（确定按钮 10s 倒计时解锁） */}
      {purgeTarget && (
        <Modal
          title="风险警告：卸载并彻底删除模型"
          onClose={() => !purgeBusy && setPurgeTarget(null)}
          footer={
            <>
              <button
                className="btn btn-secondary btn-sm"
                onClick={() => setPurgeTarget(null)}
                disabled={purgeBusy}
              >
                取消
              </button>
              <button
                className="btn btn-danger btn-sm"
                onClick={handlePurge}
                disabled={purgeBusy || purgeCountdown > 0}
              >
                {purgeBusy
                  ? '删除中…'
                  : purgeCountdown > 0
                    ? `请确认风险（${purgeCountdown}s）`
                    : '确定删除'}
              </button>
            </>
          }
        >
          <p className="text-sm">
            即将卸载并<strong style={{ color: 'var(--color-error)' }}>彻底删除</strong>
            「{displayName(purgeTarget)}」的全部模型文件（约 {purgeTarget.size_gb.toFixed(1)}GB）。
          </p>
          <ul
            className="mt-2"
            style={{
              fontSize: 'var(--font-size-xs)',
              color: 'var(--color-text-tertiary)',
              listStyle: 'disc',
              paddingLeft: 18,
            }}
          >
            <li>文件将从磁盘永久删除，<strong>不可恢复</strong>，需要时须重新下载</li>
            <li>该模型当前已加载时将先自动卸载释放显存</li>
            <li>依赖此模型的功能（对话/绘画/视频等）将不可用，直至重新导入</li>
          </ul>
        </Modal>
      )}

      {/* vLLM 停止二次确认弹窗（POST /v1/models/vllm/stop：终止子进程并回收显存） */}
      {vllmStopOpen && (
        <Modal
          title="停止 vLLM 推理引擎"
          onClose={() => !vllmStopping && setVllmStopOpen(false)}
          footer={
            <>
              <button
                className="btn btn-secondary"
                onClick={() => setVllmStopOpen(false)}
                disabled={vllmStopping}
              >
                取消
              </button>
              <button
                className="btn btn-danger"
                onClick={handleVllmStop}
                disabled={vllmStopping}
              >
                {vllmStopping ? '停止中…' : '确认停止'}
              </button>
            </>
          }
        >
          <p className="text-sm">
            确认停止 vLLM 推理服务？将终止推理子进程
            {vllmStatus?.pid != null ? `（PID ${vllmStatus.pid}）` : ''}
            并回收显存。
          </p>
          <p className="text-tertiary mt-2" style={{ fontSize: 'var(--font-size-xs)' }}>
            正在进行的推理请求会立即中断；依赖 Qwen3-VL 8B 的对话等功能将不可用，直至重新启动引擎。
          </p>
        </Modal>
      )}
    </div>
  );

  /** 渲染单个模型卡片 */
  function renderModelCard(model: ModelInfo): React.ReactNode {
    const isSelected = selectedId === model.id;
    const isBusy = busy === model.id;
    const statusKey = model.loaded ? 'loaded' : model.status;
    const statusText = model.loaded ? '已加载' : (MODEL_RUNTIME_STATUS_LABELS[model.status] || model.status);
    // 本机配置推荐标记
    const runnable = isRunnable(model);
    const isBest = Object.values(bestOfType).some((b) => b?.id === model.id);
    return (
      <div id={`model-card-${model.id}`} key={model.id} className={`model-card ${isSelected ? 'selected' : ''}`}>
        <div className="model-info">
          <div className="model-name">
            {displayName(model)}
            {' '}
            <span className={`model-status-badge ${statusKey}`}>{statusText}</span>
            {isBest && <span className="model-status-badge reco-best">本机最佳</span>}
            {runnable && !isBest && <span className="model-status-badge reco-ok">本机推荐</span>}
            {!runnable && localVramGb > 0 && (
              <span className="model-status-badge reco-over" title={`需求 ${model.min_vram_gb}GB > 本机可用 ${Math.max(0, Math.round(localVramGb - SAFE_RESERVE_GB))}GB`}>
                超出本机显存
              </span>
            )}
            {isSelected && <span className="model-status-badge selected">已选择</span>}
            {model.purpose === '用户导入' && (
              <span className="model-status-badge reco-ok" title="经「导入模型」登记的外部路径模型，文件保留在原位置">用户导入</span>
            )}
          </div>
          <div className="model-meta">
            <span>{displayPurpose(model)}</span>
            <span>大小 {model.size_gb ? `${model.size_gb.toFixed(1)} GB` : '—'}</span>
            <span>参数量 {displayParams(model)}</span>
            <span>最低显存 {model.min_vram_gb ? `${model.min_vram_gb} GB` : '—'}</span>
          </div>
          {/* 输入/输出能力行：文字+图片 → 视频（2026-08-20 用户需求） */}
          {(() => {
            const io = modelIO(model);
            const renderModes = (modes: IOMode[], prefix: string) =>
              modes.map((mode) => {
                const meta = IO_MODE_META[mode];
                const Icon = meta.icon;
                return (
                  <span
                    key={`${prefix}-${mode}`}
                    style={{
                      display: 'inline-flex',
                      alignItems: 'center',
                      gap: 3,
                      padding: '1px 6px',
                      borderRadius: 4,
                      fontSize: 'var(--font-size-xs)',
                      background: 'var(--color-bg-secondary, var(--color-input-bg))',
                      color: 'var(--color-text-secondary)',
                    }}
                  >
                    <Icon size={11} aria-hidden="true" />
                    {meta.label}
                  </span>
                );
              });
            return (
              <div
                className="model-meta"
                style={{ alignItems: 'center', marginTop: 4 }}
                title="该模型支持的输入模态 → 输出模态"
              >
                <span
                  style={{
                    display: 'inline-flex',
                    alignItems: 'center',
                    gap: 4,
                    color: 'var(--color-text-tertiary)',
                    flexShrink: 0,
                  }}
                >
                  <ArrowRight size={11} aria-hidden="true" style={{ transform: 'rotate(180deg)' }} />
                  支持
                </span>
                {renderModes(io.inputs, 'in')}
                <ArrowRight size={12} aria-hidden="true" style={{ color: 'var(--color-primary)', flexShrink: 0 }} />
                {renderModes(io.outputs, 'out')}
              </div>
            );
          })()}
          {/* 大白话功能说明：用一句话讲清这模型能替你干什么 */}
          <div
            style={{
              marginTop: 4,
              fontSize: 'var(--font-size-xs)',
              color: 'var(--color-text-secondary)',
              display: 'flex',
              alignItems: 'flex-start',
              gap: 4,
            }}
            title="通俗解释：这个模型能替你做什么"
          >
            <MessageSquare
              size={11}
              aria-hidden="true"
              style={{
                flexShrink: 0,
                marginTop: 2,
                color: 'var(--color-primary)',
              }}
            />
            <span>{plainSpeech(model)}</span>
          </div>
        </div>
        <div className="model-actions">
          {/* 选择按钮（规格 §14 约束10：手动选择；3d/auxiliary 类别无对应功能，不可选） */}
          <button
            className={`btn btn-sm ${isSelected ? 'btn-primary' : 'btn-secondary'}`}
            onClick={() => handleSelect(model)}
            disabled={isBusy || model.status !== 'ready' || !categoryToFeature(model.category)}
            title={
              !categoryToFeature(model.category)
                ? '该类别不支持手动选择'
                : model.status === 'ready'
                  ? '选择此模型'
                  : '模型未就绪'
            }
          >
            {isSelected ? (
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}><Check size={13} aria-hidden="true" /> 已选择</span>
            ) : (
              '选择'
            )}
          </button>

          {/* 加载（POST /v1/models/load；未下载/已加载置灰） */}
          <button
            className="btn btn-secondary btn-sm"
            onClick={() => handleLoad(model)}
            disabled={isBusy || model.loaded || !model.downloaded}
            title={
              model.loaded
                ? '模型已加载'
                : !model.downloaded
                  ? '模型未下载，无法加载'
                  : '加载模型到显存'
            }
          >
            加载
          </button>

          {/* 校验 */}
          <button
            className="btn btn-secondary btn-sm"
            onClick={() => handleVerify(model)}
            disabled={isBusy || !model.downloaded}
            title="校验模型完整性"
          >
            校验
          </button>

          {/* 卸载（卸载+彻底删除磁盘文件；风险弹窗 10s 倒计时确认） */}
          <button
            className="btn btn-secondary btn-sm"
            onClick={() => {
              setPurgeCountdown(10);
              setPurgeTarget(model);
            }}
            disabled={isBusy || !model.downloaded}
            title="卸载并彻底删除模型文件（不可恢复）"
          >
            卸载
          </button>

          {/* 删除（DELETE /v1/models/{id}，仅移除注册表记录，弹窗确认） */}
          <button
            className="btn btn-secondary btn-sm"
            onClick={() => setDeleteTarget(model)}
            disabled={isBusy}
            title="从注册表移除（不删除磁盘权重文件）"
            style={{ color: 'var(--color-error)' }}
          >
            删除
          </button>
        </div>
      </div>
    );
  }
};

export default ModelManager;
