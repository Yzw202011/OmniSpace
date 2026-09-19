// 本项目仅供学习使用，商业授权请+Q 3553191368
/* ==========================================================================
 * GlobalSearch.tsx —— 全局搜索命令面板（批6 P27，2026-09-19）
 * --------------------------------------------------------------------------
 * Ctrl+K 呼出：一处搜会话/作品（漫画+漫剧+小说）/模型/设置项/帮助 FAQ，
 * Enter 直达。数据源：各列表端点轻量拉取（打开时拉一次+30s 缓存）；
 * Esc 关闭；↑↓ 选择。与帮助页快捷键表同步维护。
 * ========================================================================== */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Search } from 'lucide-react';
import { get } from '@/services/api';
import { reportBgError } from '@/utils/errors';

interface SearchItem {
  kind: string;
  label: string;
  sub: string;
  route: string;
}

export default function GlobalSearch() {
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [keyword, setKeyword] = useState('');
  const [idx, setIdx] = useState(0);
  const [pool, setPool] = useState<SearchItem[]>([]);
  const inputRef = useRef<HTMLInputElement | null>(null);

  // 数据源：打开时拉一次（轻量限量），30s 内不重拉
  const loadPool = useCallback(() => {
    const jobs: Array<Promise<SearchItem[]>> = [
      get<{ items?: Array<{ id: string; title?: string }> }>('/chat/sessions')
        .then((d) => (d.items ?? []).slice(0, 30).map((s) => ({
          kind: '会话', label: s.title || '未命名会话', sub: '', route: '/chat',
        }))).catch(() => []),
      get<{ items?: Array<{ project_id: string; name: string }> }>('/comic/project/list?type=comic')
        .then((d) => (d.items ?? []).slice(0, 20).map((p) => ({
          kind: '漫画', label: p.name, sub: '', route: '/paint',
        }))).catch(() => []),
      get<{ items?: Array<{ project_id: string; name: string }> }>('/comic/project/list?type=manga')
        .then((d) => (d.items ?? []).slice(0, 20).map((p) => ({
          kind: '漫剧', label: p.name, sub: '', route: '/storyboard',
        }))).catch(() => []),
      get<{ items?: Array<{ id: string; name: string }> }>('/novel/project/list')
        .then((d) => (d.items ?? []).slice(0, 20).map((p) => ({
          kind: '写作', label: p.name, sub: '', route: '/novel',
        }))).catch(() => []),
      get<{ items?: Array<{ id: string; name: string }> }>('/models/list')
        .then((d) => (d.items ?? []).slice(0, 20).map((m) => ({
          kind: '模型', label: m.name || m.id, sub: '', route: '/models',
        }))).catch(() => []),
    ];
    Promise.all(jobs).then((groups) => {
      setPool([
        ...groups.flat(),
        ...SETTINGS_ITEMS, ...HELP_ITEMS,
      ]);
    }).catch((err: unknown) => reportBgError('GlobalSearch', err));
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        setOpen((v) => !v);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  useEffect(() => {
    if (open) {
      if (pool.length === 0) loadPool();
      setKeyword(''); setIdx(0);
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [open, pool.length, loadPool]);

  const results = useMemo(() => {
    const kw = keyword.trim().toLowerCase();
    if (!kw) return pool.slice(0, 12);
    return pool
      .filter((it) => it.label.toLowerCase().includes(kw)
        || it.kind.includes(kw) || it.sub.toLowerCase().includes(kw))
      .slice(0, 12);
  }, [pool, keyword]);

  const go = useCallback((route: string) => {
    setOpen(false);
    navigate(route);
  }, [navigate]);

  if (!open) return null;
  return (
    <div
      role="dialog"
      aria-label="全局搜索"
      onClick={() => setOpen(false)}
      style={{
        position: 'fixed', inset: 0, zIndex: 2200,
        background: 'rgba(0,0,0,0.5)', backdropFilter: 'blur(2px)',
        display: 'flex', justifyContent: 'center',
        paddingTop: '12vh',
      }}
    >
      <div
        className="card"
        onClick={(e) => e.stopPropagation()}
        style={{ width: 'min(560px, calc(100vw - 32px))',
                 padding: 'var(--space-3) var(--space-4)' }}
      >
        <div className="flex items-center gap-2">
          <Search size={16} aria-hidden="true" />
          <input
            ref={inputRef}
            className="input"
            style={{ border: 'none', background: 'transparent', flex: 1 }}
            placeholder="搜会话 / 作品 / 模型 / 设置…（Enter 直达，Esc 关闭）"
            value={keyword}
            onChange={(e) => { setKeyword(e.target.value); setIdx(0); }}
            onKeyDown={(e) => {
              if (e.key === 'Escape') setOpen(false);
              if (e.key === 'ArrowDown') { e.preventDefault(); setIdx((i) => Math.min(i + 1, results.length - 1)); }
              if (e.key === 'ArrowUp') { e.preventDefault(); setIdx((i) => Math.max(0, i - 1)); }
              if (e.key === 'Enter' && results[idx]) go(results[idx].route);
            }}
          />
        </div>
        <div className="flex flex-col gap-0.5" style={{ marginTop: 'var(--space-2)', maxHeight: 360, overflowY: 'auto' }}>
          {results.length === 0 && (
            <div className="text-secondary text-sm" style={{ padding: 'var(--space-2)' }}>
              没有匹配——换个关键词试试
            </div>
          )}
          {results.map((it, i) => (
            <button
              key={`${it.kind}-${it.label}-${i}`}
              type="button"
              className="flex items-center gap-2"
              style={{
                padding: 'var(--space-2) var(--space-3)',
                borderRadius: 'var(--radius-sm)',
                background: i === idx ? 'var(--color-primary-50)' : 'transparent',
                border: 'none', cursor: 'pointer', textAlign: 'left',
              }}
              onMouseEnter={() => setIdx(i)}
              onClick={() => go(it.route)}
            >
              <span className="badge primary" style={{ flexShrink: 0 }}>{it.kind}</span>
              <span className="text-sm ellipsis">{it.label}</span>
              {it.sub && <span className="text-tertiary text-xs">{it.sub}</span>}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

const SETTINGS_ITEMS: SearchItem[] = [
  { kind: '设置', label: '通用设置', sub: '', route: '/settings?tab=general' },
  { kind: '设置', label: '外观与主题', sub: '', route: '/settings?tab=appearance' },
  { kind: '设置', label: 'AI 服务（云端API）', sub: '', route: '/settings?tab=ai' },
  { kind: '设置', label: '数据与备份', sub: '', route: '/settings?tab=data' },
  { kind: '设置', label: '插件管理', sub: '', route: '/settings?tab=plugins' },
  { kind: '设置', label: '维护（体检/升级）', sub: '', route: '/settings?tab=maintenance' },
];

const HELP_ITEMS: SearchItem[] = [
  { kind: '帮助', label: '使用指南', sub: '', route: '/help' },
  { kind: '帮助', label: '常见问题', sub: '', route: '/help' },
  { kind: '帮助', label: '快捷键', sub: '', route: '/help' },
];
