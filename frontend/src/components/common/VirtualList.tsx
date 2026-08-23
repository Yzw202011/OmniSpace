/* ==========================================================================
 * VirtualList —— 轻量窗口化（P3-⑥ 前端长列表窗口化）
 * --------------------------------------------------------------------------
 * 零依赖可变行高虚拟列表：
 *   - 仅当 items.length > threshold 时激活（短列表走普通流式渲染，
 *     行为与改造前完全一致，不影响常见小会话）；
 *   - 激活后以 position:relative 占满 totalHeight 的容器 + 绝对定位
 *     可视窗口内行（带 overscan 缓冲）。行高经 ResizeObserver 实测并
 *     增量校正偏移数组（O(1) 摊还），滚动只渲染可视片段；
 *   - endRef 锚点置于容器末尾：调用方原 auto-scroll（scrollIntoView）
 *     目标不变，滚动跟随 / 生成完成滚到底语义保持。
 * 使用方负责：
 *   - 在可滚动容器上挂 onScroll={handleScroll} 驱动窗口重算，并传入
 *     scrollRef（用于读 clientHeight 判定视口）；
 *   - 行 key 取自 item.id（可变高度以 id 为键度量）。
 * ========================================================================== */

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
  type RefObject,
} from 'react';

interface VirtualListProps<T extends { id: string }> {
  /** 数据源（仅当超阈值才虚拟化） */
  items: readonly T[];
  scrollRef: RefObject<HTMLElement | null>;
  /** 可视窗口边缘行数缓冲 */
  overscan?: number;
  /** 未测量行高的初始估算（px） */
  estimate?: number;
  /** 相邻行间距（px），需与调用方改造前的列表间距一致 */
  gap?: number;
  /** 激活阈值：低于此数量走普通流式渲染（保持原 DOM 与间距语义） */
  threshold?: number;
  /** 末尾锚点（可选；调用方 auto-scroll 的目标，置于容器末尾） */
  endRef?: RefObject<HTMLDivElement | null>;
  renderItem: (item: T, index: number) => ReactNode;
}

/** 在升序偏移数组内定位首个 >= target 的下标（二分） */
function lowerBound(offsets: number[], target: number): number {
  let lo = 0;
  let hi = offsets.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (offsets[mid] < target) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

export function VirtualList<T extends { id: string }>({
  items,
  scrollRef,
  overscan = 8,
  estimate = 64,
  gap = 20,
  threshold = 60,
  endRef,
  renderItem,
}: VirtualListProps<T>) {
  /** 实测行高（id -> px） */
  const heights = useRef<Map<string, number>>(new Map());
  /** 累积偏移（offsets[i] = 第 i 行顶部相对列表顶部的 y，含前序 gap） */
  const offsets = useRef<number[]>([]);
  /** 每个已渲染行的 ResizeObserver（释放时断开） */
  const observers = useRef<Map<string, ResizeObserver>>(new Map());
  const [, setTick] = useState(0); // 测量/滚动后强制重算
  const [win, setWin] = useState<[number, number]>([0, 0]);

  const total = items.length;
  const activate = total > threshold;
  const idsKey = activate ? items.map((i) => i.id).join('\u0001') : '';
  const prevKey = useRef('');

  const recompute = useCallback(
    (anchor?: number) => {
      const off = offsets.current;
      if (off.length < 2) return;
      const top = anchor ?? scrollRef.current?.scrollTop ?? 0;
      const viewportH = scrollRef.current?.clientHeight ?? 400;
      const start = Math.max(0, lowerBound(off, top) - overscan - 1);
      const end = Math.min(
        total - 1,
        lowerBound(off, top + viewportH + 8) + overscan,
      );
      setWin([start, end]);
    },
    [overscan, scrollRef, total],
  );

  // 滚动驱动窗口：激活时由 scrollRef 滚动触发重算（Internal 监听，
  // 不侵入调用方容器；未激活/无容器时跳过）
  useEffect(() => {
    const c = scrollRef.current;
    if (!c) return;
    const handler = () => recompute();
    c.addEventListener('scroll', handler, { passive: true });
    return () => c.removeEventListener('scroll', handler);
  }, [scrollRef, recompute]);

  // 数据 / 激活态变化：重建偏移缓存并重置窗口到列表尾部
  useEffect(() => {
    if (!activate) {
      prevKey.current = '';
      return;
    }
    if (prevKey.current === idsKey) return;
    prevKey.current = idsKey;
    heights.current = new Map();
    for (const ro of observers.current.values()) ro.disconnect();
    observers.current.clear();
    const prev = offsets.current;
    const off: number[] = [0];
    for (let i = 0; i < items.length; i++) {
      off.push(off[i] + (prev?.[i + 1] ?? estimate) + gap);
    }
    offsets.current = off;
    setWin([Math.max(0, items.length - 20), items.length - 1]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activate, idsKey, items.length]);

  // 卸载清理观察器
  useEffect(() => {
    return () => {
      for (const ro of observers.current.values()) ro.disconnect();
      observers.current.clear();
    };
  }, []);

  if (!activate) {
    // 普通流式渲染：保留原 DOM 与间距语义（gap 由父容器 space-y 控制）
    return (
      <>
        {items.map((item, i) => (
          <div key={item.id}>{renderItem(item, i)}</div>
        ))}
        {endRef ? <div ref={endRef} /> : null}
      </>
    );
  }

  // 激活：总高 = 末批次偏移（含各行高度与 gap）；endRef 置于末行下沿
  const off = offsets.current;
  const totalH = off.length >= 2 ? off[off.length - 1] : 0;
  const [start, end] = win;

  const rows: ReactNode[] = [];
  for (let i = Math.max(0, start); i <= Math.min(total - 1, end); i++) {
    const item = items[i];
    const top = off[i] ?? i * (estimate + gap);
    rows.push(
      <div
        key={`wrap-${item.id}`}
        data-vrow={i}
        style={{ position: 'absolute', top, left: 0, right: 0 }}
        ref={(el) => {
          if (!el) {
            const ro = observers.current.get(item.id);
            ro?.disconnect();
            observers.current.delete(item.id);
            return;
          }
          if (observers.current.has(item.id)) return; // 已观察过本行
          const ro = new ResizeObserver(() => {
            const h = el.offsetHeight;
            const known = heights.current.get(item.id) ?? estimate;
            if (h === known) return;
            heights.current.set(item.id, h);
            const delta = known - h;
            if (delta) {
              const n = offsets.current.length;
              for (let k = i + 1; k < n; k++) {
                offsets.current[k] -= delta;
              }
            }
            setTick((t) => t + 1);
          });
          ro.observe(el);
          observers.current.set(item.id, ro);
        }}
      >
        {renderItem(item, i)}
      </div>,
    );
  }

  return (
    <div style={{ position: 'relative', height: totalH }} data-vlist>
      {rows}
      {endRef ? (
        <div
          ref={endRef}
          style={{ position: 'absolute', top: totalH, left: 0, right: 0 }}
        />
      ) : null}
    </div>
  );
}

export default VirtualList;