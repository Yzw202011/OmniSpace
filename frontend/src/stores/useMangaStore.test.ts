/* ==========================================================================
 * useMangaStore 状态流转测试（TASK-P1-04，审计 P12）
 * --------------------------------------------------------------------------
 * 不依赖后端：mangaApi / ws / systemApi 全部 mock，
 * 直测 zustand store 的状态迁移正确性（加载态/乐观更新/回滚/轮询收敛）。
 * ========================================================================== */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import type { StoryboardRow, ComicProject } from '@/types';
import { useAppStore } from './useAppStore';
import { useTaskStore } from './useTaskStore';

vi.mock('@/services/mangaApi', () => ({
  getStoryboardRows: vi.fn(),
  saveStoryboardRows: vi.fn(async () => []),
  updateStoryboardRow: vi.fn(),
  autoSplitStoryboard: vi.fn(),
  importScript: vi.fn(),
  reorderStoryboard: vi.fn(),
  listProjects: vi.fn(),
  createProject: vi.fn(),
  renameProject: vi.fn(),
  deleteProject: vi.fn(),
  listAssets: vi.fn(),
  bindAsset: vi.fn(),
  unbindAsset: vi.fn(),
  listKeyframes: vi.fn(),
  listVoices: vi.fn(),
  bindVoice: vi.fn(),
  updateVoiceEmotion: vi.fn(),
  previewVoice: vi.fn(),
  generateVideo: vi.fn(),
  getVideoStatus: vi.fn(),
  getVideoDownloadUrl: vi.fn((taskId: string) => `/api/v1/manga/video/${taskId}/download`),
  cancelVideo: vi.fn(),
}));

// useTaskStore 的 WS 订阅在 node 测试环境不可用，mock 掉连接层
vi.mock('@/services/ws', () => ({
  getWsHub: vi.fn(() => ({
    subscribe: vi.fn(() => vi.fn()),
    connect: vi.fn(),
    disconnect: vi.fn(),
    on: vi.fn(),
    off: vi.fn(),
  })),
}));

// useAppStore 加载设置走 systemApi；mock 避免真实请求
vi.mock('@/services/systemApi', () => ({
  getSettings: vi.fn(async () => ({})),
  updateSettings: vi.fn(async () => undefined),
}));

import * as mangaApi from '@/services/mangaApi';
import { useMangaStore } from './useMangaStore';

const mockedApi = vi.mocked(mangaApi);

/** 构造分镜行测试数据 */
function makeRow(overrides: Partial<StoryboardRow> = {}): StoryboardRow {
  return {
    id: overrides.id ?? 'row-1',
    shot_number: overrides.shot_number ?? 1,
    sort_index: overrides.sort_index ?? overrides.shot_number ?? 1,
    original_dialogue: overrides.original_dialogue ?? '台词',
    description: overrides.description ?? '画面描述',
    characters: overrides.characters ?? [],
    scene: overrides.scene ?? '',
    props: overrides.props ?? [],
    voice_id: '',
    voice_emotion: '默认',
    speed: 1.0,
    volume: 0.0,
    director_stage_done: false,
    generation_status: overrides.generation_status ?? 'pending',
    is_ai_generated: false,
    asset_ids: [],
    is_locked: false,
    ...overrides,
  } as StoryboardRow;
}

/** 构造项目测试数据 */
function makeProject(id = 'p1', name = '测试项目'): ComicProject {
  return {
    project_id: id,
    name,
    created_at: 1700000000,
    updated_at: 1700000000,
    work_mode: 'regular',
  };
}

/** 首次 import 后的 pristine 快照（含 action 闭包，可整表替换复位） */
const initialMangaState = useMangaStore.getState();

/** 三个全局 store 复位，隔离用例间状态泄漏 */
function resetStores(): void {
  useMangaStore.setState(initialMangaState, true);
  useAppStore.setState({ activeFeature: null, featureBlockedMessage: null, toasts: [] });
  useTaskStore.setState({ tasks: [] });
}

describe('useMangaStore 项目与分镜表状态流转', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetStores();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('openProject：设置当前项目、复位挂接态、拉取分镜行与资产', async () => {
    const rows = [makeRow({ id: 'r1', shot_number: 1 }), makeRow({ id: 'r2', shot_number: 2 })];
    mockedApi.getStoryboardRows.mockResolvedValue(rows);
    mockedApi.listAssets.mockResolvedValue([]);

    const project = makeProject('p1', '项目A');
    await useMangaStore.getState().openProject(project);

    const s = useMangaStore.getState();
    expect(s.currentProject).toEqual({ id: 'p1', name: '项目A', work_mode: 'regular' });
    expect(s.rows).toHaveLength(2);
    expect(s.loading).toBe(false);
    // 资产并行拉取（微任务完成后收敛）
    await Promise.resolve();
    expect(mockedApi.getStoryboardRows).toHaveBeenCalledWith('p1');
    expect(mockedApi.listAssets).toHaveBeenCalledWith('p1');
  });

  it('openProject 失败：loading 收敛为 false 且异常上抛', async () => {
    mockedApi.getStoryboardRows.mockRejectedValue(new Error('网络错误'));
    await expect(
      useMangaStore.getState().openProject(makeProject('p2', '项目B')),
    ).rejects.toThrow('网络错误');
    expect(useMangaStore.getState().loading).toBe(false);
  });

  it('autoSplit：splitting 全程翻转，新行按序追加并返回新增数', async () => {
    useMangaStore.setState({
      currentProject: { id: 'p1', name: 'x' },
      rows: [makeRow({ id: 'r1', shot_number: 1 })],
    });
    let splittingDuringCall = false;
    mockedApi.autoSplitStoryboard.mockImplementation(async () => {
      splittingDuringCall = useMangaStore.getState().splitting;
      return { added: [makeRow({ id: 'r2', shot_number: 2 })], total: 2 };
    });

    const count = await useMangaStore.getState().autoSplit('新剧本');
    const s = useMangaStore.getState();

    expect(count).toBe(1);
    expect(splittingDuringCall).toBe(true);
    expect(s.splitting).toBe(false);
    expect(s.rows.map((r) => r.id)).toEqual(['r1', 'r2']);
  });

  it('reorderRows：乐观重排即时生效，服务端响应覆盖本地顺序', async () => {
    useMangaStore.setState({
      currentProject: { id: 'p1', name: 'x' },
      rows: [
        makeRow({ id: 'a', shot_number: 1 }),
        makeRow({ id: 'b', shot_number: 2 }),
        makeRow({ id: 'c', shot_number: 3 }),
      ],
    });
    // 服务端按新顺序返回且重编 sort_index
    const serverRows = [
      makeRow({ id: 'c', shot_number: 1, sort_index: 1 }),
      makeRow({ id: 'a', shot_number: 2, sort_index: 2 }),
      makeRow({ id: 'b', shot_number: 3, sort_index: 3 }),
    ];
    let localOrderDuringCall: string[] = [];
    mockedApi.reorderStoryboard.mockImplementation(async () => {
      localOrderDuringCall = useMangaStore.getState().rows.map((r) => r.id);
      return serverRows;
    });

    await useMangaStore.getState().reorderRows(['c', 'a', 'b']);
    const s = useMangaStore.getState();

    // 请求在途时本地已乐观重排
    expect(localOrderDuringCall).toEqual(['c', 'a', 'b']);
    // 成功后以服务端行序为准
    expect(s.rows.map((r) => r.id)).toEqual(['c', 'a', 'b']);
    expect(s.rows.map((r) => r.sort_index)).toEqual([1, 2, 3]);
  });

  it('reorderRows 失败：上抛异常并回滚拉取服务端行序', async () => {
    useMangaStore.setState({
      currentProject: { id: 'p1', name: 'x' },
      rows: [makeRow({ id: 'a' }), makeRow({ id: 'b' })],
    });
    mockedApi.reorderStoryboard.mockRejectedValue(new Error('保存失败'));
    mockedApi.getStoryboardRows.mockResolvedValue([
      makeRow({ id: 'a', shot_number: 1 }),
      makeRow({ id: 'b', shot_number: 2 }),
    ]);

    await expect(useMangaStore.getState().reorderRows(['b', 'a'])).rejects.toThrow('保存失败');
    // 回滚路径重新拉取服务端行序
    await vi.waitFor(() => {
      expect(mockedApi.getStoryboardRows).toHaveBeenCalledWith('p1');
    });
    await vi.waitFor(() => {
      expect(useMangaStore.getState().rows.map((r) => r.id)).toEqual(['a', 'b']);
    });
  });

  it('removeProject 删除当前项目：回项目库并清空全部挂接态', async () => {
    useMangaStore.setState({
      projects: [makeProject('p1', '当前'), makeProject('p2', '其他')],
      currentProject: { id: 'p1', name: '当前' },
      rows: [makeRow()],
      selectedRowId: 'row-1',
      assetsLoaded: true,
      selectedAssetId: 'asset-1',
      bindingTarget: { rowId: 'row-1', kind: 'character' },
    });
    mockedApi.deleteProject.mockResolvedValue(undefined);

    await useMangaStore.getState().removeProject('p1');
    const s = useMangaStore.getState();

    expect(s.projects.map((p) => p.project_id)).toEqual(['p2']);
    expect(s.currentProject).toBeNull();
    expect(s.rows).toEqual([]);
    expect(s.selectedRowId).toBeNull();
    expect(s.selectedAssetId).toBeNull();
    expect(s.bindingTarget).toBeNull();
    expect(s.assetsLoaded).toBe(false);
  });

  it('closeProject：清空工作区状态（无 API 调用）', () => {
    useMangaStore.setState({
      currentProject: { id: 'p1', name: 'x' },
      rows: [makeRow()],
      videoTasks: [],
    });
    useMangaStore.getState().closeProject();
    const s = useMangaStore.getState();
    expect(s.currentProject).toBeNull();
    expect(s.rows).toEqual([]);
    expect(s.keyframes).toEqual({});
  });
});

describe('useMangaStore 视频生成状态流转', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetStores();
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  /** 发起视频生成并等待首个轮询 tick 收敛 */
  async function launchVideo(): Promise<void> {
    useMangaStore.setState({
      currentProject: { id: 'p1', name: 'x' },
      rows: [makeRow({ id: 'row-1', shot_number: 3 })],
    });
    mockedApi.generateVideo.mockResolvedValue({ task_id: 'task-1', status: 'generating' });
    await useMangaStore.getState().generateVideo(makeRow({ id: 'row-1', shot_number: 3 }));
    // 首个立即 tick 的微任务链
    await vi.advanceTimersByTimeAsync(0);
  }

  it('generateVideo 全流程：任务入列 → generating → done 收敛并释放功能锁', async () => {
    mockedApi.getVideoStatus
      .mockResolvedValueOnce({
        task_id: 'task-1',
        status: 'generating',
        progress: 0.4,
      })
      .mockResolvedValue({
        task_id: 'task-1',
        status: 'done',
        progress: 1,
      });

    await launchVideo();

    // 首次 tick 后：任务 generating、行状态同步、功能锁持有
    let s = useMangaStore.getState();
    expect(s.videoTasks).toHaveLength(1);
    expect(s.videoTasks[0].status).toBe('generating');
    expect(s.videoTasks[0].row_id).toBe('row-1');
    expect(s.videoTasks[0].shot_number).toBe(3);
    expect(s.videoGenerating).toBe(true);
    expect(useAppStore.getState().activeFeature).toBe('video_gen');
    expect(s.rows[0].generation_status).toBe('generating');

    // 下一轮轮询（2s 间隔）命中 done 终态
    await vi.advanceTimersByTimeAsync(2000);

    s = useMangaStore.getState();
    expect(s.videoTasks[0].status).toBe('done');
    expect(s.videoTasks[0].progress).toBe(1);
    expect(s.videoTasks[0].download_url).toContain('/manga/video/task-1/download');
    expect(s.videoGenerating).toBe(false);
    expect(useAppStore.getState().activeFeature).toBeNull();
    expect(s.rows[0].generation_status).toBe('done');
    // 全局任务条同步完成
    const task = useTaskStore.getState().tasks.find((t) => t.id === 'task-1');
    expect(task?.status).toBe('done');
  });

  it('generateVideo 失败：功能锁释放、返回 false、行状态回 pending', async () => {
    useMangaStore.setState({
      currentProject: { id: 'p1', name: 'x' },
      rows: [makeRow({ id: 'row-1' })],
    });
    mockedApi.generateVideo.mockRejectedValue(
      Object.assign(new Error('模型未加载'), { code: 'MODEL_NOT_READY' }),
    );

    const ok = await useMangaStore.getState().generateVideo(makeRow({ id: 'row-1' }));
    const s = useMangaStore.getState();

    expect(ok).toBe(false);
    expect(s.videoTasks).toHaveLength(0);
    expect(s.videoGenerating).toBe(false);
    expect(useAppStore.getState().activeFeature).toBeNull();
    expect(useAppStore.getState().toasts.at(-1)?.level).toBe('error');
  });

  it('功能互斥：已有活跃功能锁时直接拒绝（返回 false）', async () => {
    useAppStore.setState({ activeFeature: 'paint' });
    const ok = await useMangaStore.getState().generateVideo(makeRow({ id: 'row-1' }));
    expect(ok).toBe(false);
    expect(mockedApi.generateVideo).not.toHaveBeenCalled();
  });

  it('轮询连续失败 3 次：任务收敛为 error 终态、行状态同步、功能锁释放', async () => {
    mockedApi.getVideoStatus.mockRejectedValue(new Error('超时'));

    await launchVideo();
    // 两次周期性 tick（首次立即 tick 已计第 1 次失败）
    await vi.advanceTimersByTimeAsync(2000);
    await vi.advanceTimersByTimeAsync(2000);

    const s = useMangaStore.getState();
    // 修复验证：放弃轮询必须收敛终态，否则 video_gen 锁永久挂死
    expect(s.videoTasks[0].status).toBe('error');
    expect(s.videoTasks[0].error).toContain('停止轮询');
    expect(s.videoGenerating).toBe(false);
    expect(useAppStore.getState().activeFeature).toBeNull();
    expect(s.rows[0].generation_status).toBe('error');
    const lastToast = useAppStore.getState().toasts.at(-1);
    expect(lastToast?.text).toContain('停止轮询');
  });

  it('cancelVideo：任务置为已取消并同步行状态为 error', async () => {
    mockedApi.getVideoStatus.mockResolvedValue({
      task_id: 'task-1',
      status: 'generating',
      progress: 0.2,
    });
    await launchVideo();
    mockedApi.cancelVideo.mockResolvedValue(undefined);

    await useMangaStore.getState().cancelVideo('task-1');
    const s = useMangaStore.getState();

    expect(s.videoTasks[0].status).toBe('error');
    expect(s.videoTasks[0].error).toBe('已手动取消');
    expect(s.rows[0].generation_status).toBe('error');
    expect(mockedApi.cancelVideo).toHaveBeenCalledWith('task-1');
  });

  it('removeVideoTask：条目移除且空闲时释放功能锁', async () => {
    mockedApi.getVideoStatus.mockResolvedValue({
      task_id: 'task-1',
      status: 'generating',
      progress: 0.2,
    });
    await launchVideo();
    expect(useAppStore.getState().activeFeature).toBe('video_gen');

    useMangaStore.getState().removeVideoTask('task-1');
    const s = useMangaStore.getState();
    expect(s.videoTasks).toHaveLength(0);
    expect(s.videoGenerating).toBe(false);
    expect(useAppStore.getState().activeFeature).toBeNull();
  });
});
