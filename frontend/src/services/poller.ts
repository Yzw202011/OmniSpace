// 本项目仅供学习使用，商业授权请+Q 3559331368
/* 可见性感知轮询工厂（B7 步3，2026-09-14）。
 *
 * 病根（R2 普查）：轮询基建三套互不复用（useStyleStore/useHardwareStore/
 * videoPoller 各自实现「setInterval + visibilitychange 降频重建」）。
 * 本工厂统一该语义：前台常速、页面隐藏降频、恢复可见可选立即补一次。
 *
 * 实现注记（教训）：visibilitychange 监听必须**模块级单例**——若每实例
 * 各自 addEventListener，可见性事件会触发 N 实例×tick 的重复执行
 * （B7 实测：每实例注册致 useMangaStore 假计时器测试 3 挂）。工厂内部
 * 持有运行中实例注册表，单例监听遍历重建+补 tick。
 *
 * 用法：
 *   const p = createVisibilityPoller({ name: 'style', intervalMs: 5000,
 *       hiddenIntervalMs: 10000, tick: () => pollTask(),
 *       tickOnVisible: true });
 *   p.start();  // 幂等
 *   p.stop();
 */

/** 轮询体（异常由调用方自行兜底；工厂不吞错不记日志，保持职责单一） */
export type PollTick = () => void;

export interface VisibilityPoller {
  /** 启动（幂等：已启动则先重建定时器） */
  start: () => void;
  /** 停止并从模块注册表摘除（幂等） */
  stop: () => void;
  /** 是否运行中 */
  readonly running: boolean;
}

export interface VisibilityPollerOptions {
  /** 日志/调试名（预留调试日志标签） */
  name: string;
  /** 前台轮询间隔（毫秒） */
  intervalMs: number;
  /** 页面隐藏时的降频间隔（默认=前台间隔，即不降频） */
  hiddenIntervalMs?: number;
  /** 轮询体 */
  tick: PollTick;
  /** 恢复可见时立即补一次 tick（videoPoller 语义） */
  tickOnVisible?: boolean;
  /** start 时立即执行一次（默认 false；调用方若自带「注册后手动
   *  tick」必须保持 false，避免双重立即 tick） */
  tickOnStart?: boolean;
}

/** 运行中实例注册表（模块级单例监听遍历用） */
const _running = new Set<{
  rebuild: () => void;
  tick: PollTick;
  tickOnVisible: boolean;
}>();

let _visInstalled = false;

function _installVisListener(): void {
  if (_visInstalled || typeof document === 'undefined') {
    return;
  }
  _visInstalled = true;
  document.addEventListener('visibilitychange', () => {
    const hidden = typeof document !== 'undefined' && document.hidden;
    for (const inst of _running) {
      inst.rebuild();
      if (!hidden && inst.tickOnVisible) {
        inst.tick();
      }
    }
  });
}

export function createVisibilityPoller(
    opts: VisibilityPollerOptions): VisibilityPoller {
  const { tick, tickOnVisible = false, tickOnStart = false } = opts;
  void opts.name; // 预留：调试日志标签
  let handle: ReturnType<typeof setInterval> | null = null;
  let running = false;

  const interval = (): number =>
    typeof document !== 'undefined' && document.hidden
      ? (opts.hiddenIntervalMs ?? opts.intervalMs)
      : opts.intervalMs;

  const rebuild = (): void => {
    if (handle !== null) {
      clearInterval(handle);
    }
    handle = setInterval(tick, interval());
  };

  const inst = {
    rebuild,
    tick,
    tickOnVisible,
  };

  return {
    start(): void {
      if (running) {
        rebuild(); // 幂等：重复 start 重置计时（对齐旧 schedulePoll 行为）
        return;
      }
      running = true;
      rebuild();
      _running.add(inst);
      _installVisListener();
      if (tickOnStart) {
        tick();
      }
    },

    stop(): void {
      if (handle !== null) {
        clearInterval(handle);
        handle = null;
      }
      _running.delete(inst);
      running = false;
    },

    get running(): boolean {
      return running;
    },
  };
}
