/* 漫剧分镜表切片（TASK-P2-01）：行 CRUD / 自动分镜 / 剧本导入 / 拖拽重排 */

import type { StateCreator } from 'zustand';
import * as mangaApi from '@/services/mangaApi';
import { reportBgError } from '@/utils/errors';
import { currentPid } from './helpers';
import type { MangaState, RowsSlice } from './types';

export const createRowsSlice: StateCreator<MangaState, [], [], RowsSlice> = (set, get) => ({
  rows: [],
  loading: false,
  splitting: false,
  selectedRowId: null,

  setSelectedRow: (rowId) => set({ selectedRowId: rowId }),

  fetchRows: async (projectId) => {
    const target = projectId ?? currentPid(get);
    set({ loading: true });
    try {
      const rows = await mangaApi.getStoryboardRows(target);
      set({ rows, loading: false });
      return rows;
    } catch (err) {
      set({ loading: false });
      throw err;
    }
  },

  saveRows: async (rows) => {
    const saved = await mangaApi.saveStoryboardRows(currentPid(get), rows);
    set({ rows: saved });
    return saved;
  },

  updateRow: async (rowId, patch) => {
    const row = await mangaApi.updateStoryboardRow(currentPid(get), rowId, patch);
    set((state) => ({
      rows: state.rows.map((r) => (r.id === rowId ? { ...r, ...row } : r)),
    }));
  },

  autoSplit: async (script) => {
    // R6-AF2 竞态守卫（2026-09-20）：记录发起时的项目，响应落地前若已
    // 切换项目则丢弃——原实现会把 A 项目的拆分行追加进 B 项目并随下次
    // 全量保存持久化进 B 库（跨项目串数据）。
    const pidAtStart = currentPid(get);
    set({ splitting: true });
    try {
      const res = await mangaApi.autoSplitStoryboard(pidAtStart, script);
      if (currentPid(get) !== pidAtStart) return res.added.length;
      set((state) => ({ rows: [...state.rows, ...res.added] }));
      return res.added.length;
    } finally {
      set({ splitting: false });
    }
  },

  /** AI 镜头级分镜预览（dry_run 不落库，2026-08-23 真分镜改造） */
  autoSplitPreview: async (script) => {
    set({ splitting: true });
    try {
      return await mangaApi.autoSplitPreview(currentPid(get), script);
    } finally {
      set({ splitting: false });
    }
  },

  /** AI 分镜确认落库（复用预览 split_id，免二次推理） */
  autoSplitCommit: async (splitId) => {
    // R6-AF2 竞态守卫：同 autoSplit——迟到响应不跨项目追加。
    const pidAtStart = currentPid(get);
    set({ splitting: true });
    try {
      const res = await mangaApi.autoSplitCommit(pidAtStart, splitId);
      if (currentPid(get) !== pidAtStart) return res.added.length;
      set((state) => ({ rows: [...state.rows, ...res.added] }));
      return res.added.length;
    } finally {
      set({ splitting: false });
    }
  },

  importScript: async (script) => {
    // R6-AF2 竞态守卫：同 autoSplit——迟到响应不跨项目追加。
    const pidAtStart = currentPid(get);
    const res = await mangaApi.importScript(pidAtStart, script);
    if (currentPid(get) !== pidAtStart) return res.rows.length;
    set((state) => ({ rows: [...state.rows, ...res.rows] }));
    return res.rows.length;
  },

  reorderRows: async (rowIds) => {
    // 乐观更新本地顺序（sort_index 连续重编，与服务端语义一致）
    const orderMap = new Map(rowIds.map((id, i) => [id, i + 1]));
    set((state) => ({
      rows: [...state.rows]
        .sort(
          (a, b) =>
            (orderMap.get(a.id) ?? Number.MAX_SAFE_INTEGER) -
            (orderMap.get(b.id) ?? Number.MAX_SAFE_INTEGER),
        )
        .map((r) => {
          const idx = orderMap.get(r.id);
          return idx !== undefined ? { ...r, sort_index: idx } : r;
        }),
    }));
    try {
      const rows = await mangaApi.reorderStoryboard(rowIds, currentPid(get));
      set({ rows });
    } catch (err) {
      // 失败回滚：重新拉取服务端行序（拉取本身失败属后台刷新，CONSOLE 级）
      void get()
        .fetchRows()
        .catch((rollbackErr) => reportBgError('rowsSlice.reorderRollback', rollbackErr));
      throw err;
    }
  },
});
// 本项目仅供学习使用，商业授权请+Q 3559331368
