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
import { trackBehavior } from '@/services/learningApi';
import { useAppStore } from './useAppStore';
import { mirrorPref } from '@/services/uiPrefs';
import { reportBgError } from '@/utils/errors';

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

/** 最近一次已提醒的降级组合（from>engine）——同一降级只 toast 一次 */
let _lastDegradedKey = '';

/** 需持久化的对话参数 */
interface DialogParamsSnapshot {
  model: DialogModelSize;
  modelId: string;
  temperature: number;
  contextTokens: number;
  /** 深度思考开关（system prompt 四步框架引导 + reasoning 双通道） */
  thinking: boolean;
}

/** 旧档位标签 → 缺省完整 model_id（localStorage 兼容迁移） */
const _LEGACY_SIZE_TO_MODEL: Record<DialogModelSize, string> = {
  '8B': 'qwen3-vl-8b',
  '4B': 'qwen3-vl-4b',
  '2B': 'qwen2-vl-2b',
};

/** 由完整 model_id 派生档位标签（qwen3-vl-4b → 4B） */
export function sizeFromModelId(modelId: string): DialogModelSize {
  const m = /(\d+(?:\.\d+)?)b/i.exec(modelId);
  if (!m) return '4B';
  const label = `${m[1]}B` as DialogModelSize;
  return DIALOG_MODEL_OPTIONS.includes(label) ? label : '4B';
}

/** 从 localStorage 恢复对话参数（非法值回退默认，绝不抛错阻断启动） */
function loadDialogParams(): DialogParamsSnapshot {
  const fallback: DialogParamsSnapshot = {
    model: '4B', modelId: 'qwen3-vl-4b', temperature: 0.7, contextTokens: 4096,
    thinking: false,
  };
  try {
    const raw = localStorage.getItem(DIALOG_PARAMS_STORAGE_KEY);
    if (!raw) {
      return fallback;
    }
    const parsed = JSON.parse(raw) as Partial<DialogParamsSnapshot>;
    const model = DIALOG_MODEL_OPTIONS.includes(parsed.model as DialogModelSize)
      ? (parsed.model as DialogModelSize)
      : fallback.model;
    // modelId：新字段优先；旧快照只有档位标签 → 映射补全（迁移）
    const modelId = typeof parsed.modelId === 'string' && parsed.modelId
      ? parsed.modelId
      : _LEGACY_SIZE_TO_MODEL[model] || fallback.modelId;
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
    // thinking：旧快照无此字段 → 回退默认关闭（宽松布尔，仅 true 才开）
    const thinking = parsed.thinking === true;
    return { model: sizeFromModelId(modelId), modelId, temperature, contextTokens, thinking };
  } catch {
    return fallback;
  }
}

/** 持久化对话参数到 localStorage（失败静默，不影响交互） */
function persistDialogParams(snapshot: DialogParamsSnapshot): void {
  try {
    localStorage.setItem(DIALOG_PARAMS_STORAGE_KEY, JSON.stringify(snapshot));
    mirrorPref(DIALOG_PARAMS_STORAGE_KEY, snapshot); // 界面偏好镜像（2026-09-12 修复重启回退）
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
  /** 模型档位（8B/4B/2B，默认 4B 均衡档；派生自 modelId，仅作展示与兼容） */
  model: DialogModelSize;
  /** 选定对话模型完整 id（如 qwen3-vl-4b；随 WS 发送真实生效，2026-08-20 接线） */
  modelId: string;
  /** 可选模型清单（GET /dialog/models；空 = 拉取前/失败回退档位） */
  modelOptions: dialogApi.DialogModelInfo[];
  /** 采样温度（0~2，默认 0.7） */
  temperature: number;
  /** 上下文长度（tokens） */
  contextTokens: number;
  /** 深度思考开关（开启后随 WS 下发 thinking=true，回复经 reasoning 双通道展示） */
  thinking: boolean;

  /* ------------------------------ 内部 ------------------------------ */
  /** 当前流式连接的解订函数集合 */
  _streamUnsubs: Array<() => void>;
  /** 当前流式 assistant 消息 ID */
  _streamingMessageId: string | null;
  /** 当前流式所属会话 ID（abortStream 释放连接用，2026-09-15） */
  _streamSessionId: string | null;

  /* ------------------------------ 动作 ------------------------------ */
  /** 拉取会话列表 */
  fetchSessions: (keyword?: string) => Promise<void>;
  /** 切换当前会话（拉取消息） */
  selectSession: (sessionId: string) => Promise<void>;
  /** 创建会话 */
  createSession: (title?: string, mode?: string) => Promise<DialogSession>;
  /** 删除会话 */
  deleteSession: (sessionId: string) => Promise<void>;
  /** 批量删除会话（超 100 自动分批；返回实际删除数） */
  batchDeleteSessions: (sessionIds: string[]) => Promise<number>;
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
  /** 按完整 model_id 选定对话模型（真实生效，随 WS 发送） */
  setModelId: (modelId: string) => void;
  /** 拉取可选模型清单并校正当前选择（不可承载自动回退） */
  loadDialogModels: () => Promise<void>;
  /** 设置采样温度（0~2，持久化） */
  setTemperature: (temperature: number) => void;
  /** 设置上下文长度（持久化） */
  setContextTokens: (contextTokens: number) => void;
  /** 设置深度思考开关（持久化） */
  setThinking: (thinking: boolean) => void;
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
  modelOptions: [],

  _streamUnsubs: [],
  _streamingMessageId: null,
  _streamSessionId: null,

  fetchSessions: async (keyword) => {
    try {
      const res = await dialogApi.listSessions({ keyword });
      set({ sessions: res.items || [], sessionsLoaded: true });
    } catch (err: unknown) {
      // 2026-09-15 审计修复：加载失败静默显示空列表——上报留痕（三分法 CONSOLE 级）
      reportBgError('会话列表加载失败', err);
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
    } catch (err: unknown) {
      // 2026-09-15 审计修复：会话详情拉取失败静默清空——上报留痕再兜底
      reportBgError('会话详情加载失败（回退消息列表）', err);
      try {
        const res = await dialogApi.listMessages(sessionId);
        set({ messages: res.items || [] });
      } catch (err2: unknown) {
        reportBgError('消息列表加载失败', err2);
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

  batchDeleteSessions: async (sessionIds) => {
    if (sessionIds.length === 0) return 0;
    // 后端单批上限 100：超量顺序分批提交，聚合结果
    const done = new Set<string>();
    for (let i = 0; i < sessionIds.length; i += 100) {
      const res = await dialogApi.batchDeleteSessions(sessionIds.slice(i, i + 100));
      res.deleted_ids.forEach((id) => done.add(id));
    }
    set((state) => {
      const currentDeleted = state.currentSession
        ? done.has(state.currentSession.id) : false;
      return {
        sessions: state.sessions.filter((s) => !done.has(s.id)),
        currentSession: currentDeleted ? null : state.currentSession,
        messages: currentDeleted ? [] : state.messages,
      };
    });
    return done.size;
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

    // 功能互斥前置检查（规格 §6.1）＋ 排队放行（2026-09-08 对话排队）：
    // 本页发起的绘画/视频/训练进行中时不再前端秒拒——放行走 WS，
    // 由后端功能锁排队（锁释放后自动继续，气泡显示「排队中」）。
    // 对话以外的互斥（如 training 之外的本地态）维持原拒绝语义。
    const appStore = useAppStore.getState();
    if (!appStore.setActiveFeature('dialog')) {
      const active = useAppStore.getState().activeFeature;
      if (active !== 'paint' && active !== 'video_gen'
          && active !== 'training') {
        return false; // 被阻断，已自动 toast
      }
      // 排队放行：不抢 activeFeature（真实持锁方保持），消息交后端排队
    }
    // 排队放行的对称收尾：仅在 activeFeature 仍是 dialog（本功能真实
    // 持有）时释放——排队场景（paint 等持锁）绝不误清真实持锁方
    const releaseDialogFeature = (): void => {
      if (useAppStore.getState().activeFeature === 'dialog') {
        appStore.releaseActiveFeature();
      }
    };

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
    const startedAt = Date.now();
    const assistantMsg: DialogMessage = {
      id: assistantId,
      session_id: sessionId,
      role: 'assistant',
      content: '',
      created_at: startedAt,
    };
    set((state) => ({
      messages: [...state.messages, userMsg, assistantMsg],
      generating: true,
      _streamingMessageId: assistantId,
      _streamSessionId: sessionId,
    }));

    // 行为学习埋点（fire-and-forget，失败静默）
    trackBehavior('dialog_send', {
      content: content.slice(0, 200),
      context: images?.length ? `images:${images.length}` : 'text',
      feature: 'dialog',
    });

    // 3. 建立 WebSocket 流式连接
    const conn = getDialogStream(sessionId);
    conn.connect();

    // 流式 token 追加（首个 token = 思考结束/正文开始，定格 reasoning_ms）
    let firstTokenAt = 0;
    /** 清除加载占位（2026-09-02 修复占位串进正文）：必须在追加 token
     * 之前执行——旧实现放在独立 offFirstToken 监听器里，而 offToken
     * 先注册先分发，追加后 content 变成「占位+正文」，精确匹配恒失败，
     * 占位永远留在气泡里。改为 append 前先剥占位（同处理器内顺序保证）。 */
    const consumeLoadingHint = (): boolean => {
      if (!loadingHint) return false;
      const hint = loadingHint;
      loadingHint = '';
      set((state) => ({
        messages: state.messages.map((m) =>
          m.id === assistantId && m.content === hint
            ? { ...m, content: '' }
            : m,
        ),
      }));
      return true;
    };
    const offToken = conn.on<{ token?: string; content?: string }>(
      'token',
      (data) => {
        const text = data?.token ?? data?.content ?? '';
        if (!text) {
          return;
        }
        consumeLoadingHint();
        if (!firstTokenAt) {
          firstTokenAt = Date.now();
        }
        set((state) => ({
          messages: state.messages.map((m) => {
            if (m.id !== assistantId) return m;
            const next: DialogMessage = { ...m, content: m.content + text };
            if (m.reasoning && m.reasoning_ms === undefined) {
              next.reasoning_ms = Math.max(0, firstTokenAt - startedAt);
            }
            return next;
          }),
        }));
      },
    );

    // reasoning 事件（深度思考双通道：思考过程逐段追加，与 content 分离）
    const offReasoning = conn.on<{ text?: string }>('reasoning', (data) => {
      const text = data?.text ?? '';
      if (!text) {
        return;
      }
      set((state) => ({
        messages: state.messages.map((m) =>
          m.id === assistantId
            ? { ...m, reasoning: (m.reasoning ?? '') + text }
            : m,
        ),
      }));
    });

    // web_refs 事件（联网搜索 v1：来源列表，供消息下方来源卡片渲染；
    // 事件在 token 流开始前一次性下发）
    const offWebRefs = conn.on<{ refs?: Array<import('../types').WebRef> }>(
      'web_refs',
      (data) => {
        const refs = data?.refs;
        if (!refs || refs.length === 0) {
          return;
        }
        set((state) => ({
          messages: state.messages.map((m) =>
            m.id === assistantId ? { ...m, web_refs: refs } : m,
          ),
        }));
      },
    );

    // meta 事件（引擎来源标注）
    const offMeta = conn.on<{
      engine?: string;
      message_id?: string;
      degraded_from?: string;
    }>(
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
          // 2026-09-08 用户报「对话的 AI 跟模型选择的不符」却无任何
          // 提示：降级只写了事件日志（前端零感知）。实际引擎与所选
          // 不一致（或后端 meta 明示 degraded_from，需后端重启后才有）
          // 时当场 toast 说明+出路；同一降级只提醒一次防刷屏
          const sel = get().modelId;
          const from = data.degraded_from
            || (sel && data.engine !== sel ? sel : '');
          if (from && from !== data.engine) {
            const key = `${from}>${data.engine}`;
            if (_lastDegradedKey !== key) {
              _lastDegradedKey = key;
              const friendly = (id: string) =>
                get().modelOptions.find((o) => o.model_id === id)?.name ?? id;
              useAppStore.getState().showToast(
                `您选的「${friendly(from)}」在当前显存余量下装不下，`
                + `本条已自动改用「${friendly(data.engine)}」回答`
                + '（消息上方有标注）。关闭占用显存的应用后'
                + '重新选择即可换回', 'warning');
            }
          }
        }
      },
    );

    // error 事件（40002 模型未就绪，如实返回）
    const offError = conn.on<{ code?: number; message?: string; detail?: unknown }>(
      'error',
      (data) => {
        const msg = data?.message || '生成失败';
        useAppStore.getState().showToast(msg, 'error');
        // P1 修复（2026-09-02 冷启动实测）：content 为加载占位符时必须
        // 先清掉再写错误文案——`m.content ||` 短路会把错误吞进占位符，
        // 用户只看到永远「冷启动中」的气泡、无任何失败提示
        const hint = loadingHint;
        loadingHint = '';
        set((state) => ({
          messages: state.messages.map((m) => {
            if (m.id !== assistantId) return m;
            const base = !m.content || m.content === hint
              ? '' : m.content;
            return { ...m, content: base || `[生成失败] ${msg}` };
          }),
          generating: false,
        }));
        cleanup();
        releaseDialogFeature();
      },
    );

    // done 事件（流式结束）
    const offDone = conn.on('done', () => {
      set({ generating: false });
      cleanup();
      releaseDialogFeature();
    });

    // status 事件（模型冷启动等阶段提示，2026-08-22 思考过长事故）：
    // 写入占位消息——首个 token 到达前用户能看到加载阶段而非空白"思考中"。
    // queued（2026-09-08 对话排队）持续推送每 2s 刷新等待秒数——精确
    // 匹配旧 hint 覆盖（content 非空也允许更新，仅限自家占位文案）
    let loadingHint = '';
    const offStatus = conn.on<{ phase?: string; message?: string }>(
      'status',
      (data) => {
        if (!data?.message) return;
        const prevHint = loadingHint;
        loadingHint = `[${data.message}]`;
        set((state) => ({
          messages: state.messages.map((m) =>
            m.id === assistantId && (!m.content || m.content === prevHint)
              ? { ...m, content: loadingHint }
              : m,
          ),
        }));
      },
    );

    // 深度思考模式下首个产出是 reasoning 帧而非 token 帧 → reasoning
    // 通道同样清除占位（reasoning 不改 content，精确匹配安全）
    const offFirstReasoning = conn.on<{ text?: string }>('reasoning', () => {
      consumeLoadingHint();
      offFirstReasoning();
    });

    // 断流兜底（2026-09-15 审计 P0 修复）：后端重启/断网时连接断开，
    // 本次生成不会再有 done/error 帧——旧实现 generating 永久 true，
    // 输入框与重生成被卡死到用户手点停止。策略：连接级状态离开 open
    // 即起 8s 宽限计时（初始 connecting 不算——流尚未建立）；期间恢复
    // open 则撤销，超时则终止流并在消息位落中断提示（后端按 socket
    // 路由流式回复，重连后旧流不续，诚实终止优于假等）。
    let connGraceTimer: number | null = null;
    let streamWasOpen = false;
    const clearStreamGrace = () => {
      if (connGraceTimer !== null) {
        clearTimeout(connGraceTimer);
        connGraceTimer = null;
      }
    };
    const offConnStatus = conn.onStatus((st) => {
      if (st === 'open') {
        streamWasOpen = true;
        clearStreamGrace();
        return;
      }
      if (!streamWasOpen
          || get()._streamingMessageId !== assistantId) {
        return;
      }
      clearStreamGrace();
      connGraceTimer = window.setTimeout(() => {
        if (get()._streamingMessageId !== assistantId) return;
        set((state) => ({
          messages: state.messages.map((m) =>
            m.id === assistantId && !m.content
              ? { ...m, content: '[连接中断，生成已终止——请重发]' }
              : m,
          ),
        }));
        get().abortStream();
      }, 8000);
    });

    function cleanup() {
      offToken();
      offReasoning();
      offWebRefs();
      offMeta();
      offError();
      offDone();
      offStatus();
      offFirstReasoning();
      offConnStatus();
      clearStreamGrace();
      set({ _streamingMessageId: null, _streamUnsubs: [] });
      // 审计 R3-FE2：流式结束释放连接池条目（destroy 并移出 Map），避免池随会话数线性增长；
      // 同会话再次发送时 getDialogStream 会重建连接（connect 幂等，复用语义不变）
      releaseDialogStream(sessionId);
    }

    set({
      _streamUnsubs: [
        offToken, offReasoning, offWebRefs, offMeta, offError, offDone,
        offStatus, offFirstReasoning, offConnStatus,
      ],
    });

    // 4. 通过 WS 发送用户输入（后端按 session_id 路由流式回复；
    //    携带生成参数 model(完整id)/temperature/context_tokens —— 2026-08-20
    //    模型选择接线：后端按 model 热切换引擎，规格 §6.2.1 参数控制真实生效）
    const { modelId, temperature, contextTokens, thinking } = get();
    conn.send('message', {
      content,
      images,
      mode: currentSession.mode,
      model: modelId,
      temperature,
      context_tokens: contextTokens,
      thinking,
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
    } catch (err: unknown) {
      // 2026-09-15 审计修复：用户主动动作失败不再吞——上报留痕（本地中断仍继续）
      reportBgError('停止生成请求失败（本地已中断）', err);
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
    const streamSession = get()._streamSessionId;
    set({
      _streamUnsubs: [],
      _streamingMessageId: null,
      _streamSessionId: null,
      generating: false,
    });
    // 2026-09-15 审计修复：abort 路径同样释放连接池条目——此前只有
    // done/error 驱动的 cleanup() 会调 releaseDialogStream，用户停止/
    // 断流兜底走 abortStream 时连接留在池里持续自动重连（僵尸累积）
    if (streamSession) {
      try {
        releaseDialogStream(streamSession);
      } catch {
        /* 忽略 */
      }
    }
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
    } catch (err: unknown) {
      reportBgError('消息评分失败', err);  // 2026-09-15：吞错改上报
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
    } catch (err: unknown) {
      reportBgError('收藏切换失败', err);  // 2026-09-15：吞错改上报
    }
  },

  /* ------------------------------ 生成参数设置（规格 §6.2.1，持久化） ------------------------------ */
  setModel: (model) => {
    // 兼容入口：按档位标签映射回完整 model_id（首选该档位的本地模型）
    const modelId = _LEGACY_SIZE_TO_MODEL[model] || get().modelId;
    set({ model, modelId });
    const { temperature, contextTokens, thinking } = get();
    persistDialogParams({ model, modelId, temperature, contextTokens, thinking });
  },

  /** 按完整 model_id 选定模型（2026-08-20 模型选择接线） */
  setModelId: (modelId) => {
    const model = sizeFromModelId(modelId);
    set({ model, modelId });
    const { temperature, contextTokens, thinking } = get();
    persistDialogParams({ model, modelId, temperature, contextTokens, thinking });
  },

  /** 拉取可选模型清单；首次拉取时校正当前选择（不可承载/不存在 → 回退可承载项） */
  loadDialogModels: async () => {
    try {
      const res = await dialogApi.getDialogModels();
      const models = res.models || [];
      set({ modelOptions: models });
      const { modelId } = get();
      const cur = models.find((m) => m.model_id === modelId);
      if (cur && !cur.fits_local) {
        // 当前选择本机装不下 → 回退第一个可承载项（无则保持原值由引擎路由）
        const alt = models.find((m) => m.fits_local);
        if (alt) {
          get().setModelId(alt.model_id);
        }
      } else if (!cur && models.length > 0) {
        // 持久化的 id 已不在清单（模型被删）→ 回退可承载首项
        const alt = models.find((m) => m.fits_local) || models[0];
        get().setModelId(alt.model_id);
      }
    } catch {
      /* 清单拉取失败静默：保留本地缺省，发送仍带 modelId 由引擎校验 */
    }
  },

  setTemperature: (temperature) => {
    // 钳制到规格区间 0~2（§6.2.1）
    const clamped = Math.min(TEMPERATURE_MAX, Math.max(TEMPERATURE_MIN, temperature));
    set({ temperature: clamped });
    const { model, modelId, contextTokens, thinking } = get();
    persistDialogParams({ model, modelId, temperature: clamped, contextTokens, thinking });
  },

  setContextTokens: (contextTokens) => {
    set({ contextTokens });
    const { model, modelId, temperature, thinking } = get();
    persistDialogParams({ model, modelId, temperature, contextTokens, thinking });
  },

  setThinking: (thinking) => {
    set({ thinking });
    const { model, modelId, temperature, contextTokens } = get();
    persistDialogParams({ model, modelId, temperature, contextTokens, thinking });
  },
}));

export default useDialogStore;
// 本项目仅供学习使用，商业授权请+Q 3559331368
