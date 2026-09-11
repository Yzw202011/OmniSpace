/* ==========================================================================
 * 状态文案集中营一致性测试（TASK-P2-07，审计 P13）
 * --------------------------------------------------------------------------
 * 1. 后端枚举真值核对：直读 backend/data/models.py 提取 TrainStatus /
 *    ModelStatus / GenerationStatus 枚举值，与前端常量/适配层映射双向核对；
 * 2. 完备性核对：每个标签 Record 键与对应类型字面量集合一致、值非空；
 * 3. 漂移即红：后端新增/改名状态而前端未跟随时立即失败。
 * ========================================================================== */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import {
  LEARN_SESSION_STATUS_LABELS,
  MODEL_AVAILABLE_STATUS_LABELS,
  MODEL_RUNTIME_STATUS_LABELS,
  ROW_GEN_STATUS_BADGE,
  ROW_GEN_STATUS_LABELS,
  SAVE_STATUS_LABELS,
  TRAIN_STATUS_CLASSES,
  TRAIN_STATUS_LABELS,
  VIDEO_STATUS_LABELS,
  VIDEO_TASK_STATUSES,
} from './statusLabels';
import type { AvailableModel, ShotSaveStatus } from '@/types';

const BACKEND_MODELS = path.resolve(process.cwd(), '../backend/data/models.py');

/** 从 backend/data/models.py 提取指定枚举类的全部取值 */
function extractBackendEnum(enumName: string): string[] {
  const src = readFileSync(BACKEND_MODELS, 'utf-8');
  const re = new RegExp(`class ${enumName}\\b[^:]*:\\s*\\n((?:\\s+\\w+\\s*=\\s*['"][\\w]+['"]\\s*\\n?)+)`);
  const m = re.exec(src);
  if (!m) throw new Error(`后端枚举 ${enumName} 未找到（提取逻辑失效或后端重构）`);
  const values = [...m[1].matchAll(/\w+\s*=\s*['"](\w+)['"]/g)].map((x) => x[1]);
  if (values.length === 0) throw new Error(`后端枚举 ${enumName} 提取为空`);
  return values;
}

/** 前端 TrainTask['status'] 字面量集合（编译期类型，运行时手写镜像） */
const FRONTEND_TRAIN_STATUSES = ['pending', 'running', 'done', 'error', 'cancelled'] as const;

describe('训练状态：前端 TrainTask ↔ 后端 TrainStatus', () => {
  it('后端每个枚举值都能经 TRAIN_STATUS_MAP 适配到前端状态', async () => {
    // 适配层在 services/learnApi.ts；直读其源码核对映射覆盖
    const learnApiSrc = readFileSync(
      path.resolve(process.cwd(), 'src/services/learnApi.ts'),
      'utf-8',
    );
    const backend = extractBackendEnum('TrainStatus');
    for (const v of backend) {
      expect(
        learnApiSrc,
        `后端 TrainStatus "${v}" 未在 learnApi TRAIN_STATUS_MAP 中登记适配`,
      ).toMatch(new RegExp(`\\b${v}\\s*:`));
    }
  });

  it('前端训练状态标签 Record 键集合 = TrainTask status 字面量集合', () => {
    expect(Object.keys(TRAIN_STATUS_LABELS).sort()).toEqual([...FRONTEND_TRAIN_STATUSES].sort());
    expect(Object.keys(TRAIN_STATUS_CLASSES).sort()).toEqual([...FRONTEND_TRAIN_STATUSES].sort());
    for (const v of Object.values(TRAIN_STATUS_LABELS)) {
      expect(v.length).toBeGreaterThan(0);
    }
  });
});

describe('分镜行生成状态：前端 StoryboardGenerationStatus ↔ 后端 GenerationStatus', () => {
  it('后端 GenerationStatus ⊆ 前端标签集合（skipped 为前端批量操作扩展态）', () => {
    const backend = extractBackendEnum('GenerationStatus');
    const frontend = Object.keys(ROW_GEN_STATUS_LABELS);
    const missing = backend.filter((v) => !frontend.includes(v));
    expect(missing, `后端行生成状态未被前端承接: ${missing}`).toEqual([]);
    // skipped 只能来自前端批量操作，后端枚举不含
    expect(backend).not.toContain('skipped');
  });

  it('标签与徽标 Record 键一致且值非空', () => {
    expect(Object.keys(ROW_GEN_STATUS_LABELS).sort()).toEqual(
      Object.keys(ROW_GEN_STATUS_BADGE).sort(),
    );
    for (const v of Object.values(ROW_GEN_STATUS_LABELS)) expect(v.length).toBeGreaterThan(0);
    for (const v of Object.values(ROW_GEN_STATUS_BADGE)) expect(v.length).toBeGreaterThan(0);
  });
});

describe('模型状态标签', () => {
  it('后端 ModelStatus ⊆ 前端运行态标签（not_ready 为前端显存派生态）', () => {
    const backend = extractBackendEnum('ModelStatus');
    const frontend = Object.keys(MODEL_RUNTIME_STATUS_LABELS);
    const missing = backend.filter((v) => !frontend.includes(v));
    expect(missing, `后端 ModelStatus 未被前端承接: ${missing}`).toEqual([]);
  });

  it('可用模型标签键集合 = AvailableModel.status 字面量集合', () => {
    const expected: AvailableModel['status'][] = ['ready', 'loading', 'not_installed', 'downloaded', 'offload'];
    expect(Object.keys(MODEL_AVAILABLE_STATUS_LABELS).sort()).toEqual([...expected].sort());
    for (const v of Object.values(MODEL_AVAILABLE_STATUS_LABELS)) expect(v.length).toBeGreaterThan(0);
  });
});

describe('本地状态机与转发出口', () => {
  it('SAVE_STATUS_LABELS 键集合 = ShotSaveStatus 字面量集合', () => {
    const expected: ShotSaveStatus[] = ['idle', 'dirty', 'saving', 'saved', 'error'];
    expect(Object.keys(SAVE_STATUS_LABELS).sort()).toEqual([...expected].sort());
    // idle 为空文案（状态点隐藏），其余必须非空
    for (const [k, v] of Object.entries(SAVE_STATUS_LABELS)) {
      if (k !== 'idle') expect(v.length).toBeGreaterThan(0);
    }
  });

  it('videoStatus 转发出口与源模块一致', () => {
    expect(Object.keys(VIDEO_STATUS_LABELS).sort()).toEqual([...VIDEO_TASK_STATUSES].sort());
  });

  it('学习会话标签四态完备', () => {
    expect(Object.keys(LEARN_SESSION_STATUS_LABELS).sort()).toEqual(
      ['idle', 'paused', 'running', 'stopped'].sort(),
    );
  });
});
// 本项目仅供学习使用，商业授权请+Q 3559331368
