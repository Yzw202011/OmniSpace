/**
 * 界面偏好镜像（2026-09-12）—— 修复「选型重启回退」类 bug
 * --------------------------------------------------------------------------
 * 背景：对话模型选择/漫剧模型配置/提示词配置/布局偏好等只存 localStorage，
 * localStorage 因换端口启动（5800 被占顺延 5801+）、桌面壳 WebView 配置、
 * 清缓存而丢失时，选型打回默认（用户实测：洱海月主题、Qwen3-VL-8B 均中招）。
 * 机制：镜像到后端 KV system_settings 表（key='ui.prefs'，值为
 * {原localStorage键: 值}）——
 *   - replayUiPrefs()：渲染前回放（main.tsx bootstrap 阻塞 ≤2s，超时按本地值）
 *   - mirrorPref(key, value)：各写入点旁路镜像（400ms 防抖 PUT 整包）
 * localStorage 仍是运行时读取层（各 store 零改动）；后端是不可丢的真源。
 * 主题不走此通道（system.settings.theme 独立回放，见 useAppStore.loadSettings）。
 */
import { getUiPrefs, updateUiPrefs } from './systemApi';

/** 偏好缓存（最近一次已知的服务端整包；null=尚未回放） */
let cache: Record<string, unknown> | null = null;

/** 防抖定时器 */
let putTimer: number | null = null;

/** 启动渲染前回放：把服务端镜像写回 localStorage（≤2s 超时，失败按本地值） */
export async function replayUiPrefs(): Promise<void> {
  try {
    // 超时经 api.ts 的 timeout 通道下发（2026-09-16 修复：此前
    // AbortController 建了但 signal 从未接线，「≤2s」契约是死代码，
    // 后端冷启动慢时 main.tsx 的阻塞式 await 会无限拖住首屏）
    const prefs = await getUiPrefs(2000);
    cache = prefs && typeof prefs === 'object' ? prefs : {};
    for (const [key, value] of Object.entries(cache)) {
      try {
        localStorage.setItem(key, JSON.stringify(value));
      } catch {
        /* 隐私模式写入失败：本地值兜底 */
      }
    }
  } catch {
    /* 后端未就绪/超时：按本地 localStorage 值启动（原行为） */
  }
}

/**
 * 镜像一条偏好到后端（在各 localStorage.setItem 写入点旁路调用）。
 * 400ms 防抖合并整包 PUT；失败静默（localStorage 仍是运行时真相，
 * 后端镜像只服务下次启动回放）。
 */
export function mirrorPref(key: string, value: unknown): void {
  if (!cache) {
    // 回放前就有写入（罕见）：以本地现值为初始缓存，避免丢其他键
    cache = {};
  }
  cache[key] = value;
  if (putTimer !== null) {
    window.clearTimeout(putTimer);
  }
  putTimer = window.setTimeout(() => {
    putTimer = null;
    void updateUiPrefs(cache ?? {}).catch(() => {
      /* 镜像失败静默：下次写入重试整包 */
    });
  }, 400);
}
