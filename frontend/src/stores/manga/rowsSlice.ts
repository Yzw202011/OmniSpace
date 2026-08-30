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
    set({ splitting: true });
    try {
      const res = await mangaApi.autoSplitStoryboard(currentPid(get), script);
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
    set({ splitting: true });
    try {
      const res = await mangaApi.autoSplitCommit(currentPid(get), splitId);
      set((state) => ({ rows: [...state.rows, ...res.added] }));
      return res.added.length;
    } finally {
      set({ splitting: false });
    }
  },

  importScript: async (script) => {
    const res = await mangaApi.importScript(currentPid(get), script);
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
