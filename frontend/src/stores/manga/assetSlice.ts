/* 漫剧资产切片（TASK-P2-01）：资产库拉取 / 行绑定 toggle / 面板状态 */

import type { StateCreator } from 'zustand';
import * as mangaApi from '@/services/mangaApi';
import { currentPid } from './helpers';
import type { AssetSlice, MangaState } from './types';

export const createAssetSlice: StateCreator<MangaState, [], [], AssetSlice> = (set, get) => ({
  assets: [],
  assetsLoaded: false,
  assetsLoading: false,
  selectedAssetId: null,
  bindingTarget: null,

  fetchAssets: async (kind) => {
    set({ assetsLoading: true });
    try {
      const assets = await mangaApi.listAssets(currentPid(get), kind);
      set({ assets, assetsLoaded: true, assetsLoading: false });
    } catch (err) {
      set({ assetsLoading: false });
      throw err;
    }
  },

  bindAssetToRow: async (assetId, rowId) => {
    // toggle 语义：目标行 asset_ids 已含该资产 → unbind，否则 → bind（追加去重）
    const row = get().rows.find((r) => r.id === rowId);
    const bound = (row?.asset_ids ?? (row?.asset_id ? [row.asset_id] : [])).includes(assetId);
    const res = bound
      ? await mangaApi.unbindAsset(assetId, rowId)
      : await mangaApi.bindAsset(assetId, rowId);
    // 以响应 asset_ids 为唯一真实来源更新行（不合并局部推测）
    set((state) => ({
      rows: state.rows.map((r) =>
        r.id === rowId ? { ...r, asset_ids: res.asset_ids, asset_id: res.asset_id } : r,
      ),
    }));
  },

  setSelectedAsset: (assetId) => set({ selectedAssetId: assetId }),

  setBindingTarget: (target) => set({ bindingTarget: target }),
});
// 本项目仅供学习使用，商业授权请+Q 3559331368
