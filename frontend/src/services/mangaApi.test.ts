/* ==========================================================================
 * mangaApi Zod 运行时校验测试（TASK-P1-04，审计 P12 / FE-033）
 * --------------------------------------------------------------------------
 * mock 掉请求层（services/api），直测 mangaApi 各函数对响应载荷的
 * Zod 解析行为：合法载荷通过、非法载荷抛 FRONTEND_PARSE_ERROR、
 * 宽容多余字段（passthrough）不破坏前端。
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

import { get, post } from './api';
import * as mangaApi from './mangaApi';
import type { ApiError } from '@/types';

const mockedGet = vi.mocked(get);
const mockedPost = vi.mocked(post);

/** 提取 parseWith 抛出的 ApiError（避免 throw 字面量对象被 vitest 吞字段） */
function asApiError(err: unknown): ApiError {
  expect(err).toBeTruthy();
  expect(typeof err).toBe('object');
  return err as ApiError;
}

/** 最小合法分镜行载荷（对齐后端 _row_to_storyboard_row） */
function rowPayload(id = 'r1', shot = 1): Record<string, unknown> {
  return {
    id,
    shot_number: shot,
    original_dialogue: '台词',
    description: '画面',
    characters: ['主角'],
    scene: '教室',
    props: [],
    voice_id: '',
    voice_emotion: '默认',
    speed: 1.0,
    volume: 0.0,
    director_stage_done: false,
    generation_status: 'pending',
    is_ai_generated: false,
    asset_ids: [],
    is_locked: false,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('getStoryboardRows —— 分镜表响应解析', () => {
  it('合法载荷：返回强类型行列表', async () => {
    mockedGet.mockResolvedValue({
      project_id: 'p1',
      rows: [rowPayload('r1'), rowPayload('r2', 2)],
      total: 2,
    });

    const rows = await mangaApi.getStoryboardRows('p1');

    expect(rows).toHaveLength(2);
    expect(rows[0].id).toBe('r1');
    expect(rows[1].shot_number).toBe(2);
    expect(mockedGet).toHaveBeenCalledWith('/manga/storyboard/p1');
  });

  it('宽容多余字段：后端新增字段不破坏前端（passthrough）', async () => {
    mockedGet.mockResolvedValue({
      project_id: 'p1',
      rows: [{ ...rowPayload(), brand_new_field: { nested: true } }],
      total: 1,
      extra_meta: 'v2.4 后端新增',
    });

    const rows = await mangaApi.getStoryboardRows('p1');
    expect(rows).toHaveLength(1);
  });

  it('缺必需字段（rows 缺失）：抛 FRONTEND_PARSE_ERROR 且报错可读', async () => {
    mockedGet.mockResolvedValue({ project_id: 'p1', total: 0 });

    let caught: unknown;
    try {
      await mangaApi.getStoryboardRows('p1');
    } catch (err) {
      caught = err;
    }
    const apiErr = asApiError(caught);
    expect(apiErr.code).toBe('FRONTEND_PARSE_ERROR');
    expect(apiErr.message).toContain('分镜表');
    expect(apiErr.message).toContain('rows');
  });

  it('字段类型错误（shot_number 为字符串）：同样拒绝', async () => {
    mockedGet.mockResolvedValue({
      project_id: 'p1',
      rows: [{ ...rowPayload(), shot_number: 'not-a-number' }],
      total: 1,
    });

    await expect(mangaApi.getStoryboardRows('p1')).rejects.toMatchObject({
      code: 'FRONTEND_PARSE_ERROR',
    });
  });

  it('可选字段缺省：Zod default 补齐（原值为空不崩溃）', async () => {
    // 后端早期版本可能省略新增的可选字段
    mockedGet.mockResolvedValue({
      project_id: 'p1',
      rows: [
        {
          id: 'r1',
          shot_number: 1,
          // asset_ids / is_locked / voice_emotion 等全部缺省
        },
      ],
      total: 1,
    });

    const rows = await mangaApi.getStoryboardRows('p1');
    expect(rows[0].asset_ids).toEqual([]);
    expect(rows[0].is_locked).toBe(false);
    expect(rows[0].generation_status).toBe('pending');
  });
});

describe('getVideoStatus —— 视频状态响应解析', () => {
  it('generating 态：进度数值合法通过', async () => {
    mockedGet.mockResolvedValue({
      task_id: 't1',
      status: 'generating',
      progress: 0.42,
    });

    const st = await mangaApi.getVideoStatus('t1');
    expect(st.status).toBe('generating');
    expect(st.progress).toBeCloseTo(0.42);
    expect(mockedGet).toHaveBeenCalledWith('/manga/video/t1/status');
  });

  it('cancelled 终态：合法枚举值通过（取消链路不误报）', async () => {
    mockedGet.mockResolvedValue({ task_id: 't1', status: 'cancelled', progress: 0.3 });

    const st = await mangaApi.getVideoStatus('t1');
    expect(st.status).toBe('cancelled');
  });

  it('error 态：error 字段透传', async () => {
    mockedGet.mockResolvedValue({
      task_id: 't1',
      status: 'error',
      progress: 0.8,
      error: '显存不足',
    });

    const st = await mangaApi.getVideoStatus('t1');
    expect(st.error).toBe('显存不足');
  });

  it('progress 越界（1.5）：拒绝——进度条契约 0~1', async () => {
    mockedGet.mockResolvedValue({ task_id: 't1', status: 'done', progress: 1.5 });

    await expect(mangaApi.getVideoStatus('t1')).rejects.toMatchObject({
      code: 'FRONTEND_PARSE_ERROR',
    });
  });

  it('未知状态值（paused）：拒绝——前后端枚举漂移立即暴露', async () => {
    mockedGet.mockResolvedValue({ task_id: 't1', status: 'paused', progress: 0.1 });

    await expect(mangaApi.getVideoStatus('t1')).rejects.toMatchObject({
      code: 'FRONTEND_PARSE_ERROR',
    });
  });
});

describe('generateVideo / getVideoResult —— 发起与结果解析', () => {
  it('发起响应：task_id + status 通过', async () => {
    mockedPost.mockResolvedValue({ task_id: 't9', status: 'generating' });

    const res = await mangaApi.generateVideo({
      storyboard_row_id: 'r1',
      description: '画面',
      screenshot_4in1: '',
    });

    expect(res.task_id).toBe('t9');
    expect(mockedPost).toHaveBeenCalledWith('/manga/video/generate', {
      storyboard_row_id: 'r1',
      description: '画面',
      screenshot_4in1: '',
    });
  });

  it('结果响应：result 为 null（未完成）与完整详情两态均通过', async () => {
    // 未完成：result null
    mockedGet.mockResolvedValueOnce({ task_id: 't1', status: 'generating', result: null });
    const pending = await mangaApi.getVideoResult('t1');
    expect(pending.result).toBeNull();

    // 完成：完整详情（含扩展字段 download_url / file_exists）
    mockedGet.mockResolvedValueOnce({
      task_id: 't1',
      status: 'done',
      result: {
        id: 't1',
        file_path: 'generated/t1.mp4',
        model_used: 'ltx-2',
        duration_seconds: 4,
        resolution: '720p',
        generation_time_ms: 12000,
        has_audio_sync: false,
        download_url: '/api/v1/manga/video/t1/download',
        file_exists: true,
      },
    });
    const done = await mangaApi.getVideoResult('t1');
    expect(done.result?.download_url).toContain('/manga/video/t1/download');
    expect(done.result?.file_exists).toBe(true);
  });

  it('结果详情缺必需字段（model_used）：拒绝', async () => {
    mockedGet.mockResolvedValue({
      task_id: 't1',
      status: 'done',
      result: {
        id: 't1',
        file_path: 'x.mp4',
        duration_seconds: 4,
        resolution: '720p',
        generation_time_ms: 1,
        has_audio_sync: false,
      },
    });

    await expect(mangaApi.getVideoResult('t1')).rejects.toMatchObject({
      code: 'FRONTEND_PARSE_ERROR',
    });
  });
});

describe('getVideoDownloadUrl —— 下载地址拼装（纯函数）', () => {
  it('拼装直下载地址', () => {
    const url = mangaApi.getVideoDownloadUrl('abc');
    expect(url).toBe('/api/v1/manga/video/abc/download');
  });
});

describe('createProject —— project_type 必传回归（审计 09-10 P1-E/P2-9）', () => {
  it('project_type 随请求体下发（comic 面）', async () => {
    mockedPost.mockResolvedValue({ project_id: 'p1', name: 'n', rows: [] });

    await mangaApi.createProject('n', 'comic');

    expect(mockedPost).toHaveBeenCalledWith(
      '/comic/project/create',
      expect.objectContaining({ project_type: 'comic' }),
    );
  });

  it('project_type 随请求体下发（manga 面）', async () => {
    mockedPost.mockResolvedValue({ project_id: 'p2', name: 'm', rows: [] });

    await mangaApi.createProject('m', 'manga', 'comic_drama');

    expect(mockedPost).toHaveBeenCalledWith(
      '/comic/project/create',
      expect.objectContaining({ project_type: 'manga', template: 'comic_drama' }),
    );
  });
});
// 本项目仅供学习使用，商业授权请+Q 3559331368
