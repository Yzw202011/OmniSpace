/* 漫剧资产切片（TASK-P2-01）：资产库拉取 / 行绑定 toggle / 面板状态 */

import type { StateCreator } from 'zustand';
import * as mangaApi from '@/services/mangaApi';
import { currentPid } from './helpers';
import type { AssetSlice, MangaState } from './types';

export const createAssetSlice: StateCreator<MangaState, [], [], AssetSlice> = (set, get) => ({
  assets: [],
  globalAssets: [],
  assetsLoaded: false,
  assetsLoading: false,
  selectedAssetId: null,
  bindingTarget: null,

  fetchAssets: async (kind) => {
    set({ assetsLoading: true });
    try {
      // 项目清单 + 该面全局清单并行拉：分镜行绑定引用可能指向全局资产
      // （离开项目页自动转全局后），只查项目清单会让资产列渲染成空——
      // 2026-10-03 绑定显示丢失根修（DB 绑定完好，纯查找表缺成员）
      const [assets, globalAssets] = await Promise.all([
        // borrowedFor=本项目：借用中的全局资产并入项目清单（2026-10-05 借用引入）
        mangaApi.listAssets(currentPid(get), kind, undefined, undefined, currentPid(get)),
        mangaApi.listAssets('', kind, 'global', 'manga'),
      ]);
      set({ assets, globalAssets, assetsLoaded: true, assetsLoading: false });
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
