/* ==========================================================================
 * OmniSpace AI v2.1 —— 模型管理状态（规格 §4.5 模型管理）
 * --------------------------------------------------------------------------
 * 职责：
 *   - models 列表（按类别分组）
 *   - selectedModels 各功能当前选择的模型（feature → model_id）
 *   - import / verify / delete / select / load / unload 操作
 * 非大模型功能，始终可用（不参与功能互斥）。
 * 说明：后端模型以 id 为唯一键（播种数据 id === name），本 store 一律按 id 匹配。
 * ========================================================================== */

import { create } from 'zustand';
import type { ModelInfo, ModelCategory } from '@/types';
import * as modelApi from '@/services/modelApi';

/** 各功能当前选择的模型（feature → model_id） */
export type SelectedModels = Partial<Record<string, string>>;

/** 模型管理状态 */
export interface ModelState {
  /** 全部模型列表 */
  models: ModelInfo[];
  /** 是否已加载 */
  loaded: boolean;
  /** 加载中 */
  loading: boolean;
  /** 各功能当前选择的模型 */
  selectedModels: SelectedModels;

  /* ------------------------------ 动作 ------------------------------ */
  /** 拉取模型列表 */
  fetchModels: (category?: ModelCategory) => Promise<void>;
  /** 按类别分组 */
  getGroupedModels: () => Record<ModelCategory, ModelInfo[]>;
  /** 导入模型 */
  importModel: (body: modelApi.ImportModelBody) => Promise<ModelInfo>;
  /** 校验模型完整性（SHA256） */
  verifyModel: (modelId: string) => Promise<modelApi.VerifyResult>;
  /** 删除模型（从注册表移除） */
  deleteModel: (modelId: string) => Promise<void>;
  /** 选择模型为某功能当前使用（PUT /models/select） */
  selectModel: (modelId: string, feature: modelApi.SelectableFeature) => Promise<void>;
  /** 加载模型到 GPU 显存 */
  loadModel: (modelId: string) => Promise<void>;
  /** 从 GPU 卸载模型 */
  unloadModel: (modelId: string) => Promise<void>;
}

/** 全部模型类别 */
const ALL_CATEGORIES: ModelCategory[] = [
  'dialog',
  'video',
  'voice',
  'vision',
  'language',
  '3d',
  'auxiliary',
];

export const useModelStore = create<ModelState>((set, get) => ({
  models: [],
  loaded: false,
  loading: false,
  selectedModels: {},

  fetchModels: async (category) => {
    set({ loading: true });
    try {
      const list = await modelApi.listModels(category ? { category } : undefined);
      set({ models: list || [], loaded: true });
    } catch {
      // 后端未就绪时静默
      set({ loaded: true });
    } finally {
      set({ loading: false });
    }
  },

  getGroupedModels: () => {
    const grouped = {} as Record<ModelCategory, ModelInfo[]>;
    for (const cat of ALL_CATEGORIES) {
      grouped[cat] = [];
    }
    for (const m of get().models) {
      if (grouped[m.category]) {
        grouped[m.category].push(m);
      } else {
        // 未知类别归入 auxiliary
        grouped.auxiliary.push(m);
      }
    }
    return grouped;
  },

  importModel: async (body) => {
    const model = await modelApi.importModel(body);
    set((state) => ({ models: [...state.models, model] }));
    return model;
  },

  verifyModel: async (modelId) => {
    const result = await modelApi.verifyModel(modelId);
    // 回填校验指纹
    set((state) => ({
      models: state.models.map((m) =>
        m.id === modelId ? { ...m, sha256: result.sha256 } : m,
      ),
    }));
    return result;
  },

  deleteModel: async (modelId) => {
    await modelApi.deleteModel(modelId);
    set((state) => ({
      models: state.models.filter((m) => m.id !== modelId),
      // 清理选择引用
      selectedModels: Object.fromEntries(
        Object.entries(state.selectedModels).filter(([, mid]) => mid !== modelId),
      ),
    }));
  },

  selectModel: async (modelId, feature) => {
    await modelApi.selectModel({ feature, model_id: modelId });
    set((state) => ({
      selectedModels: { ...state.selectedModels, [feature]: modelId },
    }));
  },

  loadModel: async (modelId) => {
    const result = await modelApi.loadModel({ model_id: modelId });
    set((state) => ({
      models: state.models.map((m) =>
        m.id === modelId ? { ...m, loaded: result.loaded } : m,
      ),
    }));
  },

  unloadModel: async (modelId) => {
    await modelApi.unloadModel(modelId);
    set((state) => ({
      models: state.models.map((m) =>
        m.id === modelId ? { ...m, loaded: false } : m,
      ),
    }));
  },
}));

export default useModelStore;
