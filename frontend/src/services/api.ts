/* ==========================================================================
 * OmniSpace AI v2.5.0 —— REST 通信封装（文档B §9.1 / 文档D §4.2.5 统一信封）
 * --------------------------------------------------------------------------
 * 基地址：同源推导 ${location.origin}/api/v1，file:// 回退 http://127.0.0.1:5800/api/v1
 *        （ADR-03：由历史 /v1 迁移；审计 R3-FE5）
 * 统一响应：{ success, data, error, meta }
 *   - success === true：返回 data
 *   - success === false：抛出 ApiError（含语义化 code/message/detail/suggestion/request_id）
 * 错误码：语义化字符串 6 类体系（MODEL_* / KNOWLEDGE_* / BROWSER_* / TRAINING_* /
 *        SYSTEM_* / FRONTEND_*）；前端自造码：FRONTEND_NETWORK_ERROR（网络不可达）/
 *        FRONTEND_PARSE_ERROR（响应解析失败）/ FRONTEND_REQUEST_ABORTED（请求取消）
 * ========================================================================== */

import type { ApiError, ApiResponse } from '@/types';

/**
 * 推导后端 API 基地址（审计 R3-FE5，策略同 services/ws.ts deriveWsRoot）：
 * http(s) 协议下前端由后端同源 serve，location.origin 即后端地址（规格 §14 约束2）；
 * file:// 直开（Tauri/纯文件）或 SSR 等无 http(s) location 场景回退 127.0.0.1:5800。
 */
function deriveApiBase(): string {
  if (
    typeof location === 'undefined' ||
    (location.protocol !== 'http:' && location.protocol !== 'https:')
  ) {
    return 'http://127.0.0.1:5800/api/v1';
  }
  return `${location.origin}/api/v1`;
}

/** 后端 API 基地址（ADR-03：/api/v1 前缀；同源推导，file:// 回退 127.0.0.1:5800） */
export const API_BASE = deriveApiBase();

/**
 * 后端根地址（不带 /api/v1 前缀）。
 * 仅 /health 等极少数根路径端点使用（main.py 将 health 挂在应用根，
 * 不在 API_PREFIX 之下）；常规业务端点一律走 API_BASE。
 */
export const API_ROOT = API_BASE.replace(/\/api\/v1\/?$/, '');

/** 查询参数值类型 */
export type QueryValue = string | number | boolean | null | undefined;

/** 查询参数集合（带索引签名，便于命名接口/对象类型直接传入） */
export type QueryParams = Record<string, QueryValue>;

/** 请求选项 */
export interface RequestOptions {
  /** HTTP 方法，默认 GET */
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE' | 'PATCH';
  /** JSON 请求体（自动序列化并设置 Content-Type） */
  body?: unknown;
  /** URL 查询参数（自动拼接到 path） */
  query?: QueryParams;
  /** 自定义请求头 */
  headers?: Record<string, string>;
  /** AbortSignal，用于取消请求 */
  signal?: AbortSignal;
  /** 请求超时毫秒，默认 0（不超时） */
  timeout?: number;
  /** 是否使用根地址（不带 /api/v1 前缀），仅 /health 等根路径端点使用，默认 false */
  root?: boolean;
}

/** 将查询参数对象序列化为 querystring */
function buildQuery(query?: QueryParams): string {
  if (!query) {
    return '';
  }
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null) {
      continue;
    }
    params.append(key, String(value));
  }
  const qs = params.toString();
  return qs ? `?${qs}` : '';
}

/** 规范化路径：确保以 / 开头，避免与 API_BASE 拼接产生 404 */
function normalizePath(path: string): string {
  if (!path) {
    return '';
  }
  return path.charAt(0) === '/' ? path : `/${path}`;
}

/**
 * 判断值是否为 ApiError 结构。
 * 兼容后端异常处理器返回的统一信封与前端构造的错误对象。
 */
export function isApiError(err: unknown): err is ApiError {
  return (
    typeof err === 'object' &&
    err !== null &&
    typeof (err as ApiError).code === 'string' &&
    typeof (err as ApiError).message === 'string'
  );
}

/**
 * 核心 fetch 封装：统一处理信封、错误与超时。
 * @param path  不带 /api/v1 前缀的路径，如 '/chat/sessions'
 * @param opts  请求选项
 * @returns     成功时返回 ApiResponse.data
 */
export async function request<T = unknown>(
  path: string,
  opts: RequestOptions = {},
): Promise<T> {
  const {
    method = 'GET',
    body,
    query,
    headers = {},
    signal,
    timeout = 0,
    root = false,
  } = opts;

  const url = `${root ? API_ROOT : API_BASE}${normalizePath(path)}${buildQuery(query)}`;

  // 构造请求头与请求体
  const finalHeaders: Record<string, string> = { ...headers };
  let reqBody: BodyInit | undefined;

  if (body instanceof FormData) {
    // multipart 由浏览器自动带 boundary，勿手设 Content-Type
    reqBody = body;
  } else if (body !== undefined && body !== null) {
    finalHeaders['Content-Type'] = 'application/json';
    reqBody = JSON.stringify(body);
  }

  // 超时控制：内部 AbortController 与外部 signal 合并
  const ctrl = new AbortController();
  const onExternalAbort = () => ctrl.abort();
  if (signal) {
    if (signal.aborted) {
      ctrl.abort();
    } else {
      signal.addEventListener('abort', onExternalAbort, { once: true });
    }
  }
  let timer: ReturnType<typeof setTimeout> | undefined;
  if (timeout > 0) {
    timer = setTimeout(() => ctrl.abort(), timeout);
  }

  let res: Response;
  try {
    res = await fetch(url, {
      method,
      headers: finalHeaders,
      body: reqBody,
      signal: ctrl.signal,
    });
  } catch (netErr) {
    // 网络层失败（后端未启动/断网）：统一为 FRONTEND_NETWORK_ERROR，附友好中文提示
    if (timer) clearTimeout(timer);
    const aborted =
      netErr instanceof DOMException && netErr.name === 'AbortError';
    const err: ApiError = aborted
      ? { code: 'FRONTEND_REQUEST_ABORTED', message: '请求已取消', detail: String(netErr) }
      : {
          code: 'FRONTEND_NETWORK_ERROR',
          message: '无法连接后端服务（127.0.0.1:5800），请确认服务已启动',
          detail: String(netErr),
        };
    throw err;
  } finally {
    if (timer) clearTimeout(timer);
    if (signal) signal.removeEventListener('abort', onExternalAbort);
  }

  // B7 步4（2026-09-14）：raw 模式已删——零消费方的死选项（文件下载
  // 类需求将来用独立的 requestRaw(): Promise<Response> 承载，不与信封
  // 泛型混用类型谎言（as unknown as T）。
  // 解析统一信封（文档D：{ success, data, error, meta }）
  let env: ApiResponse<T> | null;
  try {
    env = (await res.json()) as ApiResponse<T>;
  } catch (parseErr) {
    throw {
      code: 'FRONTEND_PARSE_ERROR',
      message: `响应解析失败（HTTP ${res.status}）`,
      detail: String(parseErr),
    } satisfies ApiError;
  }

  if (!env || typeof env.success !== 'boolean') {
    throw {
      code: 'FRONTEND_PARSE_ERROR',
      message: `响应信封格式非法（HTTP ${res.status}）`,
      detail: '缺少 success 字段，疑似非统一信封响应',
    } satisfies ApiError;
  }

  if (!env.success) {
    // 激活门禁（P8 补洞）：任何接口报未激活 → 通知全局遮罩弹出激活窗
    // （激活面板不止在启动页——堵「绕过启动页直接进界面」的路）
    if (env.error?.code === 'LICENSE_REQUIRED') {
      window.dispatchEvent(new CustomEvent('omnispace:license-required'));
    }
    throw {
      code: env.error?.code ?? 'FRONTEND_PARSE_ERROR',
      message: env.error?.message || `请求失败（HTTP ${res.status}）`,
      detail: env.error?.detail,
      suggestion: env.error?.suggestion,
      request_id: env.meta?.request_id,
    } satisfies ApiError;
  }

  return env.data as T;
}

/* ------------------------------ 便捷方法 ------------------------------ */

/** GET 请求 */
export function get<T = unknown>(
  path: string,
  query?: QueryParams,
  opts: Omit<RequestOptions, 'method' | 'query' | 'body'> = {},
): Promise<T> {
  return request<T>(path, { ...opts, method: 'GET', query });
}

/** POST 请求 */
export function post<T = unknown>(
  path: string,
  body?: unknown,
  opts: Omit<RequestOptions, 'method' | 'body'> = {},
): Promise<T> {
  return request<T>(path, { ...opts, method: 'POST', body });
}

/** PUT 请求 */
export function put<T = unknown>(
  path: string,
  body?: unknown,
  opts: Omit<RequestOptions, 'method' | 'body'> = {},
): Promise<T> {
  return request<T>(path, { ...opts, method: 'PUT', body });
}

/** DELETE 请求 */
export function del<T = unknown>(
  path: string,
  opts: Omit<RequestOptions, 'method' | 'body'> = {},
): Promise<T> {
  return request<T>(path, { ...opts, method: 'DELETE' });
}

/** 上传文件（multipart/form-data） */
export function upload<T = unknown>(
  path: string,
  formData: FormData,
  opts: Omit<RequestOptions, 'method' | 'body'> = {},
): Promise<T> {
  return request<T>(path, { ...opts, method: 'POST', body: formData });
}

export default {
  request,
  get,
  post,
  put,
  del,
  upload,
  isApiError,
  API_BASE,
  API_ROOT,
};
// 本项目仅供学习使用，商业授权请+Q 3559331368
