/* ==========================================================================
 * WS 消息与高频端点 Zod schema 测试（批 3：FE-033 渐进覆盖 3b/3c）
 * --------------------------------------------------------------------------
 * 宁松勿严契约的回归锁定：多余字段放行（passthrough）、缺可选字段放行，
 * 但对象性/必需字段/数值类型错误必须拒绝（防 undefined 渗透 UI 引发崩溃）。
 * ========================================================================== */

import { describe, it, expect } from 'vitest';
import {
  WsFrameSchema,
  RealtimePayloadSchema,
  TaskBroadcastSchema,
  DrawStatusRespSchema,
  DialogSessionListRespSchema,
} from './schema';

describe('WS 帧信封 WsFrameSchema（三端点单点把关）', () => {
  it('合法帧通过，多余字段放行（passthrough）', () => {
    const r = WsFrameSchema.safeParse({ type: 'task_progress', data: { percent: 50 }, extra: 1 });
    expect(r.success).toBe(true);
  });

  it('缺 type / 空 type / 非对象帧拒绝', () => {
    expect(WsFrameSchema.safeParse({ data: {} }).success).toBe(false);
    expect(WsFrameSchema.safeParse({ type: '', data: {} }).success).toBe(false);
    expect(WsFrameSchema.safeParse('hello').success).toBe(false);
    expect(WsFrameSchema.safeParse(null).success).toBe(false);
    expect(WsFrameSchema.safeParse(undefined).success).toBe(false);
  });

  it('data 允许任意载荷（null/数组/标量）', () => {
    expect(WsFrameSchema.safeParse({ type: 'x', data: null }).success).toBe(true);
    expect(WsFrameSchema.safeParse({ type: 'x', data: [1, 2] }).success).toBe(true);
    expect(WsFrameSchema.safeParse({ type: 'x', data: 'raw' }).success).toBe(true);
  });
});

describe('硬件遥测载荷 RealtimePayloadSchema', () => {
  it('空对象与完整遥测都通过（宁松勿严：缺字段放行）', () => {
    expect(RealtimePayloadSchema.safeParse({}).success).toBe(true);
    expect(
      RealtimePayloadSchema.safeParse({
        cpu: { usage_percent: 8 },
        ram: { usage_percent: 44, total_gb: 31.7, available_gb: 17 },
        gpu: { usage_percent: 2, vram_used_mb: 2800, vram_total_mb: 16303, temp_celsius: 41 },
        timestamp: '2026-09-05T00:00:00Z',
      }).success,
    ).toBe(true);
  });

  it('字段类型错误拒绝（防字符串百分比渗透 UI）', () => {
    expect(RealtimePayloadSchema.safeParse({ cpu: { usage_percent: '8' } }).success).toBe(false);
    expect(RealtimePayloadSchema.safeParse({ gpu: [1, 2] }).success).toBe(false);
    expect(RealtimePayloadSchema.safeParse('telemetry').success).toBe(false);
    expect(RealtimePayloadSchema.safeParse(null).success).toBe(false);
  });
});

describe('任务广播载荷 TaskBroadcastSchema', () => {
  it('对象通过（含空对象，字段守卫在 normalizeTaskEvent）', () => {
    expect(TaskBroadcastSchema.safeParse({ task_id: 't1', percent: 50 }).success).toBe(true);
    expect(TaskBroadcastSchema.safeParse({}).success).toBe(true);
  });

  it('数组/标量/null 拒绝（.id 访问防崩溃边界）', () => {
    expect(TaskBroadcastSchema.safeParse([1, 2]).success).toBe(false);
    expect(TaskBroadcastSchema.safeParse('x').success).toBe(false);
    expect(TaskBroadcastSchema.safeParse(null).success).toBe(false);
  });
});

describe('绘画任务状态 DrawStatusRespSchema（queue_position 链入口）', () => {
  it('最小契约 {status} 通过，排队位次字段通过', () => {
    expect(DrawStatusRespSchema.safeParse({ status: 'running', percent: 30 }).success).toBe(true);
    expect(DrawStatusRespSchema.safeParse({ status: 'pending', queue_position: 2 }).success).toBe(true);
    expect(DrawStatusRespSchema.safeParse({ status: 'done', file_path: 'generated/images/a.png' }).success).toBe(true);
  });

  it('缺 status / queue_position 非正整数拒绝', () => {
    expect(DrawStatusRespSchema.safeParse({ percent: 10 }).success).toBe(false);
    expect(DrawStatusRespSchema.safeParse({ status: 'pending', queue_position: 0 }).success).toBe(false);
    expect(DrawStatusRespSchema.safeParse({ status: 'pending', queue_position: 1.5 }).success).toBe(false);
  });
});

describe('会话列表 DialogSessionListRespSchema（对话高频端点）', () => {
  const session = { id: 's1', title: '新对话', created_at: '2026-09-05', updated_at: 1 };

  it('合法分页通过，时间戳双形态（字符串/数字）都接受', () => {
    expect(DialogSessionListRespSchema.safeParse({ items: [session], total: 1 }).success).toBe(true);
    expect(
      DialogSessionListRespSchema.safeParse({ items: [{ ...session, created_at: 1, updated_at: 2 }], total: 1 }).success,
    ).toBe(true);
  });

  it('缺 items/total 拒绝', () => {
    expect(DialogSessionListRespSchema.safeParse({ items: [session] }).success).toBe(false);
    expect(DialogSessionListRespSchema.safeParse({ total: 0 }).success).toBe(false);
    expect(DialogSessionListRespSchema.safeParse(null).success).toBe(false);
  });
});
