/* ==========================================================================
 * OmniSpace AI v2.3.1 —— AI漫画页（漫画模块 M1，2026-09-07）
 * --------------------------------------------------------------------------
 * 漫画模块的产品面入口，替代原 AI 绘画页（/paint 路由）。
 * 生成底座全部复用漫剧模块既有链路（docs/漫画模块立项方案-2026-09-07.md）：
 *   - 项目/分格 = projects(project_type='comic') + storyboard_rows
 *   - 分格出图 = 关键帧链（comfy+PuLID 身份硬锁 / diffusers 降级）
 *   - 角色引用 = comic_assets（四视图资产自动触发 PuLID face_ref）
 *   - 画风 = 项目级 art_style（ART_STYLES 预置 + 自定义风格包）
 * 库 ⇄ 工作台两级结构（与漫剧 MangaPage 同构，本地状态不建全局 store）。
 * ========================================================================== */

import { useState } from 'react';
import type { ComicProject } from '@/types';
import ComicLibrary from './ComicLibrary';
import ComicWorkspace from './ComicWorkspace';

export default function ComicPage() {
  const [project, setProject] = useState<ComicProject | null>(null);

  if (project) {
    return (
      <ComicWorkspace
        project={project}
        onExit={() => setProject(null)}
        onProjectUpdated={(p) => setProject((cur) => (cur ? { ...cur, ...p } : cur))}
      />
    );
  }
  return (
    <ComicLibrary
      onOpen={(p) => setProject(p)}
    />
  );
}
