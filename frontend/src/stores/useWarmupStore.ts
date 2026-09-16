/* ==========================================================================
 * OmniSpace AI v2.5.0 —— 对话模型冷启动弹窗状态（2026-08-23）
 * --------------------------------------------------------------------------
 * 进入对话页点火预热（POST /models/warmup fire-and-forget）后，冷启动
 * 约 2-3 分钟（vLLM ~157s 实测）。此前仅一条 toast 提醒，用户无感知进度；
 * 现改为全局弹窗 + 时间渐近进度条（后端无细粒度进度接口，进度按已耗时
 * 曲线逼近 90%，就绪信号 GET /dialog/status state=ready 校正跳 100%）。
 * 弹窗可「后台继续」关闭，加载不中断；预热被模块切换终止时自动静默关闭。
 * ========================================================================== */

import { create } from 'zustand';

/** 对话模型预热弹窗状态 */
export interface WarmupState {
  /** 弹窗是否可见 */
  visible: boolean;
  /** 模型是否已就绪（就绪态展示成功样式后自动关闭） */
  done: boolean;
  /** 点火时刻（Date.now()，进度曲线自变量） */
  startedAt: number;
  /** 预热目标模型（展示用，空 = 引擎默认） */
  modelId: string;
  /** 预热目标模块（弹窗分流：dialog=vLLM 对话 / paint=AI 绘画管线） */
  kind: 'dialog' | 'paint';
  /** 弹窗开启（App.tsx 预热点火 started=true 时调用） */
  begin: (modelId?: string, kind?: 'dialog' | 'paint') => void;
  /** 模型就绪（轮询检测 state=ready 时调用） */
  finish: () => void;
  /** 关闭弹窗（用户点「后台继续」/被中断/自动关闭，不影响后台加载） */
  dismiss: () => void;
}

export const useWarmupStore = create<WarmupState>((set) => ({
  visible: false,
  done: false,
  startedAt: 0,
  modelId: '',
  kind: 'dialog',
  begin: (modelId = '', kind = 'dialog') =>
    set({ visible: true, done: false, startedAt: Date.now(), modelId, kind }),
  finish: () => set({ done: true }),
  dismiss: () => set({ visible: false }),
}));

export default useWarmupStore;
// 本项目仅供学习使用，商业授权请+Q 3559331368
