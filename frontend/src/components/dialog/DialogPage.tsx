/* ==========================================================================
 * OmniSpace AI v2.1 —— 对话页（容器组件）
 * --------------------------------------------------------------------------
 * 职责：将 useDialogStore 状态映射为 DialogView 所需 props。
 * DialogView 是受控展示组件（props-injected），本组件负责数据桥接与
 * 生命周期管理（挂载时拉取会话列表）。
 *
 * 类型映射：
 *   DialogSession (store) → ChatSession (SessionList)
 *   DialogMessage (store) → ChatMessage (MessageBubble)
 * ========================================================================== */

import { useEffect, useCallback, useRef, useState } from 'react';
import { DialogView } from './DialogView';
import type { Attachment } from './DialogView';
import type { ChatSession } from './SessionList';
import type { ChatMessage } from './MessageBubble';
import { useDialogStore } from '@/stores/useDialogStore';
import { useAppStore } from '@/stores/useAppStore';
import ModuleGuide from '@/components/common/ModuleGuide';
import { getErrorMessage, reportBgError } from '@/utils/errors';
import { prewarmModel, rateMessage } from '@/services/dialogApi';
import type { DialogSession, DialogMessage } from '@/types';

/** 时间戳统一转毫秒 */
function toMs(ts: string | number): number {
  if (typeof ts === 'number') return ts;
  const n = Number(ts);
  return isNaN(n) ? new Date(ts).getTime() : n;
}

/** DialogSession → ChatSession */
function mapSession(s: DialogSession): ChatSession {
  return {
    id: s.id,
    title: s.title,
    preview: s.last_message,
    updatedAt: toMs(s.updated_at),
    pinned: s.pinned,
  };
}

/** DialogMessage → ChatMessage */
function mapMessage(m: DialogMessage, isStreaming: boolean): ChatMessage {
  return {
    id: m.id,
    role: m.role,
    content: m.content,
    timestamp: toMs(m.created_at),
    model: m.engine,
    streaming: isStreaming,
    reasoning: m.reasoning,
    reasoningMs: m.reasoning_ms,
    images: m.images,
    web_refs: m.web_refs,
  };
}

export default function DialogPage() {
  const sessions = useDialogStore((s) => s.sessions);
  const currentSession = useDialogStore((s) => s.currentSession);
  const messages = useDialogStore((s) => s.messages);
  const generating = useDialogStore((s) => s.generating);
  const sessionsLoaded = useDialogStore((s) => s.sessionsLoaded);
  const fetchSessions = useDialogStore((s) => s.fetchSessions);
  const selectSession = useDialogStore((s) => s.selectSession);
  const createSession = useDialogStore((s) => s.createSession);
  const deleteSession = useDialogStore((s) => s.deleteSession);
  const batchDeleteSessions = useDialogStore((s) => s.batchDeleteSessions);
  const updateSession = useDialogStore((s) => s.updateSession);
  const sendMessage = useDialogStore((s) => s.sendMessage);
  const stopGenerate = useDialogStore((s) => s.stopGenerate);
  const showToast = useAppStore((s) => s.showToast);
  // 用户持久化的模型选择（预热目标，与 /models/warmup 口径一致）
  const modelId = useDialogStore((s) => s.modelId);
  // 2026-09-07 按模型能力开放附件：选中模型是否支持图片理解
  // （清单未加载/旧后端无 vision 字段 → 缺省 true 兼容存量行为）
  const modelOptions = useDialogStore((s) => s.modelOptions);
  // UAT 2026-09-10 缺陷④：能力跟随「实际答复模型」——降级场景
  // （9B 纯文本→4B 视觉）此前按所选模型误判为纯文本、误隐藏图片
  // 上传；取最后一条带 engine 标注的答复模型，无标注回退所选模型
  const lastAnswerModel = [...messages]
    .reverse()
    .find((m) => m.engine)?.engine;
  const modelSupportsVision = modelOptions.find(
    (m) => m.model_id === (lastAnswerModel ?? modelId),
  )?.vision ?? true;

  /** 引用请求（seq 递增驱动 DialogView 输入框回填） */
  const [quoteRequest, setQuoteRequest] = useState<{ seq: number; message: ChatMessage } | null>(null);

  // 审计 R3-FE1：经 ref 读取最新 messages/generating，使 onRegenerate 引用稳定，
  // 保证 MessageBubble memo 在流式期间生效（messages 每 token 变化不再击穿回调引用）
  const messagesRef = useRef(messages);
  messagesRef.current = messages;
  const generatingRef = useRef(generating);
  generatingRef.current = generating;

  // UAT 2026-09-10 P1-18 前端兜底：uvicorn h11 下客户端 FIN 对服务端
  // 不可探测（is_disconnected 轮询无效），页面关闭/隐藏时主动置停止
  // 旗标（keepalive fetch 保证卸载期送达），后端 stop_check 即停推理
  useEffect(() => {
    const onHide = () => {
      const st = useDialogStore.getState();
      const sid = st.currentSession?.id;
      if (sid && st.generating) {
        fetch('/api/v1/chat/stop', {
          method: 'POST',
          keepalive: true,
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: sid }),
        }).catch((err: unknown) => reportBgError('dialog.pagehideStop', err));
      }
    };
    window.addEventListener('pagehide', onHide);
    return () => window.removeEventListener('pagehide', onHide);
  }, []);

  // 挂载时拉取会话列表
  useEffect(() => {
    if (!sessionsLoaded) {
      fetchSessions();
    }
    // 性能优化（2026-08-22）：进入对话页即后台预热模型，用户打字期间
    // 完成加载，首条消息不再等冷启动（幂等，后端引擎锁串行化）；
    // modelId 与 /models/warmup 同目标（2026-08-23 幽灵热切换修复）。
    // S6 预热延迟（2026-08-28 V77 事故根修）：与 App.tsx 路由预热
    // 双入口共享后端 inflight 去重、先到者点火——本入口若保持立即
    // 点火会击穿 App 侧 3s 延迟（/chat 默认路由路过点火争抢生成
    // 算力）。同步延迟 3s（离开页面清理）+ 重量级功能持锁跳过。
    let cancelled = false;
    const heavy = useAppStore.getState().activeFeature;
    // 云端模型（cloud:: 前缀）无本地预热概念：跳过点火，避免预热线程
    // 对服务商端点做无谓健康轮询（批1 云端API 2026-09-06）
    const cloudTarget = (modelId || '').startsWith('cloud::');
    const timer =
      heavy === 'paint' || heavy === 'video_gen' || heavy === 'training' || cloudTarget
        ? null
        : window.setTimeout(() => {
            if (!cancelled) prewarmModel(modelId || undefined);
          }, 3000);
    return () => {
      cancelled = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [sessionsLoaded, fetchSessions, modelId]);

  // 映射数据
  const mappedSessions: ChatSession[] = sessions.map(mapSession);
  const mappedMessages: ChatMessage[] = messages.map((m, i) =>
    mapMessage(m, generating && i === messages.length - 1 && m.role === 'assistant'),
  );

  // 回调桥接
  const handleSelectSession = useCallback(
    (id: string) => {
      selectSession(id);
    },
    [selectSession],
  );

  const handleCreateSession = useCallback(() => {
    createSession('新对话');
  }, [createSession]);

  /**
   * 确保存在活跃会话（2026-08-23 输入即自动创建对话）：
   * 无会话时自动创建（遵循系统默认命名"新对话"），返回会话 id。
   * - 防重入：并发触发共享同一个创建 Promise（输入首字符与立刻回车
   *   发送两路触发只建一个会话）
   * - 竞态收敛：若期间用户已手动新建/选中会话，直接返回现有会话
   */
  const ensureSessionRef = useRef<Promise<string | null> | null>(null);
  const handleEnsureSession = useCallback((): Promise<string | null> => {
    const existing = useDialogStore.getState().currentSession;
    if (existing) return Promise.resolve(existing.id);
    if (ensureSessionRef.current) return ensureSessionRef.current;
    const p = (async () => {
      try {
        // 不传 title → 后端默认命名规则（"新对话"）
        const session = await createSession();
        return session.id;
      } catch (err) {
        showToast(getErrorMessage(err, '自动创建对话失败'), 'error');
        return null;
      } finally {
        ensureSessionRef.current = null;
      }
    })();
    ensureSessionRef.current = p;
    return p;
  }, [createSession, showToast]);

  const handleDeleteSession = useCallback(
    (id: string) => {
      deleteSession(id);
    },
    [deleteSession],
  );

  // 批量删除会话（2026-08-20：SessionList 批量管理模式）
  const handleBatchDeleteSessions = useCallback(
    async (ids: string[]) => {
      try {
        const deleted = await batchDeleteSessions(ids);
        showToast(`已删除 ${deleted} 个对话`, 'success');
      } catch (err) {
        showToast(getErrorMessage(err, '批量删除失败'), 'error');
        throw err; // 通知 SessionList 保持批量态（不清空勾选）
      }
    },
    [batchDeleteSessions, showToast],
  );

  const handleSend = useCallback(
    (text: string, attachments?: Attachment[]) => {
      // 2026-09-07 附件分型：仅图片类走 images 多模态通道；
      // 文档类已在 DialogView.send 拼进 text（📎 标记块）。
      // 返回值透传（false=同步拒绝，DialogView 保留输入与附件）
      const images = attachments
        ?.filter((a) => (a.kind ?? 'image') === 'image')
        .map((a) => a.dataUrl)
        .filter((d): d is string => Boolean(d));
      return sendMessage(
        text, images && images.length > 0 ? images : undefined);
    },
    [sendMessage],
  );

  const handleStop = useCallback(() => {
    stopGenerate();
  }, [stopGenerate]);

  // 重命名会话（PUT /chat/sessions/{id}）
  const handleRenameSession = useCallback(
    (id: string, title: string) => {
      updateSession(id, { title })
        .then(() => showToast('会话已重命名', 'success'))
        .catch(() => showToast('重命名失败', 'error'));
    },
    [updateSession, showToast],
  );

  // 置顶 / 取消置顶（PUT /chat/sessions/{id}）
  const handleTogglePinSession = useCallback(
    (id: string, pinned: boolean) => {
      updateSession(id, { pinned })
        .then(() => showToast(pinned ? '会话已置顶' : '已取消置顶', 'success'))
        .catch(() => showToast('操作失败', 'error'));
    },
    [updateSession, showToast],
  );

  // 会话搜索（GET /chat/sessions?keyword=）
  const handleSearchSessions = useCallback(
    (keyword: string) => {
      fetchSessions(keyword || undefined);
    },
    [fetchSessions],
  );

  // 复制消息（剪贴板 + toast 反馈）
  const handleCopyMessage = useCallback(
    (message: ChatMessage) => {
      navigator.clipboard
        .writeText(message.content)
        .then(() => showToast('已复制到剪贴板', 'success'))
        .catch(() => showToast('复制失败（剪贴板不可用）', 'error'));
    },
    [showToast],
  );

  // 消息评分（批5 反馈闭环：踩→本次生成引用的知识自动降权，服务端落账）
  const handleRateMessage = useCallback(
    (message: ChatMessage, rating: number) => {
      if (!currentSession) {
        return;
      }
      rateMessage(currentSession.id, message.id, rating).catch(() =>
        showToast('评分保存失败，请稍后重试', 'error'),
      );
      showToast(
        rating === -1
          ? '已记录反馈：所引用知识的权重将被降低'
          : '已记录反馈：感谢肯定',
        'success',
      );
    },
    [currentSession, showToast],
  );

  // 引用消息：下发引用请求，DialogView 回填输入框
  const handleQuoteMessage = useCallback((message: ChatMessage) => {
    setQuoteRequest((prev) => ({ seq: (prev?.seq ?? 0) + 1, message }));
  }, []);

  // 重新生成：重发该助手消息对应的最近一条用户消息（审计 R3-FE1：经 ref 取值，引用稳定）
  const handleRegenerate = useCallback(
    (message: ChatMessage) => {
      const currentMessages = messagesRef.current;
      if (generatingRef.current) {
        showToast('正在生成中，请先停止', 'warning');
        return;
      }
      const idx = currentMessages.findIndex((m) => m.id === message.id);
      // 向前找最近一条用户消息作为重发内容
      const lastUser = currentMessages
        .slice(0, idx === -1 ? currentMessages.length : idx)
        .reverse()
        .find((m) => m.role === 'user');
      if (!lastUser || !lastUser.content.trim()) {
        showToast('未找到可重发的用户消息', 'warning');
        return;
      }
      sendMessage(lastUser.content, lastUser.images);
    },
    [sendMessage, showToast],
  );

  return (
    <>
      <ModuleGuide moduleKey="chat" />
      <DialogView
        sessions={mappedSessions}
        activeSessionId={currentSession?.id ?? null}
        messages={mappedMessages}
        generating={generating}
        sessionsLoading={!sessionsLoaded}
        onSelectSession={handleSelectSession}
        onCreateSession={handleCreateSession}
        onEnsureSession={handleEnsureSession}
        onDeleteSession={handleDeleteSession}
        onBatchDeleteSessions={handleBatchDeleteSessions}
        onRenameSession={handleRenameSession}
        onTogglePinSession={handleTogglePinSession}
        onSearchSessions={handleSearchSessions}
        quoteRequest={quoteRequest}
        onSend={handleSend}
        onStop={handleStop}
        onCopy={handleCopyMessage}
        onQuote={handleQuoteMessage}
        onRegenerate={handleRegenerate}
        onRate={handleRateMessage}
        modelSupportsVision={modelSupportsVision}
      />
    </>
  );
}
