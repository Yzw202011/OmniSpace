/* ==========================================================================
 * ws.test.ts —— WebSocket 管理器自愈行为锁定（自愈重建 R4，2026-10-21）
 * --------------------------------------------------------------------------
 * 用假时钟 + 可控 WebSocket 桩，把断线重连族的五条判据钉死：
 *   I-01 指数退避 1s→2s→4s→8s→16s→30s 封顶（32s/64s 不出现）
 *   I-02 重连成功退避计数归零，下次断线从 1s 重新开始
 *   I-03 断线消息入补发队列，重连后按序补发
 *   I-04 补发队列满 50 条丢弃新消息且必须留痕告警（SH-GAP-01 回归）
 *   I-05 非法帧 Zod 跳过不断链，合法帧照常分发
 * ========================================================================== */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { WsConnection } from './ws';

/** 可受控的 WebSocket 桩（ws.ts 只依赖构造器/静态常量/readyState/send/close） */
class MockWebSocket {
  static OPEN = 1;
  static CONNECTING = 0;
  static instances: MockWebSocket[] = [];
  readyState = 0;
  sent: string[] = [];
  closed = false;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(public url: string) {
    MockWebSocket.instances.push(this);
  }

  send(data: string): void {
    this.sent.push(data);
  }

  close(): void {
    this.closed = true;
  }

  /** 测试辅助：模拟服务端握手完成 */
  serverOpen(): void {
    this.readyState = MockWebSocket.OPEN;
    this.onopen?.();
  }

  /** 测试辅助：模拟连接断开（触发指数退避重连） */
  serverClose(): void {
    this.readyState = 3;
    this.onclose?.();
  }

  /** 测试辅助：模拟服务端帧到达 */
  serverMessage(raw: string): void {
    this.onmessage?.({ data: raw });
  }
}

/** 断开当前最新连接，并断言恰好 delayMs 后才重连出下一个 socket */
function closeAndAwaitReconnect(delayMs: number): void {
  MockWebSocket.instances.at(-1)!.serverClose();
  vi.advanceTimersByTime(delayMs - 1);
  const before = MockWebSocket.instances.length;
  vi.advanceTimersByTime(1);
  expect(MockWebSocket.instances.length).toBe(before + 1);
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal('WebSocket', MockWebSocket);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('WsConnection 断线自愈族（I-01~I-05）', () => {
  it('I-01 指数退避 1s→2s→4s→8s→16s→30s 封顶', () => {
    const conn = new WsConnection('ws://test/ws');
    conn.connect();
    expect(MockWebSocket.instances.length).toBe(1);
    expect(conn.status).toBe('connecting');

    closeAndAwaitReconnect(1000);
    closeAndAwaitReconnect(2000);
    closeAndAwaitReconnect(4000);
    closeAndAwaitReconnect(8000);
    closeAndAwaitReconnect(16000);
    // 第 6 次断线：2^5=32s 被封顶为 30s
    closeAndAwaitReconnect(30000);
    // 第 7 次：仍是 30s（64s 不出现=封顶生效）
    closeAndAwaitReconnect(30000);
    expect(conn.status).toBe('reconnecting');
  });

  it('I-02 重连成功后退避计数归零，下次断线从 1s 重新开始', () => {
    const conn = new WsConnection('ws://test/ws');
    conn.connect();
    // 先制造两级退避
    closeAndAwaitReconnect(1000);
    closeAndAwaitReconnect(2000);
    // 连上 → 计数归零
    MockWebSocket.instances.at(-1)!.serverOpen();
    expect(conn.status).toBe('open');
    // 再断：必须从 1s 重新开始（若未归零这里会是 4s）
    MockWebSocket.instances.at(-1)!.serverClose();
    vi.advanceTimersByTime(999);
    const before = MockWebSocket.instances.length;
    vi.advanceTimersByTime(1);
    expect(MockWebSocket.instances.length).toBe(before + 1);
  });

  it('I-03 断线期间消息入补发队列，重连成功后按序补发', () => {
    const conn = new WsConnection('ws://test/ws');
    conn.connect();
    MockWebSocket.instances.at(-1)!.serverOpen();
    MockWebSocket.instances.at(-1)!.serverClose();

    conn.send('task_progress', { id: 1 });
    conn.send('task_complete', { id: 2 });

    vi.advanceTimersByTime(1000);
    const reconnected = MockWebSocket.instances.at(-1)!;
    reconnected.serverOpen();
    expect(reconnected.sent).toEqual([
      JSON.stringify({ type: 'task_progress', data: { id: 1 } }),
      JSON.stringify({ type: 'task_complete', data: { id: 2 } }),
    ]);
  });

  it('I-04 补发队列满 50 条丢弃新消息且必须留痕告警（SH-GAP-01 回归）', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const conn = new WsConnection('ws://test/ws');
    conn.connect();
    MockWebSocket.instances.at(-1)!.serverClose();

    for (let i = 0; i < 50; i++) {
      conn.send('m', { i });
    }
    conn.send('overflow', { dropped: true }); // 第 51 条：入不了队

    vi.advanceTimersByTime(1000);
    const reconnected = MockWebSocket.instances.at(-1)!;
    reconnected.serverOpen();
    expect(reconnected.sent.length).toBe(50);
    expect(reconnected.sent.some((s) => s.includes('overflow'))).toBe(false);
    expect(warnSpy).toHaveBeenCalledWith(
      '[ws.pending.overflow]',
      expect.any(Error),
    );
    warnSpy.mockRestore();
  });

  it('I-05 非法帧 Zod 跳过不断链，合法帧照常分发', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const conn = new WsConnection('ws://test/ws');
    conn.connect();
    MockWebSocket.instances.at(-1)!.serverOpen();
    const seen: string[] = [];
    conn.on('task_progress', () => seen.push('hit'));

    const sockMessage = (raw: string): void => {
      MockWebSocket.instances.at(-1)!.serverMessage(raw);
    };
    expect(() => sockMessage('not-json')).not.toThrow();
    expect(() => sockMessage('{"type":123,"data":{}}')).not.toThrow();
    expect(seen).toEqual([]);
    expect(conn.status).toBe('open'); // 不断链

    sockMessage(JSON.stringify({ type: 'task_progress', data: {} }));
    expect(seen).toEqual(['hit']);
    warnSpy.mockRestore();
  });
});
