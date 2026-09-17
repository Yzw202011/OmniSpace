/* ==========================================================================
 * pluginApi Zod 运行时校验测试（2026-09-16 插件导入功能）
 * --------------------------------------------------------------------------
 * mock 掉请求层（services/api），直测 pluginApi 对响应载荷的 Zod 解析
 * 与 multipart 组装（FormData 字段/确认旗标），对齐 mangaApi.test 模式。
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
import * as pluginApi from './pluginApi';
import type { ApiError } from '@/types';

const mockedGet = vi.mocked(get);
const mockedPost = vi.mocked(post);
const mockedUpload = vi.mocked(upload);

/** 提取 parseWith 抛出的 ApiError（异步版：接住 Promise 拒绝） */
async function extractApiError(fn: () => Promise<unknown>): Promise<ApiError> {
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

describe('listPlugins', () => {
  it('合法载荷通过并解包 plugins 数组', async () => {
    mockedGet.mockResolvedValue({
      plugins: [
        {
          name: 'rust-coding', trust: 'repo_curated',
          trust_label: '出厂·已审查', origin: 'factory',
          enabled: true, state: 'unloaded', source: 'src/cutemamen/rust_coding.py',
        },
        {
          name: 'demo', trust: 'user_source', trust_label: '用户·含源码',
          origin: 'user', imported_at: '2026-09-16T12:00:00',
          enabled: false, state: 'unloaded', source: 'data/plugins/imported/demo.py',
          capability: '回显', last_error: null,
        },
      ],
    });
    const list = await pluginApi.listPlugins();
    expect(list).toHaveLength(2);
    expect(list[0].origin).toBe('factory');
    expect(list[1].trust_label).toBe('用户·含源码');
    expect(list[1].capability).toBe('回显');
  });

  it('缺必需字段（name）抛 FRONTEND_PARSE_ERROR', async () => {
    mockedGet.mockResolvedValue({ plugins: [{ trust: 'user_data' }] });
    const err = await extractApiError(() => pluginApi.listPlugins());
    expect((err as ApiError).code).toBe('FRONTEND_PARSE_ERROR');
  });

  it('宽容多余字段（passthrough）不破坏前端', async () => {
    mockedGet.mockResolvedValue({
      plugins: [{
        name: 'x', trust: 'user_data', trust_label: '用户·纯数据',
        origin: 'user', enabled: true, state: 'unloaded', source: 'x.py',
        future_field: { nested: true },
      }],
    });
    const list = await pluginApi.listPlugins();
    expect(list[0].name).toBe('x');
  });
});

describe('importPlugin multipart 组装', () => {
  it(' FormData 字段：package 必带；confirm_source 按需（v2.1 单文件）', async () => {
    mockedUpload.mockResolvedValue({
      name: 'demo', trust: 'user_data', trust_label: '用户·纯数据',
      base_model: 'rust.coding', route: 'rust', capability: '',
      origin: 'user', imported_at: '2026-09-16T12:00:00',
    });
    const pkg = new File([new Uint8Array([1, 2, 3])], 'demo.CuteMamen');

    await pluginApi.importPlugin(pkg, false);
    const fd1 = mockedUpload.mock.calls[0][1] as FormData;
    expect(fd1.get('package')).toBeInstanceOf(File);
    expect(fd1.get('confirm_source')).toBeNull();

    await pluginApi.importPlugin(pkg, true);
    const fd2 = mockedUpload.mock.calls[1][1] as FormData;
    expect(fd2.get('confirm_source')).toBe('true');
  });

  it('非法导入结果载荷抛 FRONTEND_PARSE_ERROR', async () => {
    mockedUpload.mockResolvedValue({ name: 123 }); // name 必须是字符串
    const pkg = new File([new Uint8Array([1])], 'demo.CuteMamen');
    const err = await extractApiError(() => pluginApi.importPlugin(pkg, false));
    expect((err as ApiError).code).toBe('FRONTEND_PARSE_ERROR');
  });
});

/* ---------------- 插件技能（技能插座批1，2026-09-17） ---------------- */

describe('listSkills', () => {
  it('合法载荷通过并解包 skills 数组（feature 透传为查询参数）', async () => {
    mockedGet.mockResolvedValue({
      skills: [{
        plugin: 'chat-demo', id: 'polish', feature: 'chat',
        title: '台词润色', description: '润色当段对白',
        input: 'text', trust: 'user_data', trust_label: '用户·纯数据',
      }],
      features: ['chat', 'novel', 'comic', 'manga'],
    });
    const list = await pluginApi.listSkills('chat');
    expect(list).toHaveLength(1);
    expect(list[0].title).toBe('台词润色');
    expect(mockedGet).toHaveBeenCalledWith('/plugins/skills', { feature: 'chat' });
  });

  it('缺必需字段（plugin）抛 FRONTEND_PARSE_ERROR', async () => {
    mockedGet.mockResolvedValue({ skills: [{ id: 'polish' }] });
    const err = await extractApiError(() => pluginApi.listSkills('chat'));
    expect((err as ApiError).code).toBe('FRONTEND_PARSE_ERROR');
  });

  it('skills 键缺失时按空清单处理（无技能=无入口）', async () => {
    mockedGet.mockResolvedValue({});
    const list = await pluginApi.listSkills('chat');
    expect(list).toEqual([]);
  });
});

describe('invokeChatSkill', () => {
  it('合法载荷通过且请求体原样携带 plugin/skill_id/text', async () => {
    mockedPost.mockResolvedValue({
      plugin: 'chat-demo', skill_id: 'polish',
      title: '台词润色', output: '已处理: 你好',
    });
    const result = await pluginApi.invokeChatSkill({
      plugin: 'chat-demo', skill_id: 'polish', text: '你好',
    });
    expect(result.output).toBe('已处理: 你好');
    expect(mockedPost).toHaveBeenCalledWith('/dialog/skill', {
      plugin: 'chat-demo', skill_id: 'polish', text: '你好',
    });
  });

  it('缺 output（必需）抛 FRONTEND_PARSE_ERROR', async () => {
    mockedPost.mockResolvedValue({ plugin: 'chat-demo', skill_id: 'polish' });
    const err = await extractApiError(() =>
      pluginApi.invokeChatSkill({ plugin: 'chat-demo', skill_id: 'polish', text: 'hi' }));
    expect((err as ApiError).code).toBe('FRONTEND_PARSE_ERROR');
  });
});
