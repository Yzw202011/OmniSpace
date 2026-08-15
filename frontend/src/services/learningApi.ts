/* ==========================================================================
 * OmniSpace AI v2.3 —— 知识学习 v2.3 API（TASK-036 / 后端 v2.3 约定）
 * --------------------------------------------------------------------------
 * 端点（统一信封 {code,message,data}，code!==0 时 api 层抛 ApiError）：
 *   学习主题：  POST /learn/topic/create、GET /learn/topic/list、DELETE /learn/topic/delete
 *   学习会话：  POST /learn/session/start|pause|resume|stop
 *              GET  /learn/session/status|logs|report
 *   学习设置：  GET /learn/settings、PUT /learn/settings
 *   浏览器：    GET /browser/status|screenshot|current-page|tabs
 *              POST /browser/navigate|takeover|handback
 *   知识库：    GET /knowledge/stats|list、GET/DELETE /knowledge/{id}
 *              POST /knowledge/import-document（multipart）
 *   行为学习：  POST /behavior/event、GET /behavior/stats、POST /behavior/clear
 * 说明：路径前缀 /v1 由 services/api.ts 的 API_BASE 提供。
 * ========================================================================== */

import { get, post, put, del, upload, request } from './api';
import type { QueryParams } from './api';

/* ------------------------------ 类型定义 ------------------------------ */

/** 学习主题 */
export interface LearnTopic {
  id: string;
  name: string;
  /** 进度 0~1 */
  progress?: number;
  /** 已提取知识点数 */
  knowledge_count?: number;
  /** 已浏览页数 */
  pages_visited?: number;
  /** 状态 */
  status?: 'pending' | 'learning' | 'paused' | 'done';
  created_at?: string | number;
}

/** 学习会话状态（GET /learn/session/status） */
export interface LearnSessionStatus {
  /** 会话 ID */
  session_id?: string;
  /** 状态：idle / running / paused / stopped */
  status: 'idle' | 'running' | 'paused' | 'stopped';
  /** 正在学习的主题名 */
  topic?: string;
  topic_id?: string;
  /** 当前页面信息 */
  current_page?: { url?: string; title?: string; site?: string };
  /** 已浏览页数 / 页数上限 */
  pages_visited?: number;
  max_pages?: number;
  /** 已提取知识点数 */
  knowledge_extracted?: number;
  /** 已用时（秒）/ 时长上限（秒） */
  elapsed_sec?: number;
  max_duration_sec?: number;
  /** AI 当前操作描述 */
  current_action?: string;
  /** AI 思考内容 */
  thinking?: string;
}

/** 学习日志条目（后端原始字段为 ts/action/reason/result，store 层归一化为 time/level/message） */
export interface LearnLogEntry {
  time?: string | number;
  level?: string;
  message?: string;
  /** 后端原始：Unix 时间戳 */
  ts?: number;
  /** 后端原始：操作类型 */
  action?: string;
  /** 后端原始：操作原因 */
  reason?: string;
  /** 后端原始：操作结果 */
  result?: string;
}

/** 学习设置（GET/PUT /learn/settings） */
export interface LearnSettings {
  /** 联网学习开关 */
  online_enabled: boolean;
  /** 单次时长上限（分钟） */
  max_duration_min: number;
  /** 单次页数上限 */
  max_pages: number;
  /** 搜索引擎 */
  search_engine: 'bing' | 'google' | 'baidu' | 'duckduckgo';
  /** 域名白名单（逗号/换行分隔存储为数组） */
  domain_whitelist: string[];
  /** 域名黑名单 */
  domain_blacklist: string[];
  /** 广告过滤开关 */
  ad_filter: boolean;
  /** 流量限制（KB/s，0 不限） */
  bandwidth_limit_kbps: number;
  /** 学习时显示浏览器窗口 */
  show_browser: boolean;
  /** 自动微调频率（天，0 关闭） */
  auto_finetune_days: number;
  /** 行为学习开关 */
  behavior_learning: boolean;
}

/** 浏览器状态（GET /browser/status） */
export interface BrowserStatus {
  running: boolean;
  /** 控制权：ai / user */
  controller?: 'ai' | 'user';
  current_url?: string;
  tabs?: number;
}

/** 浏览器截图（GET /browser/screenshot） */
export interface BrowserScreenshot {
  /** base64 图像（不含 data: 前缀） */
  image?: string;
  /** 或直接返回 data URL */
  data_url?: string;
  /** 后端实际返回字段：base64 图像 + MIME 类型 */
  image_base64?: string;
  mime?: string;
  url?: string;
}

/** 当前页信息（GET /browser/current-page） */
export interface BrowserCurrentPage {
  url?: string;
  title?: string;
  site?: string;
}

/** 浏览器标签页 */
export interface BrowserTab {
  id: string;
  url: string;
  title?: string;
  active?: boolean;
}

/** 知识库统计（GET /knowledge/stats，字段与后端真实响应对齐 2026-08-07 实测） */
export interface KnowledgeStats {
  /** 知识条目总数 */
  total: number;
  /** 容量上限 */
  capacity?: number;
  /** 占用空间（字节） */
  disk_bytes?: number;
  /** 按主题分布 */
  topics?: Record<string, number>;
  /** 按类型分布 */
  types?: Record<string, number>;
  /** 向量库后端 */
  vector_backend?: string;
  /** 嵌入模型后端 */
  embed_backend?: string;
  /** 数据库可用性 */
  db_available?: boolean;
}

/** 知识条目 */
export interface KnowledgeItem {
  id: string;
  title?: string;
  content?: string;
  source?: string;
  created_at?: string | number;
}

/** 知识图谱节点（GET /knowledge/graph，TASK-055） */
export interface KnowledgeGraphNode {
  id: string;
  name: string;
  kind?: string;
  ref_count?: number;
}

/** 知识图谱边 */
export interface KnowledgeGraphEdge {
  id: string;
  source: string;
  target: string;
  relation: string;
  weight?: number;
  kid?: string;
}

/** 知识图谱响应 */
export interface KnowledgeGraph {
  nodes: KnowledgeGraphNode[];
  edges: KnowledgeGraphEdge[];
  kid?: string;
  total_entities?: number;
  total_edges?: number;
}

/** 行为学习统计（GET /behavior/stats） */
export interface BehaviorStats {
  /** 已记录操作数 */
  event_count: number;
  /** 偏好模型状态 */
  preference_status?: string;
  /** 下次微调时间 */
  next_finetune_at?: string | number;
  /** 最近学习摘要 */
  recent_summary?: string;
}

/* ------------------------------ 学习主题 ------------------------------ */

/** 创建学习主题 */
export function createTopic(name: string) {
  return post<LearnTopic>('/learn/topic/create', { name });
}

/** 学习主题列表 */
export function listTopics() {
  return get<LearnTopic[] | { items: LearnTopic[] }>('/learn/topic/list');
}

/** 删除学习主题（DELETE 携带 JSON body {id}） */
export function deleteTopic(topicId: string) {
  return request<void>('/learn/topic/delete', { method: 'DELETE', body: { id: topicId } });
}

/* ------------------------------ 学习会话 ------------------------------ */

/** 开始学习会话（topic_id 为空时由 AI 自主决定） */
export function startSession(topicId?: string) {
  return post<{ session_id: string }>('/learn/session/start', topicId ? { topic_id: topicId } : {});
}

/** 暂停学习会话 */
export function pauseSession() {
  return post<void>('/learn/session/pause', {});
}

/** 恢复学习会话 */
export function resumeSession() {
  return post<void>('/learn/session/resume', {});
}

/** 停止学习会话 */
export function stopSession() {
  return post<void>('/learn/session/stop', {});
}

/** 学习会话实时状态 */
export function getSessionStatus() {
  return get<LearnSessionStatus>('/learn/session/status');
}

/** 学习日志 */
export function getSessionLogs(query?: QueryParams) {
  return get<LearnLogEntry[] | { items: LearnLogEntry[] }>('/learn/session/logs', query);
}

/** 学习报告 */
export function getSessionReport() {
  return get<{ summary?: string; knowledge_count?: number }>('/learn/session/report');
}

/* ------------------------------ 学习设置 ------------------------------ */

/** 获取学习设置 */
export function getLearnSettings() {
  return get<LearnSettings>('/learn/settings');
}

/** 保存学习设置（修改即保存） */
export function saveLearnSettings(patch: Partial<LearnSettings>) {
  return put<LearnSettings>('/learn/settings', patch);
}

/* ------------------------------ 内置浏览器 ------------------------------ */

/** 浏览器状态 */
export function getBrowserStatus() {
  return get<BrowserStatus>('/browser/status');
}

/** 浏览器截图（2 秒轮询） */
export function getBrowserScreenshot() {
  return get<BrowserScreenshot | string>('/browser/screenshot');
}

/** 浏览器当前页 */
export function getBrowserCurrentPage() {
  return get<BrowserCurrentPage>('/browser/current-page');
}

/** 浏览器标签页列表 */
export function getBrowserTabs() {
  return get<BrowserTab[] | { items: BrowserTab[] }>('/browser/tabs');
}

/** 浏览器导航 */
export function browserNavigate(url: string) {
  return post<void>('/browser/navigate', { url });
}

/** 用户接管浏览器（AI 暂停） */
export function browserTakeover() {
  return post<void>('/browser/takeover', {});
}

/** 交还浏览器控制权给 AI */
export function browserHandback() {
  return post<void>('/browser/handback', {});
}

/* ------------------------------ 知识库 ------------------------------ */

/** 知识库统计 */
export function getKnowledgeStats() {
  return get<KnowledgeStats>('/knowledge/stats');
}

/** 知识条目列表（搜索 + 分页） */
export function listKnowledge(query?: QueryParams) {
  return get<{ items: KnowledgeItem[]; total: number } | KnowledgeItem[]>('/knowledge/list', query);
}

/** 知识条目详情 */
export function getKnowledge(id: string) {
  return get<KnowledgeItem>(`/knowledge/${id}`);
}

/** 删除知识条目 */
export function deleteKnowledge(id: string) {
  return del<void>(`/knowledge/${id}`);
}

/** 知识图谱（TASK-055）：kid 为空返回全局图谱 */
export function getKnowledgeGraph(kid?: string, maxNodes?: number) {
  return get<KnowledgeGraph>('/knowledge/graph', {
    kid: kid || '',
    max_nodes: maxNodes ?? 200,
  });
}

/** 导入文档（multipart，pdf/docx/txt） */
export function importDocument(file: File) {
  const formData = new FormData();
  formData.append('file', file);
  return upload<{ imported: number; path?: string }>('/knowledge/import-document', formData);
}

/* ------------------------------ 行为学习 ------------------------------ */

/** 上报行为事件 */
export function postBehaviorEvent(body: { type: string; payload?: Record<string, unknown> }) {
  return post<void>('/behavior/event', body);
}

/** 行为学习统计 */
export function getBehaviorStats() {
  return get<BehaviorStats>('/behavior/stats');
}

/** 清空行为数据（重置偏好模型） */
export function clearBehavior() {
  return post<void>('/behavior/clear', {});
}

export default {
  createTopic,
  listTopics,
  deleteTopic,
  startSession,
  pauseSession,
  resumeSession,
  stopSession,
  getSessionStatus,
  getSessionLogs,
  getSessionReport,
  getLearnSettings,
  saveLearnSettings,
  getBrowserStatus,
  getBrowserScreenshot,
  getBrowserCurrentPage,
  getBrowserTabs,
  browserNavigate,
  browserTakeover,
  browserHandback,
  getKnowledgeStats,
  listKnowledge,
  getKnowledge,
  deleteKnowledge,
  getKnowledgeGraph,
  importDocument,
  postBehaviorEvent,
  getBehaviorStats,
  clearBehavior,
};
