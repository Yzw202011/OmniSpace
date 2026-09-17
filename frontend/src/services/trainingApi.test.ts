/**
 * 训练中心统一队列纯函数测试（P1，2026-09-17）。
 * 归一化容错 + 排序口径（created_at 倒序/缺失沉底，与后端一致）。
 */
import { describe, expect, it } from 'vitest';

import {
  normalizeTrainingItem,
  sortUnifiedTasks,
  type UnifiedTrainingTask,
} from './trainingApi';

describe('normalizeTrainingItem', () => {
  it('风格任务：name 缺失回落 style_prompt', () => {
    const t = normalizeTrainingItem(
      { id: 's1', style_prompt: '国风水墨', status: 'training', progress: 0.42, created_at: 3 },
      'style',
    );
    expect(t.name).toBe('国风水墨');
    expect(t.kindLabel).toBe('风格训练');
    expect(t.progress).toBeCloseTo(0.42);
  });

  it('知识任务：无 name/prompt 回落 base_model；字段缺失容错', () => {
    const t = normalizeTrainingItem(
      { id: 'k1', base_model: 'qwen3-vl-4b', created_at: 'oops' },
      'knowledge',
    );
    expect(t.name).toBe('qwen3-vl-4b');
    expect(t.status).toBe('unknown');
    expect(t.progress).toBe(0);
    expect(t.created_at).toBeNull(); // 非数字时间戳容错为 null
  });
});

describe('sortUnifiedTasks', () => {
  const mk = (id: string, at: number | null): UnifiedTrainingTask => ({
    id, kind: 'knowledge', kindLabel: '知识训练', name: id,
    status: 'queued', progress: 0, created_at: at, updated_at: at,
  });
  it('倒序 + 缺失沉底 + 不改原数组', () => {
    const arr = [mk('a', 1), mk('b', null), mk('c', 3), mk('d', 2)];
    const sorted = sortUnifiedTasks(arr);
    expect(sorted.map((t) => t.id)).toEqual(['c', 'd', 'a', 'b']);
    expect(arr.map((t) => t.id)).toEqual(['a', 'b', 'c', 'd']);
  });
});
