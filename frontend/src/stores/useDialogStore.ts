/* ==========================================================================
 * OmniSpace AI v2.1 —— 对话状态（规格 §4.1 对话端点 + §5.3 WebSocket 流式）
 * --------------------------------------------------------------------------
 * 职责：
 *   - sessions 会话列表 / currentSession 当前会话 / messages 消息列表
 *   - sendMessage 流式发送（WebSocket ws://.../dialog/stream/{session_id}）
 *   - createSession / deleteSession 会话管理
 *   - 流式逐 token 追加到 assistant 消息
 * 诚实门控：流内 meta/error 如实返回，未就绪返回 40002，绝不伪造 AI 回复。
 * 功能互斥：对话为重量级功能，发送前检查 activeFeature（规格 §6.1）。
 * ========================================================================== */

import { create } from 'zustand';
import type { DialogMessage, DialogSession } from '@/types';
import * as dialogApi from '@/services/dialogApi';
import { getDialogStream, releaseDialogStream } from '@/services/ws';
import { useAppStore } from './useAppStore';

/* ------------------------------ 对话生成参数（规格 §6.2.1 参数控制） ------------------------------ */

/** 可选模型档位（规格 §6.2.1：模型选择 8B/4B/2B） */
export type DialogModelSize = '8B' | '4B' | '2B';

/** 模型档位清单（右侧面板模型选择器数据源） */
export const DIALOG_MODEL_OPTIONS: DialogModelSize[] = ['8B', '4B', '2B'];

/** 上下文长度档位（tokens，规格 §6.2.1：上下文长度选择） */
export const CONTEXT_TOKEN_OPTIONS = [2048, 4096, 8192] as const;

/** 温度范围（规格 §6.2.1：温度滑块 0~2） */
export const TEMPERATURE_MIN = 0;
export const TEMPERATURE_MAX = 2;

/** 对话参数本地持久化键（localStorage） */
const DIALOG_PARAMS_STORAGE_KEY = 'omnispace.dialog.params';

/** 需持久化的对话参数 */
interface DialogParamsSnapshot {
  model: DialogModelSize;
  temperature: number;
  contextTokens: number;
}

/** 从 localStorage 恢复对话参数（非法值回退默认，绝不抛错阻断启动） */
function loadDialogParams(): DialogParamsSnapshot {
  const fallback: DialogParamsSnapshot = { model: '4B', temperature: 0.7, contextTokens: 4096 };
  try {
    const raw = localStorage.getItem(DIALOG_PARAMS_STORAGE_KEY);
    if (!raw) {
      return fallback;
    }
    const parsed = JSON.parse(raw) as Partial<DialogParamsSnapshot>;
    const model = DIALOG_MODEL_OPTIONS.includes(parsed.model as DialogModelSize)
      ? (parsed.model as DialogModelSize)
      : fallback.model;
    const temperature =
      typeof parsed.temperature === 'number' &&
      parsed.temperature >= TEMPERATURE_MIN &&
      parsed.temperature <= TEMPERATURE_MAX
        ? parsed.temperature
        : fallback.temperature;
    const contextTokens = (CONTEXT_TOKEN_OPTIONS as readonly number[]).includes(
      parsed.contextTokens as number,
    )
      ? (parsed.contextTokens as number)
      : fallback.contextTokens;
    return { model, temperature, contextTokens };
  } catch {
    return fallback;
  }
}

/** 持久化对话参数到 localStorage（失败静默，不影响交互） */
function persistDialogParams(snapshot: DialogParamsSnapshot): void {
  try {
    localStorage.setItem(DIALOG_PARAMS_STORAGE_KEY, JSON.stringify(snapshot));
  } catch {
    /* 隐私模式等写入失败场景静默 */
  }
}

/** 对话状态 */
export interface DialogState {
  /* ------------------------------ 数据 ------------------------------ */
  /** 会话列表 */
  sessions: DialogSession[];
  /** 当前会话 */
  currentSession: DialogSession | null;
  /** 当前会话消息列表 */
  messages: DialogMessage[];
  /** 是否正在生成（流式中） */
  generating: boolean;
  /** 是否已加载会话列表 */
  sessionsLoaded: boolean;

  /* ------------------------------ 生成参数（规格 §6.2.1） ------------------------------ */
  /** 模型档位（8B/4B/2B，默认 4B 均衡档） */
  model: DialogModelSize;
  /** 采样温度（0~2，默认 0.7） */
  temperature: number;
  /** 上下文长度（tokens） */
  contextTokens: number;

  /* ------------------------------ 内部 ------------------------------ */
  /** 当前流式连接的解订函数集合 */
  _streamUnsubs: Array<() => void>;
  /** 当前流式 assistant 消息 ID */
  _streamingMessageId: string | null;

  /* ------------------------------ 动作 ------------------------------ */
  /** 拉取会话列表 */
  fetchSessions: (keyword?: string) => Promise<void>;
  /** 切换当前会话（拉取消息） */
  selectSession: (sessionId: string) => Promise<void>;
  /** 创建会话 */
  createSession: (title?: string, mode?: string) => Promise<DialogSession>;
  /** 删除会话 */
  deleteSession: (sessionId: string) => Promise<void>;
  /** 重命名/置顶/模式 */
  updateSession: (
    sessionId: string,
    body: dialogApi.UpdateSessionBody,
  ) => Promise<void>;
  /** 清空当前会话历史 */
  clearMessages: () => Promise<void>;

  /**
   * 流式发送消息（WebSocket ws://.../dialog/stream/{session_id}）。
   * @param content 用户输入文本
   * @param images  关联图片（多模态）
   * @returns 是否成功发起（功能互斥被阻断时返回 false）
   */
  sendMessage: (content: string, images?: string[]) => Promise<boolean>;
  /** 停止当前会话进行中的流式生成 */
  stopGenerate: () => Promise<void>;
  /** 中断流式连接（前端主动） */
  abortStream: () => void;

  /** 消息评分 */
  rateMessage: (messageId: string, rating: number) => Promise<void>;
  /** 收藏切换 */
  toggleFavorite: (messageId: string) => Promise<void>;

  /* ------------------------------ 生成参数设置（规格 §6.2.1） ------------------------------ */
  /** 设置模型档位（持久化） */
  setModel: (model: DialogModelSize) => void;
  /** 设置采样温度（0~2，持久化） */
  setTemperature: (temperature: number) => void;
  /** 设置上下文长度（持久化） */
  setContextTokens: (contextTokens: number) => void;
}

/** 生成临时消息 ID */
function tempId(): string {
  return `tmp-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

export const useDialogStore = create<DialogState>((set, get) => ({
  sessions: [],
  currentSession: null,
  messages: [],
  generating: false,
  sessionsLoaded: false,

  // 生成参数：从 localStorage 恢复（规格 §6.2.1，默认 4B / 0.7 / 4096）
  ...loadDialogParams(),

  _streamUnsubs: [],
  _streamingMessageId: null,

  fetchSessions: async (keyword) => {
    try {
      const res = await dialogApi.listSessions({ keyword });
      set({ sessions: res.items || [], sessionsLoaded: true });
    } catch {
      set({ sessionsLoaded: true });
    }
  },

  selectSession: async (sessionId) => {
    // 切换前先中断旧流式
    get().abortStream();
    try {
      const detail = await dialogApi.getSession(sessionId);
      set({
        currentSession: {
          id: detail.id,
          title: detail.title,
          pinned: detail.pinned,
          mode: detail.mode,
          last_message: detail.last_message,
          message_count: detail.message_count,
          created_at: detail.created_at,
          updated_at: detail.updated_at,
        },
        messages: detail.messages || [],
      });
    } catch {
      // 失败时尝试只拉消息列表
      try {
        const res = await dialogApi.listMessages(sessionId);
        set({ messages: res.items || [] });
      } catch {
        set({ messages: [] });
      }
    }
  },

  createSession: async (title, mode) => {
    const session = await dialogApi.createSession({ title, mode });
    set((state) => ({
      sessions: [session, ...state.sessions],
      currentSession: session,
      messages: [],
    }));
    return session;
  },

  deleteSession: async (sessionId) => {
    await dialogApi.deleteSession(sessionId);
    set((state) => {
      const sessions = state.sessions.filter((s) => s.id !== sessionId);
      const isCurrent = state.currentSession?.id === sessionId;
      return {
        sessions,
        currentSession: isCurrent ? null : state.currentSession,
        messages: isCurrent ? [] : state.messages,
      };
    });
  },

  updateSession: async (sessionId, body) => {
    const updated = await dialogApi.updateSession(sessionId, body);
    set((state) => ({
      sessions: state.sessions.map((s) => (s.id === sessionId ? updated : s)),
      currentSession:
        state.currentSession?.id === sessionId ? updated : state.currentSession,
    }));
  },

  clearMessages: async () => {
    const { currentSession } = get();
    if (!currentSession) {
      return;
    }
    await dialogApi.clearMessages(currentSession.id);
    set({ messages: [] });
  },

  sendMessage: async (content, images) => {
    const { currentSession } = get();
    if (!currentSession) {
      useAppStore.getState().showToast('请先选择或创建会话', 'warning');
      return false;
    }

    // 功能互斥前置检查（规格 §6.1）
    const appStore = useAppStore.getState();
    if (!appStore.setActiveFeature('dialog')) {
      return false; // 被阻断，已自动 toast
    }

    const sessionId = currentSession.id;

    // 1. 追加用户消息
    const userMsg: DialogMessage = {
      id: tempId(),
      session_id: sessionId,
      role: 'user',
      content,
      created_at: Date.now(),
      images,
    };
    // 2. 占位 assistant 消息（流式逐 token 追加）
    const assistantId = tempId();
    const assistantMsg: DialogMessage = {
      id: assistantId,
      session_id: sessionId,
      role: 'assistant',
      content: '',
      created_at: Date.now(),
    };
    set((state) => ({
      messages: [...state.messages, userMsg, assistantMsg],
      generating: true,
      _streamingMessageId: assistantId,
    }));

    // 3. 建立 WebSocket 流式连接
    const conn = getDialogStream(sessionId);
    conn.connect();

    // 流式 token 追加
    const offToken = conn.on<{ token?: string; content?: string }>(
      'token',
      (data) => {
        const text = data?.token ?? data?.content ?? '';
        if (!text) {
          return;
        }
        set((state) => ({
          messages: state.messages.map((m) =>
            m.id === assistantId
              ? { ...m, content: m.content + text }
              : m,
          ),
        }));
      },
    );

    // meta 事件（引擎来源标注）
    const offMeta = conn.on<{ engine?: string; message_id?: string }>(
      'meta',
      (data) => {
        if (data?.message_id) {
          // 后端下发正式 ID，替换临时 ID
          set((state) => ({
            messages: state.messages.map((m) =>
              m.id === assistantId ? { ...m, id: data.message_id! } : m,
            ),
            _streamingMessageId: data.message_id,
          }));
        }
        if (data?.engine) {
          set((state) => ({
            messages: state.messages.map((m) =>
              m.id === (get()._streamingMessageId ?? assistantId)
                ? { ...m, engine: data.engine }
                : m,
            ),
          }));
        }
      },
    );

    // error 事件（40002 模型未就绪，如实返回）
    const offError = conn.on<{ code?: number; message?: string; detail?: unknown }>(
      'error',
      (data) => {
        const msg = data?.message || '生成失败';
        useAppStore.getState().showToast(msg, 'error');
        set((state) => ({
          messages: state.messages.map((m) =>
            m.id === assistantId
              ? { ...m, content: m.content || `[生成失败] ${msg}` }
              : m,
          ),
          generating: false,
        }));
        cleanup();
        appStore.releaseActiveFeature();
      },
    );

    // done 事件（流式结束）
    const offDone = conn.on('done', () => {
      set({ generating: false });
      cleanup();
      appStore.releaseActiveFeature();
    });

    function cleanup() {
      offToken();
      offMeta();
      offError();
      offDone();
      set({ _streamingMessageId: null, _streamUnsubs: [] });
      // 审计 R3-FE2：流式结束释放连接池条目（destroy 并移出 Map），避免池随会话数线性增长；
      // 同会话再次发送时 getDialogStream 会重建连接（connect 幂等，复用语义不变）
      releaseDialogStream(sessionId);
    }

    set({ _streamUnsubs: [offToken, offMeta, offError, offDone] });

    // 4. 通过 WS 发送用户输入（后端按 session_id 路由流式回复；
    //    携带生成参数 model/temperature/context_tokens，规格 §6.2.1 参数控制真实生效）
    const { model, temperature, contextTokens } = get();
    conn.send('message', {
      content,
      images,
      mode: currentSession.mode,
      model,
      temperature,
      context_tokens: contextTokens,
    });

    return true;
  },

  stopGenerate: async () => {
    const { currentSession } = get();
    if (!currentSession) {
      return;
    }
    try {
      await dialogApi.stopGenerate(currentSession.id);
    } catch {
      /* 忽略 */
    }
    get().abortStream();
  },

  abortStream: () => {
    get()._streamUnsubs.forEach((off) => {
      try {
        off();
      } catch {
        /* 忽略 */
      }
    });
    set({
      _streamUnsubs: [],
      _streamingMessageId: null,
      generating: false,
    });
    // 释放功能锁
    const appStore = useAppStore.getState();
    if (appStore.activeFeature === 'dialog') {
      appStore.releaseActiveFeature();
    }
  },

  rateMessage: async (messageId, rating) => {
    const { currentSession } = get();
    if (!currentSession) {
      return;
    }
    try {
      await dialogApi.rateMessage(currentSession.id, messageId, rating);
      set((state) => ({
        messages: state.messages.map((m) =>
          m.id === messageId ? { ...m, rating } : m,
        ),
      }));
    } catch {
      /* 忽略 */
    }
  },

  toggleFavorite: async (messageId) => {
    const { currentSession } = get();
    if (!currentSession) {
      return;
    }
    try {
      const updated = await dialogApi.toggleFavorite(
        currentSession.id,
        messageId,
      );
      set((state) => ({
        messages: state.messages.map((m) =>
          m.id === messageId ? { ...m, favorite: updated.favorite } : m,
        ),
      }));
    } catch {
      /* 忽略 */
    }
  },

  /* ------------------------------ 生成参数设置（规格 §6.2.1，持久化） ------------------------------ */
  setModel: (model) => {
    set({ model });
    const { temperature, contextTokens } = get();
    persistDialogParams({ model, temperature, contextTokens });
  },

  setTemperature: (temperature) => {
    // 钳制到规格区间 0~2（§6.2.1）
    const clamped = Math.min(TEMPERATURE_MAX, Math.max(TEMPERATURE_MIN, temperature));
    set({ temperature: clamped });
    const { model, contextTokens } = get();
    persistDialogParams({ model, temperature: clamped, contextTokens });
  },

  setContextTokens: (contextTokens) => {
    set({ contextTokens });
    const { model, temperature } = get();
    persistDialogParams({ model, temperature, contextTokens });
  },
}));

export default useDialogStore;
