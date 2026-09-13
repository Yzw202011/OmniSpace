/* ==========================================================================
 * OmniSpace AI v2.1 —— 对话 API（规格 §4.1 对话端点）
 * --------------------------------------------------------------------------
 * - POST   /chat/send                 SSE 流式发送（legacy 事件序列）
 * - POST   /chat/completions          SSE 流式发送（OpenAI 风格）
 * - POST   /chat/stop                 停止指定会话进行中的流式生成
 * - GET    /chat/sessions             会话列表（{items,total}）
 * - GET    /chat/sessions/{id}        会话详情+全部消息
 * - GET    /chat/sessions/{id}/messages  会话消息列表
 * - PUT    /chat/sessions/{id}        重命名/置顶/模式
 * - DELETE /chat/sessions/{id}        删除会话及消息
 * - POST   /chat/sessions/{id}/messages/{mid}/rating    赞/踩评分
 * - POST   /chat/sessions/{id}/messages/{mid}/favorite  收藏切换
 * - GET    /chat/favorites            收藏夹列表
 * - POST   /chat/export               导出 Markdown/TXT
 * - DELETE /chat/sessions/{id}/messages  清空历史保留会话
 * 诚实门控：SSE 流内 meta/error 如实返回，未就绪返回 40002，绝不伪造 AI 回复。
 * ========================================================================== */

import { get, post, put, del, upload } from './api';
import type { QueryParams } from './api';
import { parseWith, DialogSessionListRespSchema } from './schema';
import type {
  DialogMessage,
  DialogSession,
  Paginated,
} from '@/types';

/* ------------------------------ 会话 CRUD ------------------------------ */

/** 预热对话模型（进入对话页后台加载，fire-and-forget，失败静默）。
 *  modelId 透传用户持久化选择（2026-08-23 幽灵热切换修复：与
 *  /models/warmup 共享 inflight 去重，目标不一致会把先载好的模型
 *  热切换到引擎默认（显存紧时 GGUF 0.5B）再换回，双倍加载耗时） */
export function prewarmModel(modelId?: string): void {
  void post<{ state: string; prewarmed: boolean }>(
    '/dialog/prewarm', modelId ? { model_id: modelId } : {},
  ).catch(() => { /* 预热失败不打扰用户 */ });
}

/** 对话引擎状态快照（冷启动弹窗轮询就绪信号） */
export interface DialogEngineStatus {
  state: string;        // unavailable/unloaded/ready/error
  model: string | null;
  backend: string;      // vl/text/gguf/vllm
}

/** 查询对话引擎状态（预热进度弹窗 2s 轮询） */
export function getDialogEngineStatus() {
  return get<DialogEngineStatus>('/dialog/status');
}

/** 会话列表查询参数 */
export interface SessionListQuery extends QueryParams {
  /** 标题/消息内容搜索关键词 */
  keyword?: string;
  /** 页码 */
  page?: number;
  /** 每页数量 */
  page_size?: number;
}

/** 创建会话请求体 */
export interface CreateSessionBody {
  title?: string;
  mode?: string;
}

/** 更新会话请求体（重命名/置顶/模式） */
export interface UpdateSessionBody {
  title?: string;
  pinned?: boolean;
  mode?: string;
}

/** 获取会话列表（批 3-3c：响应过 Zod，非法抛 FRONTEND_PARSE_ERROR 走三分法） */
export function listSessions(query?: SessionListQuery) {
  return get<unknown>('/chat/sessions', query).then(
    (d) => parseWith(DialogSessionListRespSchema, d, '会话列表') as unknown as Paginated<DialogSession>,
  );
}

/** 获取会话详情（含全部消息） */
export function getSession(sessionId: string) {
  return get<DialogSession & { messages: DialogMessage[] }>(
    `/chat/sessions/${sessionId}`,
  );
}

/** 获取会话消息列表 */
export function listMessages(
  sessionId: string,
  query?: QueryParams,
) {
  return get<Paginated<DialogMessage>>(
    `/chat/sessions/${sessionId}/messages`,
    query,
  );
}

/** 创建会话 */
export function createSession(body?: CreateSessionBody) {
  return post<DialogSession>('/chat/sessions', body ?? {});
}

/** 更新会话（重命名/置顶/模式） */
export function updateSession(sessionId: string, body: UpdateSessionBody) {
  return put<DialogSession>(`/chat/sessions/${sessionId}`, body);
}

/** 删除会话及消息 */
export function deleteSession(sessionId: string) {
  return del<void>(`/chat/sessions/${sessionId}`);
}

/** 对话可用模型项（GET /dialog/models） */
export interface DialogModelInfo {
  model_id: string;
  name: string;
  size_label: string;
  est_vram_gb: number;
  fits_local: boolean;
  loaded: boolean;
  /** 是否支持图片理解（多模态）；缺省 true 兼容旧后端（2026-09-07） */
  vision?: boolean;
}

/** 拉取对话可用模型清单（含可承载判定与已加载标记） */
export function getDialogModels() {
  return get<{ models: DialogModelInfo[]; total_vram_gb: number }>(
    '/dialog/models');
}

/** 批量删除会话（POST /chat/sessions/batch-delete，单批 ≤100）
 *  返回 {deleted, deleted_ids, missing_ids}，幂等：不存在的不报错 */
export function batchDeleteSessions(sessionIds: string[]) {
  return post<{ deleted: number; deleted_ids: string[]; missing_ids: string[] }>(
    '/chat/sessions/batch-delete', { session_ids: sessionIds });
}

/** 清空历史保留会话 */
export function clearMessages(sessionId: string) {
  return del<void>(`/chat/sessions/${sessionId}/messages`);
}

/* ------------------------------ 消息评分/收藏 ------------------------------ */

/** 消息评分（1 赞 / -1 踩 / 0 取消） */
export function rateMessage(
  sessionId: string,
  messageId: string,
  rating: number,
) {
  return post<void>(
    `/chat/sessions/${sessionId}/messages/${messageId}/rating`,
    { rating },
  );
}

/** 收藏切换 */
export function toggleFavorite(sessionId: string, messageId: string) {
  return post<DialogMessage>(
    `/chat/sessions/${sessionId}/messages/${messageId}/favorite`,
    {},
  );
}

/** 收藏夹列表 */
export function listFavorites(query?: QueryParams) {
  return get<Paginated<DialogMessage>>('/chat/favorites', query);
}

/* ------------------------------ 发送/停止 ------------------------------ */

/** 发送消息请求体 */
export interface SendMessageBody {
  session_id: string;
  /** 用户输入文本 */
  content: string;
  /** 关联图片（多模态上传后的路径/ID） */
  images?: string[];
  /** 对话模式 */
  mode?: string;
  /** 流式开关 */
  stream?: boolean;
}

/**
 * 发送消息（非流式）。
 * 流式发送请使用 dialogStreamStore 中的 WebSocket 通道或 SSE 封装。
 */
export function sendMessage(body: SendMessageBody) {
  return post<DialogMessage>('/chat/send', body);
}

/** 停止指定会话进行中的流式生成 */
export function stopGenerate(sessionId: string) {
  return post<void>('/chat/stop', { session_id: sessionId });
}

/* ------------------------- 文档附件解析（2026-09-07） ------------------------- */

/**
 * 上传文档解析为纯文本（txt/md/docx/pdf；.doc 老格式后端诚实拒绝）。
 * 后端复用知识库导入的解析管线（python-docx / pymupdf + 魔数嗅验）。
 */
export function parseDocument(file: File) {
  const formData = new FormData();
  formData.append('file', file);
  return upload<{ name: string; text: string; chars: number; kind: string; truncated: boolean }>(
    '/dialog/parse-document',
    formData,
  );
}

/* B3（2026-09-13）反向死链清退：原 submitCorrection（/chat/corrections）
 * 与 exportSession（/chat/export）删除——后端无这些路由（技术债
 * R3-ARCH8），调用必 404（信封语义）；组件层零调用。后端补齐对话
 * 纠正闭环 / 会话导出时按实际契约重建。 */

export default {
  listSessions,
  getSession,
  listMessages,
  createSession,
  updateSession,
  deleteSession,
  batchDeleteSessions,
  clearMessages,
  rateMessage,
  toggleFavorite,
  listFavorites,
  sendMessage,
  stopGenerate,
  parseDocument,
};
// 本项目仅供学习使用，商业授权请+Q 3559331368
