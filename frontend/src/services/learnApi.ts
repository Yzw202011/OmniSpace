/* ==========================================================================
 * OmniSpace AI v2.3.1 —— 知识学习 API（对齐后端 /v1/learn/* 实际契约）
 * --------------------------------------------------------------------------
 * 已对接（后端 src/api/learn.py 实际存在）：
 * - POST /learn/train            发起训练（TrainTaskCreate：base_model/超参数/dataset_path）
 * - GET  /learn/tasks            训练任务列表（创建时间倒序，{items,total}）
 * - GET  /learn/tasks/{id}       训练任务详情
 * - POST /learn/dataset/upload   上传训练数据集（multipart，JSONL/JSON/TXT）
 * - GET  /learn/models           可训练基础模型（{items,total}）
 * - GET  /learn/training/status  训练服务状态（能力面板数据源）
 * 待后端补齐（前端保留调用点，失败时由调用方静默/降级处理）：
 * - POST /learn/tasks/{id}/cancel  取消训练任务
 * - POST /learn/tasks/reorder      拖拽重排优先级
 * - POST /learn/points             手动新建知识点
 * - POST /learn/urls / GET /learn/urls  学习 URL 管理
 * - POST /learn/crawl /learn/auto  网页爬取 / 自动学习
 * ========================================================================== */

import { get, post, del, upload } from './api';
import type { QueryParams } from './api';
import type {
  TrainTask,
  TrainType,
  TrainPriority,
  Paginated,
} from '@/types';

/* ------------------------------ 契约规整 ------------------------------ */

/** 后端 train_tasks 行（/learn/train 与 /learn/tasks 返回的原始结构） */
interface RawTrainTask {
  id: string;
  base_model?: string;
  lora_rank?: number;
  lora_alpha?: number;
  learning_rate?: number;
  epochs?: number;
  dataset_path?: string;
  /** queued / training / evaluating / done / error */
  status?: string;
  /** 0~1（兼容 0~100 百分制） */
  progress?: number;
  created_at?: string | number;
  updated_at?: string | number;
}

/** 后端训练状态 → 前端 TrainTask.status */
const TRAIN_STATUS_MAP: Record<string, TrainTask['status']> = {
  queued: 'pending',
  training: 'running',
  evaluating: 'running',
  done: 'done',
  error: 'error',
  cancelled: 'cancelled',
};

/** 后端训练任务行 → 前端 TrainTask（fallback 用于保留发起时的本地语义字段） */
function normalizeTrainTask(
  raw: RawTrainTask,
  fallback?: { type?: TrainType; name?: string; priority?: TrainPriority },
): TrainTask {
  const rawProgress = typeof raw.progress === 'number' ? raw.progress : 0;
  return {
    id: String(raw.id ?? ''),
    type: fallback?.type ?? 'knowledge_lora',
    name: fallback?.name ?? raw.base_model ?? '训练任务',
    status: TRAIN_STATUS_MAP[String(raw.status ?? 'queued')] ?? 'pending',
    progress: rawProgress > 1 ? rawProgress / 100 : rawProgress,
    priority: fallback?.priority ?? 'auto',
    target_model: raw.base_model,
    dataset: raw.dataset_path || undefined,
    hyperparams: {
      lora_rank: raw.lora_rank,
      lora_alpha: raw.lora_alpha,
      learning_rate: raw.learning_rate,
      epochs: raw.epochs,
    },
    created_at: raw.created_at ?? Date.now(),
    updated_at: raw.updated_at,
  };
}

/* ------------------------------ 训练任务 ------------------------------ */

/** 发起训练请求体（前端语义字段，提交前映射为后端 TrainTaskCreate） */
export interface CreateTrainBody {
  type: TrainType;
  name?: string;
  /** 手动优先级 */
  priority?: TrainPriority;
  /** 关联模型（映射为后端 base_model；缺省用默认可训练基座） */
  target_model?: string;
  /** 训练数据集路径（映射为后端 dataset_path） */
  dataset?: string;
  /** 超参数（lora_rank/lora_alpha/learning_rate/epochs 透传，其余忽略） */
  hyperparams?: Record<string, unknown>;
}

/** 后端默认可训练基座（与 src/api/learn.py _TRAINABLE_BASE_MODELS 对齐） */
const DEFAULT_BASE_MODEL = 'qwen3-vl-4b';

/** 发起训练（阈值校验 + 引擎就绪门控；数据不足 40009 / GPU 占用 40007） */
export async function createTrain(body: CreateTrainBody): Promise<TrainTask> {
  const hp = body.hyperparams || {};
  const payload = {
    base_model: body.target_model || DEFAULT_BASE_MODEL,
    lora_rank: Number(hp.lora_rank) || 16,
    lora_alpha: Number(hp.lora_alpha) || 32,
    learning_rate: Number(hp.learning_rate) || 1e-4,
    epochs: Number(hp.epochs) || 3,
    dataset_path: body.dataset || '',
  };
  const raw = await post<RawTrainTask>('/learn/train', payload);
  return normalizeTrainTask(raw, {
    type: body.type,
    name: body.name,
    priority: body.priority,
  });
}

/** 训练任务队列（后端按创建时间倒序；query 参数后端忽略，仅前端语义保留） */
export async function listTasks(query?: QueryParams): Promise<Paginated<TrainTask>> {
  const res = await get<Paginated<RawTrainTask>>('/learn/tasks', query);
  return {
    ...res,
    items: (res.items || []).map((r) => normalizeTrainTask(r)),
  };
}

/** 训练任务详情 */
export async function getTask(taskId: string): Promise<TrainTask> {
  const raw = await get<RawTrainTask>(`/learn/tasks/${taskId}`);
  return normalizeTrainTask(raw);
}

/** 取消训练任务 */
export function cancelTask(taskId: string) {
  return post<TrainTask>(`/learn/tasks/${taskId}/cancel`, {});
}

/** 删除训练任务记录（仅终态 done/error/cancelled 可删，2026-09-05 用户需求） */
export function deleteTask(taskId: string) {
  return del<{ deleted: string }>(`/learn/tasks/${taskId}`);
}

/** 拖拽重排优先级（待后端补齐端点） */
export function reorderTasks(taskIds: string[]) {
  return post<void>('/learn/tasks/reorder', { task_ids: taskIds });
}

/* ------------------------------ 数据集上传 ------------------------------ */

/** 上传训练数据集（multipart；后端落盘 data/training/uploads/ 并返回 dataset_path） */
export async function uploadDataset(
  file: File,
  meta?: { type?: TrainType },
): Promise<{ path: string; size_mb?: number; count?: number }> {
  const formData = new FormData();
  formData.append('file', file);
  if (meta?.type) {
    formData.append('type', meta.type);
  }
  const res = await upload<{
    id: string;
    filename: string;
    dataset_path: string;
    size_bytes: number;
    uploaded_at: number;
  }>('/learn/dataset/upload', formData);
  return {
    path: res.dataset_path,
    size_mb: Math.round((res.size_bytes / (1024 * 1024)) * 100) / 100,
  };
}

/* ------------------------------ 学习模型/能力面板 ------------------------------ */

/** 可训练基础模型（后端 /learn/models 返回基座清单，规整为前端语义） */
export async function listLearnModels(): Promise<
  Array<{
    name: string;
    type: TrainType;
    ready: boolean;
    current_count: number;
    threshold: number;
    description?: string;
  }>
> {
  const res = await get<{
    items: Array<{
      id: string;
      name: string;
      category: string;
      params?: string;
      size_gb?: number;
      trainable?: boolean;
    }>;
    total: number;
  }>('/learn/models');
  return (res.items || []).map((m) => ({
    name: m.name,
    // 基座模型不携带 LoRA 类型语义，填默认占位（调用方按 name/ready 使用）
    type: 'knowledge_lora' as TrainType,
    ready: Boolean(m.trainable),
    current_count: 0,
    threshold: 0,
    description: [m.params, m.size_gb ? `${m.size_gb}GB` : '', m.category]
      .filter(Boolean)
      .join(' / '),
  }));
}

/** 能力面板（数据源 GET /learn/training/status，字段容忍缺省） */
export async function getCapability(): Promise<{
  engines: Record<string, boolean>;
  chromadb: boolean;
  thresholds: Record<string, { current: number; threshold: number }>;
}> {
  const raw = await get<Record<string, unknown>>('/learn/training/status');
  return {
    engines: (raw?.engines as Record<string, boolean>) || {},
    chromadb: Boolean(raw?.chromadb),
    thresholds:
      (raw?.thresholds as Record<string, { current: number; threshold: number }>) ||
      {},
  };
}

/* ------------------------------ 知识点/URL/爬取（待后端补齐） ------------------------------ */

/** 手动新建知识点（待后端补齐端点） */
export function createPoint(body: {
  title?: string;
  content: string;
  source?: string;
  type?: string;
}) {
  return post<{ id: string; auto_train?: boolean; task_id?: string }>(
    '/learn/points',
    body,
  );
}

/** 添加学习 URL（待后端补齐端点） */
export function addUrl(body: { url: string; title?: string }) {
  return post<{ id: string }>('/learn/urls', body);
}

/** URL 列表（待后端补齐端点） */
export function listUrls() {
  return get<Paginated<{ id: string; url: string; title?: string; status?: string }>>(
    '/learn/urls',
  );
}

/** 网页爬取（待后端补齐端点） */
export function crawl(body: { url: string }) {
  return post<{ task_id: string }>('/learn/crawl', body);
}

/** 自动学习（待后端补齐端点） */
export function autoLearn(body: { url: string; max_depth?: number; max_pages?: number }) {
  return post<{ task_id: string; pages?: number }>('/learn/auto', body);
}

export default {
  createTrain,
  listTasks,
  getTask,
  cancelTask,
  reorderTasks,
  uploadDataset,
  listLearnModels,
  getCapability,
  createPoint,
  addUrl,
  listUrls,
  crawl,
  autoLearn,
};
// 本项目仅供学习使用，商业授权请+Q 3559331368
