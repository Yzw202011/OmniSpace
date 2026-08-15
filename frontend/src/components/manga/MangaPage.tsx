/* ==========================================================================
 * OmniSpace AI v2.3.1 —— 漫剧创作页（容器组件，库 ↔ 工作区 切换）
 * --------------------------------------------------------------------------
 * 竞品工作流布局（2026-08-12 重构）：
 *   - 项目库 MangaLibrary：项目卡片网格 + 新建/重命名/删除（/comic/project/*）
 *   - 工作区 MangaWorkspace：7 步向导（剧本导入 → 分镜表 → 资产库 →
 *     AI 描述 → 3D 导演台 → 视频生成 → 导出）+ 左侧资产栏 + 音色绑定
 * 切换依据：useMangaStore.currentProject（openProject 进入工作区，
 *   closeProject / 删除当前项目回项目库）。
 * ========================================================================== */

import { useMangaStore } from '@/stores/useMangaStore';
import MangaLibrary from './MangaLibrary';
import MangaWorkspace from './MangaWorkspace';

export default function MangaPage() {
  const currentProject = useMangaStore((s) => s.currentProject);
  return currentProject ? <MangaWorkspace /> : <MangaLibrary />;
}
