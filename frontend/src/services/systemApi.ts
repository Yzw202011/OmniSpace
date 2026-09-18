/* ==========================================================================
 * OmniSpace AI v2.1 —— 系统 API（规格 §4.7 系统端点）
 * --------------------------------------------------------------------------
 * 严格对齐后端 src/api/system.py 与 main.py 实际路由：
 * - GET  /system/settings         读取设置
 * - PUT  /system/settings         更新设置
 * - POST /system/diagnose         27 项诊断检测
 * - POST /system/backup           备份配置（写 data/backups/*.json）
 * - GET  /system/version          版本信息
 * - POST /system/project/export   项目导出（body: {project_id}，JSON 元数据）
 * - POST /system/project/import   项目导入（body: {file_path}，JSON）
 *
 * 已移除悬空端点（后端不存在，调用必然 404）：
 *   /system/status、/system/logs、/system/cleanup、
 *   /system/resume-scan、/system/restore
 * ========================================================================== */

import { z } from 'zod';

import { get, post, put } from './api';
import { parseWith, SystemVersionRespSchema } from './schema';

/** 版本信息（对齐 system.py /system/version 返回 data） */
export interface VersionInfo {
  version: string;
  host?: string;
  port?: number;
  build?: string;
  timestamp?: number;
}

/** 单项诊断结果（对齐后端 _DIAG_ITEMS 输出） */
export interface DiagnoseItem {
  index: number;
  name: string;
  status: 'pass' | 'warn' | 'fail';
  detail: string;
}

/** 诊断汇总 */
export interface DiagnoseSummary {
  total: number;
  pass: number;
  warn: number;
  fail: number;
}

/** 诊断响应 Zod 模板（宁松勿严：多余字段透传，核心结构必须齐） */
const DiagnoseResultSchema = z.object({
  items: z.array(z.object({
    index: z.number(),
    name: z.string(),
    status: z.enum(['pass', 'warn', 'fail']),
    detail: z.string(),
  }).passthrough()),
  summary: z.object({
    total: z.number(),
    pass: z.number(),
    warn: z.number(),
    fail: z.number(),
  }).passthrough(),
  checked_at: z.number(),
}).passthrough();

/** 诊断响应（POST /system/diagnose 返回 data） */
export interface DiagnoseResult {
  items: DiagnoseItem[];
  summary: DiagnoseSummary;
  checked_at: number;
}

/** 备份响应（POST /system/backup 返回 data） */
export interface BackupResult {
  backup_id: string;
  /** 备份文件落盘路径（写盘失败为空串） */
  path: string;
  size_bytes: number;
}

/** 项目导出响应（POST /system/project/export 返回 data） */
export interface ProjectExportResult {
  project_id: string;
  /** .omnispace 归档文件名 */
  archive: string;
  /** 归档落盘路径（写盘失败为空串） */
  path: string;
  size_bytes: number;
}

/** 项目导入响应（POST /system/project/import 返回 data） */
export interface ProjectImportResult {
  project_id: string;
  imported: boolean;
  source: string;
}

/* ------------------------------ 设置 ------------------------------ */

/** 读取设置 */
export function getSettings() {
  return get<Record<string, unknown>>('/system/settings');
}

/** 更新设置 */
export function updateSettings(body: Record<string, unknown>) {
  return put<Record<string, unknown>>('/system/settings', body);
}

/* ------------------------ 界面偏好镜像（2026-09-12） ------------------------ */

/** 读取界面偏好镜像（localStorage 键值对；选型持久化回放用） */
export function getUiPrefs(timeoutMs?: number) {
  return get<Record<string, unknown>>(
    '/system/ui_prefs', undefined, timeoutMs ? { timeout: timeoutMs } : {});
}

/** 写入界面偏好镜像（前端读合并写整包提交） */
export function updateUiPrefs(body: Record<string, unknown>) {
  return put<Record<string, unknown>>('/system/ui_prefs', body);
}

/** 联网搜索 v1 配置（架构升级计划 B-阶段一，默认关） */
export interface WebSearchSettings {
  enabled: boolean;
  provider: 'browser' | 'searxng' | 'bocha';
  searxng_url: string;
  bocha_key: string;
  trigger: 'auto' | 'always' | 'off';
  top_k: number;
  timeout_s: number;
}

/** 读取联网搜索配置（GET /system/web_search） */
export function getWebSearchSettings() {
  return get<WebSearchSettings>('/system/web_search');
}

/** 更新联网搜索配置（PUT /system/web_search，服务端做键值校验） */
export function updateWebSearchSettings(body: Partial<WebSearchSettings>) {
  return put<WebSearchSettings>('/system/web_search', body);
}

/** 测试远程推理服务器连通性（批3 D3：POST /system/dialog-remote/test） */
export function testDialogRemote(body: { base_url: string; api_key?: string }) {
  return post<{ reachable: boolean; detail: string; base_url: string }>(
    '/system/dialog-remote/test',
    body,
  );
}

/* ------------------------------ 诊断/备份 ------------------------------ */

/** 运行 27 项诊断检测（POST /system/diagnose） */
export function runDiagnose() {
  return post<unknown>('/system/diagnose', {}).then((res) =>
    parseWith(DiagnoseResultSchema, res, '运行诊断'));
}

/** 备份配置（POST /system/backup，生成 JSON 备份文件） */
export function backup() {
  return post<BackupResult>('/system/backup', {});
}

/* ------------------------------ 版本 ------------------------------ */

/** 获取版本信息（Zod 校验：version 必需，防止异常载荷渗透 UI） */
export async function getVersion(): Promise<VersionInfo> {
  const res = parseWith(
    SystemVersionRespSchema,
    await get<unknown>('/system/version'),
    '版本信息',
  );
  return res as VersionInfo;
}

/* ------------------------------ 项目导入/导出 ------------------------------ */

/**
 * 项目导出（POST /system/project/export）。
 * 后端将项目打包为 .omnispace 归档并返回 JSON 元数据（非文件下载流）。
 */
export function exportProject(projectId: string) {
  return post<ProjectExportResult>('/system/project/export', {
    project_id: projectId,
  });
}

/**
 * 项目导入（POST /system/project/import）。
 * 后端从 .omnispace 归档恢复项目；body 为 JSON {file_path}（非 multipart）。
 * 归档不存在返回 80003（项目文件损坏）。
 */
export function importProject(filePath: string) {
  return post<ProjectImportResult>('/system/project/import', {
    file_path: filePath,
  });
}

export default {
  getSettings,
  updateSettings,
  runDiagnose,
  backup,
  getVersion,
  exportProject,
  importProject,
};

/** 本地算力 · 省钱账本（GET /system/local-savings，2026-09-08）
 * 保守口径：仅统计 AI 输出侧（输入/文档未计），云端生成的不算省钱；
 * 金额按云端参考价估算，具体口径见 scope_note。 */
export interface LocalSavings {
  text: { messages: number; chars: number; tokens_est: number };
  images: { count: number; keyframes: number; comic_assets: number; paint: number };
  videos: { count: number };
  money: {
    cny_est: number;
    prices: { text_cny_per_mtok: number; image_cny_each: number; video_cny_each: number };
  };
  scope_note: string;
}

/** 拉取本地 GPU 产出统计与云端等价省钱估算 */
export function getLocalSavings() {
  return get<LocalSavings>('/system/local-savings');
}

/* ------------------------------ 一键体检与修复（自愈批4） ------------------------------ */

/** 单项体检结果（对齐 src/services/health_check.py 输出） */
export const HealthCheckItemSchema = z.object({
  key: z.string(),
  level: z.enum(['ok', 'warn', 'fail', 'unknown']),
  friendly: z.string(),
  fixable: z.boolean().optional(),
  fix_action: z.string().optional(),
  detail: z.unknown().optional(),
});
export type HealthCheckItem = z.infer<typeof HealthCheckItemSchema>;

/** 体检报告（宁松勿严：detail 结构各检查项不同，不校验内部形状） */
export const HealthCheckReportSchema = z.object({
  items: z.array(HealthCheckItemSchema),
  summary: z.string(),
  counts: z.object({ warn: z.number(), fail: z.number(), total: z.number() }),
  checked_at: z.string(),
});
export type HealthCheckReport = z.infer<typeof HealthCheckReportSchema>;

/** 一键体检（只读）：显存/内存/磁盘/孤儿进程/模型对账/队列/日志保留/激活 */
export async function runHealthCheck(): Promise<HealthCheckReport> {
  const res = parseWith(
    HealthCheckReportSchema,
    await get<unknown>('/system/health-check'),
    '一键体检',
  );
  return res as HealthCheckReport;
}

/** 修复动作结果（friendly 必需，其余随动作各异） */
export const HealthRepairResultSchema = z
  .object({ action: z.string(), friendly: z.string() })
  .passthrough();

/** 一键修复（白名单制）：clean_logs / rebuild_dirs / kill_orphans */
export async function runHealthRepair(action: string): Promise<{ action: string; friendly: string }> {
  const res = parseWith(
    HealthRepairResultSchema,
    await post<unknown>('/system/health-repair', { action }),
    '一键修复',
  );
  return res as { action: string; friendly: string };
}
// 本项目仅供学习使用，商业授权请+Q 3559331368

/** LAN 访问令牌信息（批2-1：回环或持有效令牌可读；本机模式恒空） */
export async function getLanTokenInfo(): Promise<{ enabled: boolean; token: string }> {
  return get<{ enabled: boolean; token: string }>('/system/lan-token');
}
