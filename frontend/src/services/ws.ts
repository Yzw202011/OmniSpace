/* ==========================================================================
 * OmniSpace AI v2.1 —— WebSocket 管理器（规格 §5.3 / §2.2 实时通道）
 * --------------------------------------------------------------------------
 * 端点（根地址由 location 推导，与页面同源；file:// 或 SSR 回退 127.0.0.1:5800）：
 *   - 通用消息中枢：ws://<host>/ws（任务进度/完成/失败广播，规格 §2.2）
 *   - 对话流式：    ws://<host>/api/v1/dialog/stream/{session_id}
 *   - 硬件实时：    ws://<host>/api/v1/hardware/realtime
 * 能力：
 *   - 自动重连（指数退避 1s→2s→4s→…上限 30s，规格 §5.3 重连 <30s）
 *   - 消息订阅/取消订阅（on 返回解订函数）
 *   - 连接状态管理（connecting/open/closed/reconnecting）
 *   - 未连接时消息进入待发送队列，重连后补发（COM-011 休眠恢复友好）
 * ========================================================================== */

import type { WsStatus } from '@/types';
import { WsFrameSchema } from './schema';
import { reportBgError } from '@/utils/errors';

/**
 * 推导 WebSocket 根地址（不含任何路径前缀）。
 * 前端由后端同源 serve，location.host 即后端地址（规格 §14 约束2）；
 * file:// 直开或 SSR 等无 location 场景回退默认 127.0.0.1:5800。
 */
function deriveWsRoot(): string {
  if (typeof location === 'undefined' || location.protocol === 'file:') {
    return 'ws://127.0.0.1:5800';
  }
  const scheme = location.protocol === 'https:' ? 'wss://' : 'ws://';
  return `${scheme}${location.host}`;
}

/** WebSocket 根地址（不含前缀） */
export const WS_ROOT = deriveWsRoot();

/** WebSocket 基地址（/api/v1 前缀，与 API_BASE 同主机，ADR-03） */
export const WS_BASE = `${WS_ROOT}/api/v1`;

/** 通用消息中枢端点（/ws：task_progress/task_complete/system_status 等广播） */
export const WS_HUB_URL = `${WS_ROOT}/ws`;

/** 对话流式 WebSocket 端点 */
export const DIALOG_STREAM_URL = (sessionId: string) =>
  `${WS_BASE}/dialog/stream/${sessionId}`;

/** 硬件实时 WebSocket 端点 */
export const HARDWARE_REALTIME_URL = `${WS_BASE}/hardware/realtime`;

/** 消息回调签名 */
type MessageHandler<T = unknown> = (data: T) => void;

/** 状态变更回调签名 */
type StatusHandler = (status: WsStatus) => void;

/** 重连退避上限（毫秒，规格 §5.3 <30s） */
const MAX_RECONNECT_DELAY = 30000;
/** 待发送队列上限（防爆内存） */
const MAX_PENDING = 50;

/**
 * 单条 WebSocket 连接管理器。
 * 每个端点（对话流/硬件实时）独立实例，互不干扰。
 */
export class WsConnection {
  private socket: WebSocket | null = null;
  private url: string;
  private handlers = new Map<string, Set<MessageHandler>>();
  private statusHandlers = new Set<StatusHandler>();
  private pending: string[] = [];
  private retryCount = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private manualClose = false;
  private _status: WsStatus = 'closed';

  /** 当前连接状态 */
  get status(): WsStatus {
    return this._status;
  }

  /** 是否已连接 */
  get connected(): boolean {
    return this._status === 'open';
  }

  constructor(url: string) {
    this.url = url;
  }

  /** 更新状态并通知订阅者 */
  private setStatus(status: WsStatus): void {
    this._status = status;
    this.statusHandlers.forEach((fn) => {
      try {
        fn(status);
      } catch {
        /* 单个订阅者异常不影响其他 */
      }
    });
  }

  /** 建立连接（幂等：已连接或连接中时直接返回） */
  connect(): void {
    if (
      this.socket &&
      (this.socket.readyState === WebSocket.OPEN ||
        this.socket.readyState === WebSocket.CONNECTING)
    ) {
      return;
    }
    this.manualClose = false;
    this.openSocket();
  }

  /** 内部建立 socket */
  private openSocket(): void {
    this.setStatus(this.retryCount > 0 ? 'reconnecting' : 'connecting');
    let sock: WebSocket;
    try {
      sock = new WebSocket(this.url);
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.socket = sock;

    sock.onopen = () => {
      this.retryCount = 0; // 连接成功重置退避
      this.setStatus('open');
      // 补发待发送队列
      while (this.pending.length > 0 && sock.readyState === WebSocket.OPEN) {
        sock.send(this.pending.shift() as string);
      }
    };

    sock.onmessage = (ev: MessageEvent) => {
      let raw: unknown;
      try {
        raw = JSON.parse(ev.data) as unknown;
      } catch {
        return; // 非 JSON 帧忽略
      }
      // Zod 帧信封校验（批 3-3b：三端点单点把关）。非法帧丢弃并 console
      // 留痕，不断链不抛出——解析失败降级语义 = 跳帧，后续帧照常分发。
      const frame = WsFrameSchema.safeParse(raw);
      if (!frame.success) {
        reportBgError('ws.frame', frame.error.issues[0] ?? new Error('WS 帧信封非法'));
        return;
      }
      this.dispatch(frame.data.type, frame.data.data);
    };

    sock.onclose = () => {
      this.socket = null;
      this.setStatus('closed');
      if (!this.manualClose) {
        this.scheduleReconnect();
      }
    };

    sock.onerror = () => {
      // 交由 onclose 统一触发重连，这里仅保证状态一致
      this.socket = null;
    };
  }

  /** 指数退避重连：1s → 2s → 4s → … → 上限 30s */
  private scheduleReconnect(): void {
    if (this.reconnectTimer || this.manualClose) {
      return;
    }
    this.setStatus('reconnecting');
    const delay = Math.min(
      MAX_RECONNECT_DELAY,
      1000 * Math.pow(2, this.retryCount),
    );
    this.retryCount += 1;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.openSocket();
    }, delay);
  }

  /**
   * 发送消息。未连接时进入待发送队列，重连后补发。
   * @param type 消息类型
   * @param data 消息载荷
   */
  send(type: string, data: unknown = {}): void {
    const payload = JSON.stringify({ type, data });
    if (this.socket && this.socket.readyState === WebSocket.OPEN) {
      this.socket.send(payload);
    } else if (this.pending.length < MAX_PENDING) {
      this.pending.push(payload);
    }
  }

  /**
   * 订阅服务端消息。
   * @returns 解订函数（组件卸载时调用）
   */
  on<T = unknown>(type: string, fn: MessageHandler<T>): () => void {
    let set = this.handlers.get(type);
    if (!set) {
      set = new Set();
      this.handlers.set(type, set);
    }
    set.add(fn as MessageHandler);
    return () => this.off(type, fn);
  }

  /** 取消订阅指定消息回调 */
  off<T = unknown>(type: string, fn: MessageHandler<T>): void {
    const set = this.handlers.get(type);
    if (!set) {
      return;
    }
    set.delete(fn as MessageHandler);
    if (set.size === 0) {
      this.handlers.delete(type);
    }
  }

  /** 订阅连接状态变更 */
  onStatus(fn: StatusHandler): () => void {
    this.statusHandlers.add(fn);
    return () => this.statusHandlers.delete(fn);
  }

  /** 分发消息到订阅者 */
  private dispatch(type: string, data: unknown): void {
    const set = this.handlers.get(type);
    if (!set) {
      return;
    }
    set.forEach((fn) => {
      try {
        fn(data);
      } catch {
        /* 单个订阅者异常不影响其他 */
      }
    });
  }

  /** 主动关闭连接（不再自动重连） */
  close(): void {
    this.manualClose = true;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.socket) {
      try {
        this.socket.close();
      } catch {
        /* 忽略 */
      }
      this.socket = null;
    }
    this.setStatus('closed');
  }

  /** 销毁：关闭连接并清空所有订阅 */
  destroy(): void {
    this.close();
    this.handlers.clear();
    this.statusHandlers.clear();
    this.pending = [];
  }
}

/* ------------------------------ 连接池（按 URL 复用） ------------------------------ */

const connectionPool = new Map<string, WsConnection>();

/**
 * 获取（或创建）指定 URL 的 WebSocket 连接。
 * 同一 URL 全局复用单例，避免重复连接。
 */
export function getWsConnection(url: string): WsConnection {
  let conn = connectionPool.get(url);
  if (!conn) {
    conn = new WsConnection(url);
    connectionPool.set(url, conn);
  }
  return conn;
}

/** 获取对话流式连接 */
export function getDialogStream(sessionId: string): WsConnection {
  return getWsConnection(DIALOG_STREAM_URL(sessionId));
}

/** 获取硬件实时连接（全局单例） */
export function getHardwareRealtime(): WsConnection {
  return getWsConnection(HARDWARE_REALTIME_URL);
}

/**
 * 获取通用消息中枢连接（/ws 全局单例）。
 * 任务进度/预览/完成/失败等广播均经此通道下发（规格 §2.2），
 * 与硬件实时通道 /v1/hardware/realtime 相互独立，不可混用。
 */
export function getWsHub(): WsConnection {
  return getWsConnection(WS_HUB_URL);
}

/**
 * 释放指定 URL 的连接（审计 R3-FE2）。
 * 从连接池取出并 destroy（连接中状态亦可安全关闭：manualClose 阻断重连），
 * 随后从 Map 删除；同 URL 再次 getWsConnection 将创建新连接
 * （WsConnection.connect 幂等语义不受影响）。
 */
export function releaseWsConnection(url: string): void {
  const conn = connectionPool.get(url);
  if (!conn) {
    return;
  }
  connectionPool.delete(url);
  try {
    conn.destroy();
  } catch {
    /* destroy 内部已兜底，防御性忽略 */
  }
}

/** 释放指定会话的对话流式连接（审计 R3-FE2） */
export function releaseDialogStream(sessionId: string): void {
  releaseWsConnection(DIALOG_STREAM_URL(sessionId));
}

export default {
  WsConnection,
  getWsConnection,
  getDialogStream,
  getHardwareRealtime,
  getWsHub,
  releaseWsConnection,
  releaseDialogStream,
  DIALOG_STREAM_URL,
  HARDWARE_REALTIME_URL,
  WS_HUB_URL,
  WS_ROOT,
  WS_BASE,
};
// 本项目仅供学习使用，商业授权请+Q 3559331368
