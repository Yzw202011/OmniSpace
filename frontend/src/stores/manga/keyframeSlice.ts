// 本项目仅供学习使用，商业授权请+Q 3559331368
/* 漫剧关键帧切片（TASK-P2-01）：版本缓存拉取与失效 */

import type { StateCreator } from 'zustand';
import * as mangaApi from '@/services/mangaApi';
import type { KeyframeSlice, MangaState } from './types';

export const createKeyframeSlice: StateCreator<MangaState, [], [], KeyframeSlice> = (set) => ({
  keyframes: {},

  fetchKeyframes: async (rowId) => {
    const items = await mangaApi.listKeyframes(rowId);
    set((state) => ({
      keyframes: { ...state.keyframes, [rowId]: items },
    }));
    return items;
  },

  invalidateKeyframes: (rowId) =>
    set((state) => {
      const next = { ...state.keyframes };
      delete next[rowId];
      return { keyframes: next };
    }),
});
