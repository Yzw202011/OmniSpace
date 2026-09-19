/* ==========================================================================
 * OmniSpace AI v2.5.0 —— 漫剧创作页（容器组件，库 ↔ 工作区 切换）
 * --------------------------------------------------------------------------
 * 竞品工作流布局（2026-08-12 重构）：
 *   - 项目库 MangaLibrary：项目卡片网格 + 新建/重命名/删除（/comic/project/*）
 *   - 工作区 MangaWorkspace：向导流（剧本导入 → 分镜表 → 资产库 →
 *     AI 描述 → 视频生成 → 导出；3D 导演台已于 2026-08-29 整链路移除）
 *     + 左侧资产栏 + 音色绑定
 * 切换依据：useMangaStore.currentProject（openProject 进入工作区，
 *   closeProject / 删除当前项目回项目库）。
 * ========================================================================== */

import { useMangaStore } from '@/stores/useMangaStore';
import ModuleGuide from '@/components/common/ModuleGuide';
import MangaLibrary from './MangaLibrary';
import MangaWorkspace from './MangaWorkspace';

export default function MangaPage() {
  const currentProject = useMangaStore((s) => s.currentProject);
  return (
    <>
      <ModuleGuide moduleKey="manga" />
      {currentProject ? <MangaWorkspace /> : <MangaLibrary />}
    </>
  );
}
// 本项目仅供学习使用，商业授权请+Q 3559331368
