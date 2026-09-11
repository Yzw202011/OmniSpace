// 本项目仅供学习使用，商业授权请+Q 3559331368
/* ==========================================================================
 * format.ts —— 格式化工具函数
 * --------------------------------------------------------------------------
 * 提供百分比、文件大小、时间、显存等通用格式化方法。
 * 适用于 OmniSpace AI v2.1 全部前端模块。
 * ========================================================================== */

/**
 * 格式化百分比
 * @param value 数值（0~100 或 0~1）
 * @param isRatio 是否为比值（0~1），默认 false（0~100）
 * @returns 如 "85%"
 */
export function formatPercent(value: number | null | undefined, isRatio = false): string {
  if (value === null || value === undefined || isNaN(value)) return '--';
  const pct = isRatio ? value * 100 : value;
  return Math.round(pct) + '%';
}

/**
 * 格式化文件大小
 * @param bytes 字节数
 * @returns 如 "1.5 GB"、"256 MB"
 */
export function formatFileSize(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || isNaN(bytes)) return '-';
  if (bytes === 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  const k = 1024;
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  const size = bytes / Math.pow(k, i);
  // B 级别不保留小数，KB 以上保留 1 位
  return (i === 0 ? Math.round(size) : size.toFixed(1)) + ' ' + units[i];
}

/**
 * 格式化显存大小（MB 为单位输入）
 * @param mb 显存兆字节数
 * @returns 如 "4.0 GB / 8.0 GB"
 */
export function formatVRAM(mb: number | null | undefined): string {
  if (mb === null || mb === undefined || isNaN(mb)) return '-';
  if (mb >= 1024) return (mb / 1024).toFixed(1) + ' GB';
  return Math.round(mb) + ' MB';
}

/**
 * 格式化时间（秒转 mm:ss 或 hh:mm:ss）
 * @param seconds 秒数
 * @returns 如 "01:30" 或 "1:02:30"
 */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || isNaN(seconds)) return '--';
  const s = Math.floor(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const pad = (n: number) => String(n).padStart(2, '0');
  if (h > 0) return `${h}:${pad(m)}:${pad(sec)}`;
  return `${pad(m)}:${pad(sec)}`;
}

/**
 * 入参转 Date：数值按秒级 epoch 自适应放大到毫秒
 * （后端 created_at 约定为 time.time() 秒；直接 new Date(秒) 会按毫秒
 * 解析成 1970-01，2026 年秒值 1.78e9 → 显示 1970-01-22。阈值 1e11
 * 区分：秒 < 1e11 < 毫秒，毫秒级入参不受影响）
 */
function toDateSafe(date: Date | number | string | null | undefined): Date | null {
  if (date === null || date === undefined || date === 0 || date === '') return null;
  if (typeof date === 'number' && date > 0 && date < 1e11) {
    return new Date(date * 1000);
  }
  const d = new Date(date);
  return isNaN(d.getTime()) ? null : d;
}

/**
 * 格式化日期时间
 * @param date 日期对象、时间戳或日期字符串
 * @param withSeconds 是否包含秒
 * @returns 如 "2026-08-05 14:30"
 */
export function formatDateTime(date: Date | number | string | null | undefined, withSeconds = false): string {
  const d = toDateSafe(date);
  if (!d) return '--';
  const pad = (n: number) => String(n).padStart(2, '0');
  const y = d.getFullYear();
  const mo = pad(d.getMonth() + 1);
  const da = pad(d.getDate());
  const h = pad(d.getHours());
  const mi = pad(d.getMinutes());
  const s = pad(d.getSeconds());
  return withSeconds ? `${y}-${mo}-${da} ${h}:${mi}:${s}` : `${y}-${mo}-${da} ${h}:${mi}`;
}

/**
 * 格式化相对时间（"3分钟前"）
 * @param date 日期对象、时间戳或日期字符串
 * @returns 如 "刚刚"、"3分钟前"、"2小时前"、"3天前"
 */
export function formatRelativeTime(date: Date | number | string | null | undefined): string {
  const d = toDateSafe(date);
  if (!d) return '--';
  const diff = Date.now() - d.getTime();
  if (diff < 0) return formatDateTime(d);
  const sec = Math.floor(diff / 1000);
  if (sec < 60) return '刚刚';
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}分钟前`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}小时前`;
  const day = Math.floor(hr / 24);
  if (day < 30) return `${day}天前`;
  return formatDateTime(d);
}

/**
 * 格式化数字（千分位）
 * @param value 数值
 * @returns 如 "1,234,567"
 */
export function formatNumber(value: number | null | undefined): string {
  if (value === null || value === undefined || isNaN(value)) return '--';
  return value.toLocaleString('zh-CN');
}

/**
 * 格式化参数量
 * @param params 参数数量
 * @returns 如 "7B"、"13B"、"1.5T"
 */
export function formatParams(params: number | null | undefined): string {
  if (params === null || params === undefined || isNaN(params)) return '--';
  if (params >= 1e12) return (params / 1e12).toFixed(1) + 'T';
  if (params >= 1e9) return (params / 1e9).toFixed(1) + 'B';
  if (params >= 1e6) return (params / 1e6).toFixed(1) + 'M';
  if (params >= 1e3) return (params / 1e3).toFixed(1) + 'K';
  return String(params);
}

/**
 * 负载着色：根据百分比返回内联样式
 * @param value 百分比（0~100）
 * @returns CSS color 样式对象
 */
export function getLoadColor(value: number | null | undefined): { color: string } | null {
  if (value === null || value === undefined || isNaN(value)) return null;
  if (value >= 85) return { color: 'var(--color-error)' };
  if (value >= 50) return { color: 'var(--color-warning)' };
  return { color: 'var(--color-success)' };
}

/**
 * 截断文本并添加省略号
 * @param text 原文
 * @param maxLen 最大长度
 * @returns 截断后的文本
 */
export function truncate(text: string | null | undefined, maxLen: number): string {
  if (!text) return '';
  if (text.length <= maxLen) return text;
  return text.slice(0, maxLen) + '...';
}
