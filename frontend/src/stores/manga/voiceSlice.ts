/* 漫剧音色切片（TASK-P2-01）：音色列表 / 角色绑定 / 情感 / 试听 */

import type { StateCreator } from 'zustand';
import * as mangaApi from '@/services/mangaApi';
import type { MangaState, VoiceSlice } from './types';

export const createVoiceSlice: StateCreator<MangaState, [], [], VoiceSlice> = (set) => ({
  voices: [],
  voicesLoaded: false,

  fetchVoices: async () => {
    try {
      const items = await mangaApi.listVoices();
      set({ voices: items, voicesLoaded: true });
    } catch {
      /* 后端未就绪：标记已加载避免反复请求，列表保持空态 */
      set({ voicesLoaded: true });
    }
  },

  bindVoice: async (voiceId, characterId) => {
    await mangaApi.bindVoice(voiceId, characterId);
    // 后端语义：解除该角色旧绑定后绑定新音色 → 本地同步收敛
    set((state) => ({
      voices: state.voices.map((v) => {
        if (v.id === voiceId) return { ...v, character_id: characterId };
        if (v.character_id === characterId) return { ...v, character_id: '' };
        return v;
      }),
    }));
  },

  updateVoiceEmotion: async (voiceId, emotionLabel) => {
    await mangaApi.updateVoiceEmotion(voiceId, emotionLabel);
    set((state) => ({
      voices: state.voices.map((v) =>
        v.id === voiceId ? { ...v, emotion: emotionLabel } : v,
      ),
    }));
  },

  previewVoice: async (voiceId, text, emotion) => {
    const res = await mangaApi.previewVoice(voiceId, text, emotion);
    const degraded =
      typeof (res as { degraded?: unknown }).degraded === 'boolean'
        ? ((res as { degraded?: boolean }).degraded as boolean)
        : undefined;
    const degradeReason =
      typeof (res as { degrade_reason?: unknown }).degrade_reason === 'string'
        ? ((res as { degrade_reason?: string }).degrade_reason as string)
        : undefined;
    return { audio: res.audio, format: res.format, degraded, degrade_reason: degradeReason };
  },
});
