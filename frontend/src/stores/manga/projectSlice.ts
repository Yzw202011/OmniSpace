/* 漫剧项目切片（TASK-P2-01）：项目列表 CRUD + 工作区进出 */

import type { StateCreator } from 'zustand';
import * as mangaApi from '@/services/mangaApi';
import { reportBgError } from '@/utils/errors';
import { stopVideoPoll, videoPollers } from './videoPoller';
import type { MangaState, ProjectSlice } from './types';

export const createProjectSlice: StateCreator<MangaState, [], [], ProjectSlice> = (set) => ({
  projects: [],
  projectsLoaded: false,
  projectsLoading: false,
  currentProject: null,

  fetchProjects: async () => {
    set({ projectsLoading: true });
    try {
      const projects = await mangaApi.listProjects();
      set({ projects, projectsLoaded: true, projectsLoading: false });
    } catch (err) {
      set({ projectsLoading: false });
      throw err;
    }
  },

  createProject: async (name, template, workMode) => {
    const res = await mangaApi.createProject(name, template, workMode);
    // 重新拉取列表（创建端点返回不含完整时间戳字段，以服务端为准）
    const projects = await mangaApi.listProjects();
    set({ projects, projectsLoaded: true });
    return res.project_id;
  },

  renameProject: async (projectId, name) => {
    await mangaApi.renameProject(projectId, name);
    set((state) => ({
      projects: state.projects.map((p) =>
        p.project_id === projectId ? { ...p, name } : p,
      ),
      currentProject:
        state.currentProject?.id === projectId
          ? { ...state.currentProject, name }
          : state.currentProject,
    }));
  },

  removeProject: async (projectId) => {
    await mangaApi.deleteProject(projectId);
    set((state) => {
      const isCurrent = state.currentProject?.id === projectId;
      // 清理当前项目的轮询器与任务状态
      if (isCurrent) {
        for (const taskId of videoPollers.keys()) {
          stopVideoPoll(taskId);
        }
        videoPollers.clear();
      }
      return {
        projects: state.projects.filter((p) => p.project_id !== projectId),
        // 删除当前项目：回项目库并清空挂接态
        ...(isCurrent
          ? {
              currentProject: null,
              rows: [],
              selectedRowId: null,
              assets: [],
              assetsLoaded: false,
              keyframes: {},
              selectedAssetId: null,
              bindingTarget: null,
              videoTasks: [],
            }
          : {}),
      };
    });
  },

  openProject: async (project) => {
    set({
      currentProject: { id: project.project_id, name: project.name, work_mode: project.work_mode },
      rows: [],
      selectedRowId: null,
      assets: [],
      assetsLoaded: false,
      keyframes: {},
      selectedAssetId: null,
      bindingTarget: null,
      loading: true,
    });
    try {
      const rows = await mangaApi.getStoryboardRows(project.project_id);
      set({ rows, loading: false });
    } catch (err) {
      set({ loading: false });
      throw err;
    }
    // 资产并行拉取（失败不阻塞分镜工作区，CONSOLE 级留痕）
    void mangaApi
      .listAssets(project.project_id)
      .then((assets) => set({ assets, assetsLoaded: true }))
      .catch((err) => {
        reportBgError('projectSlice.listAssets', err);
        set({ assetsLoaded: true });
      });
  },

  closeProject: () => {
    // 清理所有轮询器与任务状态
    for (const taskId of videoPollers.keys()) {
      stopVideoPoll(taskId);
    }
    videoPollers.clear();
    set({
      currentProject: null,
      rows: [],
      selectedRowId: null,
      assets: [],
      assetsLoaded: false,
      keyframes: {},
      selectedAssetId: null,
      bindingTarget: null,
      videoTasks: [],
    });
  },

  setProject: (project) => set({ currentProject: project }),
});
