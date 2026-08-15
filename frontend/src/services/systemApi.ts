/* ==========================================================================
 * OmniSpace AI v2.1 —— 系统 API（规格 §4.7 系统端点）
 * --------------------------------------------------------------------------
 * 严格对齐后端 backend/api/system.py 与 main.py 实际路由：
 * - GET  /health                  健康检查（根路径，不带 /v1 前缀，root 模式）
 * - GET  /system/settings         读取设置
 * - PUT  /system/settings         更新设置
 * - POST /system/diagnose         26 项诊断检测
 * - POST /system/backup           备份配置（写 data/backups/*.json）
 * - GET  /system/version          版本信息
 * - POST /system/project/export   项目导出（body: {project_id}，JSON 元数据）
 * - POST /system/project/import   项目导入（body: {file_path}，JSON）
 *
 * 已移除悬空端点（后端不存在，调用必然 404）：
 *   /system/status、/system/logs、/system/cleanup、
 *   /system/resume-scan、/system/restore
 * ========================================================================== */

import { get, post, put } from './api';
import { parseWith, SystemVersionRespSchema } from './schema';

/** 健康检查响应（对齐 main.py /health 返回 data） */
export interface HealthInfo {
  status: string;
  version: string;
  uptime_s: number;
  db: string;
}

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

/** 健康检查（根路径 /health，无 /v1 前缀） */
export function health() {
  return get<HealthInfo>('/health', undefined, { root: true });
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

/* ------------------------------ 诊断/备份 ------------------------------ */

/** 运行 26 项诊断检测（POST /system/diagnose） */
export function runDiagnose() {
  return post<DiagnoseResult>('/system/diagnose', {});
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
  health,
  getSettings,
  updateSettings,
  runDiagnose,
  backup,
  getVersion,
  exportProject,
  importProject,
};
