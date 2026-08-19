/* ==========================================================================
 * 视频状态枚举前后端一致性测试（TASK-P1-04，审计 P13 前置）
 * --------------------------------------------------------------------------
 * 直读 backend/api/manga/ 包源码文本（TASK-P2-01 拆包后状态字面量
 * 分散于 common/keyframe/video/comic_asset 多模块，故整包扫描），
 * 提取 video_tasks.status 全部字面量取值，与前端常量
 * （VIDEO_TASK_STATUSES / VIDEO_STATUS_LABELS）及 Zod schema 枚举
 * 三方双向核对。
 * 后端新增/改名状态而前端未跟随时，本测试立即红——枚举漂移不再静默。
 * ========================================================================== */

import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync } from 'node:fs';
import path from 'node:path';
import {
  VIDEO_TASK_STATUSES,
  VIDEO_STATUS_LABELS,
  type VideoTaskStatus,
} from './videoStatus';
import { VideoStatusRespSchema } from '@/services/schema';

/** 后端 manga 包源码目录（vitest cwd = frontend/；TASK-P2-01 拆包后为目录） */
const BACKEND_MANGA_DIR = path.resolve(process.cwd(), '../backend/api/manga');

/** 读取包内全部 .py 模块源码合并为单一文本 */
function readBackendMangaSrc(): string {
  const files = readdirSync(BACKEND_MANGA_DIR).filter((f) => f.endsWith('.py'));
  if (files.length === 0) throw new Error(`manga 包为空: ${BACKEND_MANGA_DIR}`);
  return files.map((f) => readFileSync(path.join(BACKEND_MANGA_DIR, f), 'utf-8')).join('\n');
}

/** 从源码提取 status 字面量赋值与 .get() 默认值 */
function extractBackendStatuses(src: string): Set<string> {
  const values = new Set<string>();
  // "status": "done" / 'status': 'done'（含 dict 字面量赋值）
  const assignRe = /["']status["']\s*[:=]\s*["']([a-z_]+)["']/g;
  // row.get("status", "pending")（缺省回退值）
  const defaultRe = /\.get\(\s*["']status["']\s*,\s*["']([a-z_]+)["']\s*\)/g;
  for (const m of src.matchAll(assignRe)) values.add(m[1]);
  for (const m of src.matchAll(defaultRe)) values.add(m[1]);
  return values;
}

describe('视频状态枚举：前端常量 ↔ 后端 manga 包双向一致', () => {
  const backendSrc = readBackendMangaSrc();
  const backendStatuses = extractBackendStatuses(backendSrc);

  it('后端源码可读且提取到状态集合', () => {
    // 防御：提取逻辑失效（如后端重构改写法）时先于一致性断言暴露
    expect(backendStatuses.size).toBeGreaterThanOrEqual(4);
    expect(backendStatuses.has('done')).toBe(true);
  });

  it('前端枚举 ⊇ 后端全部取值（后端新状态前端必有承接）', () => {
    const frontend = new Set<string>(VIDEO_TASK_STATUSES);
    const missing = [...backendStatuses].filter((s) => !frontend.has(s));
    expect(
      missing,
      `后端存在前端未承接的状态: ${missing.join(', ')}（补 VIDEO_TASK_STATUSES/标签/schema）`,
    ).toEqual([]);
  });

  it('前端枚举 ⊆ 后端全部取值（前端无死状态）', () => {
    const dead = VIDEO_TASK_STATUSES.filter((s) => !backendStatuses.has(s));
    expect(
      dead,
      `前端枚举存在后端不会下发的死状态: ${dead.join(', ')}`,
    ).toEqual([]);
  });

  it('每个状态都有非空中文标签', () => {
    for (const status of VIDEO_TASK_STATUSES) {
      expect(VIDEO_STATUS_LABELS[status], `状态 ${status} 缺标签`).toBeTruthy();
    }
    // 标签键与枚举一一对应（Record 类型编译期约束的运行时复核）
    expect(Object.keys(VIDEO_STATUS_LABELS).sort()).toEqual([...VIDEO_TASK_STATUSES].sort());
  });

  it('Zod schema 接受后端每一个状态值（轮询响应不被误拒）', () => {
    for (const status of backendStatuses) {
      const payload = { task_id: 't1', status, progress: 0 };
      const result = VideoStatusRespSchema.safeParse(payload);
      expect(
        result.success,
        `VideoStatusRespSchema 拒绝了后端状态 "${status}"`,
      ).toBe(true);
    }
  });

  it('未知状态值被 schema 拒绝（枚举闭环）', () => {
    const result = VideoStatusRespSchema.safeParse({
      task_id: 't1',
      status: 'paused',
      progress: 0,
    });
    expect(result.success).toBe(false);
  });

  it('VideoTaskStatus 类型与标签 Record 键集合一致（编译期契约的运行时镜像）', () => {
    // 类型层面 Record<VideoTaskStatus, string> 保证键完备；
    // 运行时再验证值均为非空字符串（防手滑写成 undefined）
    const labels: Record<VideoTaskStatus, string> = VIDEO_STATUS_LABELS;
    for (const value of Object.values(labels)) {
      expect(typeof value).toBe('string');
      expect(value.length).toBeGreaterThan(0);
    }
  });
});
