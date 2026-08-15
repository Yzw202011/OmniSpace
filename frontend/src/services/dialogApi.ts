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

import { get, post, put, del } from './api';
import type { QueryParams } from './api';
import type {
  DialogMessage,
  DialogSession,
  Paginated,
} from '@/types';

/* ------------------------------ 会话 CRUD ------------------------------ */

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

/** 获取会话列表 */
export function listSessions(query?: SessionListQuery) {
  return get<Paginated<DialogSession>>('/chat/sessions', query);
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

/* ------------------------------ 纠正/导出 ------------------------------ */

/**
 * 纠正入库（§2.2 对话闭环 RAG 底座）
 * @deprecated 后端端点未实现（技术债 R3-ARCH8），调用将返回 SYSTEM_RESOURCE_NOT_FOUND；待后端补齐或移除
 */
export function submitCorrection(body: {
  session_id: string;
  message_id?: string;
  original: string;
  correction: string;
}) {
  return post<void>('/chat/corrections', body);
}

/** 导出格式 */
export type ExportFormat = 'markdown' | 'txt';

/**
 * 导出会话为 Markdown/TXT
 * @deprecated 后端端点未实现（技术债 R3-ARCH8），调用将返回 SYSTEM_RESOURCE_NOT_FOUND；待后端补齐或移除
 */
export function exportSession(body: {
  session_id: string;
  format?: ExportFormat;
}) {
  return post<{ url: string; format: ExportFormat }>('/chat/export', body);
}

export default {
  listSessions,
  getSession,
  listMessages,
  createSession,
  updateSession,
  deleteSession,
  clearMessages,
  rateMessage,
  toggleFavorite,
  listFavorites,
  sendMessage,
  stopGenerate,
  submitCorrection,
  exportSession,
};
