/** 漫剧切片共享辅助（TASK-P2-01） */

import type { MangaState } from './types';

/** 当前项目 ID（未设置时回退默认项目，与 MangaPage DEFAULT_PROJECT_ID 一致） */
export const currentPid = (get: () => MangaState): string =>
  get().currentProject?.id ?? 'default';
