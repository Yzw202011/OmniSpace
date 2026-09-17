/* ==========================================================================
 * A/B 批新增 services 函数测试（2026-09-17 拍板=补齐，对齐 pluginApi 标准）
 * --------------------------------------------------------------------------
 * 覆盖入口补齐 A/B 批新增的读函数 Zod 解析：合法载荷通过 + 关键字段
 * 类型错必拒。mock 请求层（services/api），模式对齐 pluginApi.test.ts。
 * ========================================================================== */

import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('./api', () => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  del: vi.fn(),
  upload: vi.fn(),
  request: vi.fn(),
  API_BASE: '/api/v1',
  API_ROOT: '',
  isApiError: vi.fn(() => false),
}));

import { get, post, upload } from './api';
import { getLearnEfficiency } from './learningApi';
import { listLoraVersions } from './learnApi';
import { getBenchmarkHistory } from './modelApi';
import { listStyleTemplates } from './styleApi';
import { segmentImage, uploadCustomVoice } from './mangaApi';
import type { ApiError } from '@/types';

const mockedGet = vi.mocked(get);
const mockedPost = vi.mocked(post);
const mockedUpload = vi.mocked(upload);

async function expectApiError(fn: () => Promise<unknown>): Promise<ApiError> {
  try {
    await fn();
  } catch (err) {
    return err as ApiError;
  }
  throw new Error('期望抛出但未抛出');
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('learningApi.getLearnEfficiency', () => {
  const valid = {
    days: 7, sessions: 3, pages_visited: 12, knowledge_extracted: 5,
    extraction_rate: 0.42, per_hour: 1.5, hours: 2.5,
  };
  it('合法载荷通过', async () => {
    mockedGet.mockResolvedValue(valid);
    const out = await getLearnEfficiency(7);
    expect(out.extraction_rate).toBe(0.42);
  });
  it('必填字段类型错必拒', async () => {
    mockedGet.mockResolvedValue({ ...valid, sessions: 'three' });
    const err = await expectApiError(() => getLearnEfficiency(7));
    expect(err.message).toContain('学习效率');
  });
});

describe('learnApi.listLoraVersions', () => {
  it('合法载荷通过（items 数组 + current）', async () => {
    mockedGet.mockResolvedValue({
      items: [{ version: 'v3', created_at: 1 }, { version: 'v2' }],
      current: 'v3',
    });
    const out = await listLoraVersions();
    expect(out.items).toHaveLength(2);
    expect(out.current).toBe('v3');
  });
  it('items 非数组必拒', async () => {
    mockedGet.mockResolvedValue({ items: 'nope', current: '' });
    await expectApiError(() => listLoraVersions());
  });
});

describe('modelApi.getBenchmarkHistory', () => {
  const item = {
    model_id: 'qwen3-vl-4b', tokens_per_s: 41.2, first_token_ms: 120,
  };
  it('合法载荷通过（可选字段带缺省）', async () => {
    mockedGet.mockResolvedValue({ items: [item] });
    const out = await getBenchmarkHistory('qwen3-vl-4b');
    expect(out[0].runs).toBe(0);          // 缺省补零
    expect(out[0].tokens_per_s).toBe(41.2);
  });
  it('tokens_per_s 类型错必拒', async () => {
    mockedGet.mockResolvedValue({ items: [{ ...item, tokens_per_s: 'fast' }] });
    await expectApiError(() => getBenchmarkHistory('x'));
  });
});

describe('styleApi.listStyleTemplates', () => {
  it('合法载荷通过', async () => {
    mockedGet.mockResolvedValue({
      items: [{ name: '赛博水彩', lora_rank: 16, epochs: 10 }],
    });
    const out = await listStyleTemplates();
    expect(out[0].name).toBe('赛博水彩');
  });
  it('name 缺失必拒', async () => {
    mockedGet.mockResolvedValue({ items: [{ lora_rank: 16 }] });
    await expectApiError(() => listStyleTemplates());
  });
});

describe('mangaApi.segmentImage / uploadCustomVoice', () => {
  it('抠图：蒙版与置信分解析通过', async () => {
    mockedPost.mockResolvedValue({
      mask_png_b64: 'iVBOR...', score: 0.93, elapsed_s: 1.2,
    });
    const out = await segmentImage('data:image/png;base64,x', [[10, 20]]);
    expect(out.score).toBeCloseTo(0.93);
  });
  it('抠图：score 类型错必拒', async () => {
    mockedPost.mockResolvedValue({ mask_png_b64: 'x', score: 'high' });
    await expectApiError(() => segmentImage('x', []));
  });
  it('音色上传：voice_id/name 解析通过', async () => {
    mockedUpload.mockResolvedValue({ voice_id: 'v1', name: '旁白哥' });
    const out = await uploadCustomVoice('旁白哥', new File([], 'a.wav'));
    expect(out.voice_id).toBe('v1');
  });
});
