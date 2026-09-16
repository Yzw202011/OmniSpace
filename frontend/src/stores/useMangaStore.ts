// 本项目仅供学习使用，商业授权请+Q 3559331368
/* ==========================================================================
 * OmniSpace AI v2.5.0 —— 漫剧状态入口（TASK-P2-01 按子域切片后的组装点）
 * --------------------------------------------------------------------------
 * 原单一 744 行 store 拆为 ./manga/ 六切片（project/rows/asset/keyframe/
 * voice/video），此处组合为单一 useMangaStore——对外接口与切片前完全一致，
 * 22 个消费方零改动。切片契约与后端能力边界说明见 ./manga/types.ts。
 * ========================================================================== */

import { create } from 'zustand';
import { createProjectSlice } from './manga/projectSlice';
import { createRowsSlice } from './manga/rowsSlice';
import { createAssetSlice } from './manga/assetSlice';
import { createKeyframeSlice } from './manga/keyframeSlice';
import { createVoiceSlice } from './manga/voiceSlice';
import { createVideoSlice } from './manga/videoSlice';
import type { MangaState } from './manga/types';

export type {
  MangaProjectRef,
  MangaState,
  MangaVideoTask,
} from './manga/types';

export const useMangaStore = create<MangaState>()((...args) => ({
  ...createProjectSlice(...args),
  ...createRowsSlice(...args),
  ...createAssetSlice(...args),
  ...createKeyframeSlice(...args),
  ...createVoiceSlice(...args),
  ...createVideoSlice(...args),
}));

export default useMangaStore;
