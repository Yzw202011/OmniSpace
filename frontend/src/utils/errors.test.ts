/* ==========================================================================
 * 前端错误呈现三分法防回归测试（TASK-P2-08，审计 P16）
 * --------------------------------------------------------------------------
 * 1. 静态扫描：src/ 内禁止 `.catch(() => undefined/null/void 0/{})` 与空
 *    catch 块（策略见 docs/design/frontend-error-policy.md）；
 * 2. 真源唯一：禁止再定义本地 getErrMessage/errMsg 副本；
 * 3. 单元语义：getErrorMessage 提取、isBenignError 白名单判定、
 *    reportBgError 输出格式、reportActionError → showToast 透出。
 * 漂移即红：新增静默吞错或本地副本时 npm run test 直接失败。
 * ========================================================================== */

import { describe, it, expect, vi, afterEach } from 'vitest';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { readdirSync, statSync } from 'node:fs';
import {
  getErrorMessage,
  isBenignError,
  reportActionError,
  reportBgError,
} from './errors';
import { useAppStore } from '@/stores/useAppStore';

const SRC_ROOT = path.resolve(process.cwd(), 'src');

/** 递归收集源码文件（排除测试自身与策略模块——后者注释里含被禁字样） */
function collectSourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const full = path.join(dir, name);
    if (statSync(full).isDirectory()) {
      out.push(...collectSourceFiles(full));
    } else if (/\.(ts|tsx)$/.test(name) && !/\.test\.ts$/.test(name) && full !== __filename) {
      out.push(full);
    }
  }
  return out;
}

const FILES = collectSourceFiles(SRC_ROOT).filter(
  (f) => !f.endsWith(path.join('utils', 'errors.ts')),
);

describe('静态扫描：禁止静默吞错（三分法铁律）', () => {
  it('源码中不存在 `.catch(() => undefined/null/void 0/{})`', () => {
    const re = /\.catch\(\s*\(\s*\)\s*=>\s*(?:undefined|null|void 0|\{\})\s*\)/;
    const offenders: string[] = [];
    for (const f of FILES) {
      readFileSync(f, 'utf-8').split(/\r?\n/).forEach((line, i) => {
        if (re.test(line)) offenders.push(`${path.relative(SRC_ROOT, f)}:${i + 1}`);
      });
    }
    expect(offenders, `发现静默吞错：\n${offenders.join('\n')}\n→ 用 reportActionError/reportBgError（@/utils/errors）`).toEqual([]);
  });

  it('源码中不存在空 catch 块（单行形态）', () => {
    const re = /catch\s*(?:\([^)]*\))?\s*\{\s*\}/;
    const offenders: string[] = [];
    for (const f of FILES) {
      readFileSync(f, 'utf-8').split(/\r?\n/).forEach((line, i) => {
        if (re.test(line)) offenders.push(`${path.relative(SRC_ROOT, f)}:${i + 1}`);
      });
    }
    expect(offenders, `发现空 catch 块：\n${offenders.join('\n')}`).toEqual([]);
  });

  it('错误消息提取真源唯一：无本地 getErrMessage/errMsg 副本', () => {
    const re = /(?:function\s+getErrMessage\s*\(|const\s+errMsg\s*=)/;
    const offenders: string[] = [];
    for (const f of FILES) {
      readFileSync(f, 'utf-8').split(/\r?\n/).forEach((line, i) => {
        if (re.test(line)) offenders.push(`${path.relative(SRC_ROOT, f)}:${i + 1}`);
      });
    }
    expect(offenders, `发现本地副本定义：\n${offenders.join('\n')}\n→ 统一 import { getErrorMessage } from '@/utils/errors'`).toEqual([]);
  });
});

describe('getErrorMessage：错误对象 → 用户可读文案', () => {
  it('Error 实例取 message', () => {
    expect(getErrorMessage(new Error('网络中断'), '兜底')).toBe('网络中断');
  });

  it('带 message 的普通对象取 message', () => {
    expect(getErrorMessage({ message: '后端 500' }, '兜底')).toBe('后端 500');
  });

  it('字符串错误原样返回', () => {
    expect(getErrorMessage('纯文本错误', '兜底')).toBe('纯文本错误');
  });

  it('空 message / null / undefined 回退 fallback', () => {
    expect(getErrorMessage({ message: '   ' }, '兜底')).toBe('兜底');
    expect(getErrorMessage(null, '兜底')).toBe('兜底');
    expect(getErrorMessage(undefined, '兜底')).toBe('兜底');
    expect(getErrorMessage(42, '兜底')).toBe('兜底');
  });

  it('后端 suggestion 字段随 message 一并呈现（漫剧生图未加载指引）', () => {
    expect(
      getErrorMessage(
        { message: '绘画模型未就绪', suggestion: '可到「模型管理」手动加载' },
        '兜底',
      ),
    ).toBe('绘画模型未就绪。可到「模型管理」手动加载');
  });

  it('suggestion 已含在 message 中时不重复拼接', () => {
    expect(
      getErrorMessage(
        { message: '失败。可到「模型管理」手动加载', suggestion: '可到「模型管理」手动加载' },
        '兜底',
      ),
    ).toBe('失败。可到「模型管理」手动加载');
  });

  it('无 suggestion / 非 string suggestion 不影响原文案', () => {
    expect(getErrorMessage({ message: '普通错误' }, '兜底')).toBe('普通错误');
    expect(getErrorMessage({ message: '普通错误', suggestion: 42 }, '兜底')).toBe('普通错误');
  });
});

describe('isBenignError：白名单判定（主动取消类）', () => {
  it('AbortError 命中白名单', () => {
    expect(isBenignError(new DOMException('aborted', 'AbortError'))).toBe(true);
    const named = new Error('cancelled');
    named.name = 'AbortError';
    expect(isBenignError(named)).toBe(true);
  });

  it('普通错误不命中', () => {
    expect(isBenignError(new Error('real failure'))).toBe(false);
    expect(isBenignError(null)).toBe(false);
  });
});

describe('两个呈现入口', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('reportBgError：console.warn 带 [scope] 前缀', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const err = new Error('刷新失败');
    reportBgError('StoryboardTable.prefetchKeyframes', err);
    expect(warn).toHaveBeenCalledTimes(1);
    expect(warn).toHaveBeenCalledWith('[StoryboardTable.prefetchKeyframes]', err);
  });

  it('reportActionError：toast error 级透出 err.message', () => {
    const toast = vi.spyOn(useAppStore.getState(), 'showToast').mockImplementation(() => {});
    reportActionError(new Error('磁盘已满'), '分镜保存');
    expect(toast).toHaveBeenCalledWith('磁盘已满', 'error');
  });

  it('reportActionError：无 message 时用「动作名失败」兜底', () => {
    const toast = vi.spyOn(useAppStore.getState(), 'showToast').mockImplementation(() => {});
    reportActionError(null, '导演台进度保存');
    expect(toast).toHaveBeenCalledWith('导演台进度保存失败', 'error');
  });
});
