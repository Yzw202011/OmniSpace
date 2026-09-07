/* ==========================================================================
 * NovelPage.tsx —— 写作台（小说模块 MVP，/novel，批2 2026-09-05）
 * --------------------------------------------------------------------------
 * 结构：
 *   ProjectLibrary  书架（项目卡片 + 新建表单）
 *   NovelWorkspace  工作区三栏：
 *     左  OutlinePanel  大纲树（作品→卷→章细纲，可编辑）
 *     中  ChapterPanel  章节列表 + 正文编辑器（生成/保存/取消）
 *     右  SidePanel     角色 / 伏笔账本 / 世界观入库
 * 生成链路：入队即返回（后端串行队列），本页 3s 轮询进度（POLL_MS 在
 * store）；模型未加载时后端自动装载（产品铁律），前端无需「再点一次」。
 * ========================================================================== */

import React, { useEffect, useMemo, useState } from 'react';
import {
  AlertCircle,
  ArrowLeft,
  BookOpen,
  Bookmark,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Clock,
  Download,
  Feather,
  Loader2,
  Play,
  Plus,
  Save,
  Sparkles,
  Square,
  Trash2,
  Users,
  XCircle,
} from 'lucide-react';
import { useNovelStore } from '@/stores/useNovelStore';
import type {
  NovelChapter,
  NovelForeshadow,
  NovelOutlineNode,
} from '@/services/novelApi';

/** 章节状态 → 视觉标记 */
const STATUS_META: Record<
  NovelChapter['status'],
  { label: string; cls: string; icon: React.ReactNode }
> = {
  pending: {
    label: '待写',
    cls: 'text-white/50 border-white/20',
    icon: <Clock size={12} aria-hidden="true" />,
  },
  generating: {
    label: '生成中',
    cls: 'text-sky-300 border-sky-400/40',
    icon: <Loader2 size={12} className="animate-spin" aria-hidden="true" />,
  },
  done: {
    label: '已完成',
    cls: 'text-emerald-300 border-emerald-400/40',
    icon: <CheckCircle2 size={12} aria-hidden="true" />,
  },
  error: {
    label: '失败',
    cls: 'text-rose-300 border-rose-400/40',
    icon: <XCircle size={12} aria-hidden="true" />,
  },
};

function downloadText(filename: string, content: string): void {
  const blob = new Blob([content], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

export const NovelPage: React.FC = () => {
  const project = useNovelStore((s) => s.project);
  const err = useNovelStore((s) => s.err);
  const notice = useNovelStore((s) => s.notice);
  const fetchProjects = useNovelStore((s) => s.fetchProjects);
  const setErr = useNovelStore((s) => s.setErr);
  const setNotice = useNovelStore((s) => s.setNotice);

  useEffect(() => {
    void fetchProjects();
  }, [fetchProjects]);

  return (
    <div className="page">
      <h1 className="page-title">
        <Feather size={20} aria-hidden="true" /> 写作台
      </h1>
      <p className="page-subtitle">AI 小说创作：灵感 → 大纲 → 章节 → 导出（每一步可人工修改）</p>

      {err && (
        <div className="card border border-rose-400/40 mb-3 flex items-center gap-2 text-rose-200 text-sm">
          <AlertCircle size={16} aria-hidden="true" />
          <span className="flex-1">{err}</span>
          <button className="btn btn-ghost btn-sm" onClick={() => setErr('')}>
            关闭
          </button>
        </div>
      )}
      {notice && (
        <div className="card border border-sky-400/40 mb-3 flex items-center gap-2 text-sky-200 text-sm">
          <Sparkles size={16} aria-hidden="true" />
          <span className="flex-1">{notice}</span>
          <button className="btn btn-ghost btn-sm" onClick={() => setNotice('')}>
            关闭
          </button>
        </div>
      )}

      {project ? <NovelWorkspace /> : <ProjectLibrary />}
    </div>
  );
};

/* ══ 书架 ═══════════════════════════════════════════════════ */

const ProjectLibrary: React.FC = () => {
  const projects = useNovelStore((s) => s.projects);
  const busy = useNovelStore((s) => s.busy);
  const createProject = useNovelStore((s) => s.createProject);
  const openProject = useNovelStore((s) => s.openProject);
  const removeProject = useNovelStore((s) => s.removeProject);
  const [form, setForm] = useState({ name: '', genre: '', description: '' });

  const submit = async () => {
    if (!form.name.trim()) return;
    if (await createProject(form)) setForm({ name: '', genre: '', description: '' });
  };

  return (
    <div className="grid grid-cols-[1fr_320px] gap-4 mt-4">
      {/* 项目卡片 */}
      <div className="grid grid-cols-2 gap-3 content-start">
        {projects.length === 0 && (
          <div className="card text-white/50 text-sm col-span-2">
            还没有作品。右侧填一句灵感，创建后一键让 AI 出大纲。
          </div>
        )}
        {projects.map((p) => (
          <div key={p.id} className="card hoverable flex flex-col gap-2">
            <div className="flex items-center gap-2">
              <BookOpen size={16} className="text-[var(--color-primary,#FF6B9D)]" aria-hidden="true" />
              <span className="font-medium flex-1 truncate">{p.name}</span>
              <button
                className="btn btn-ghost btn-sm text-rose-300"
                title="删除作品（章节/大纲一并删除）"
                onClick={() => {
                  if (window.confirm(`删除《${p.name}》？全部章节与大纲会一并删除。`)) {
                    void removeProject(p.id);
                  }
                }}
              >
                <Trash2 size={14} aria-hidden="true" />
              </button>
            </div>
            <div className="text-xs text-white/50 line-clamp-2 min-h-[2rem]">
              {p.description || '（无简介）'}
            </div>
            <div className="text-xs text-white/40">
              {p.genre || '未设题材'} · {p.chapter_done}/{p.chapter_total} 章完成 ·{' '}
              {p.total_words} 字
            </div>
            <button className="btn btn-secondary btn-sm" onClick={() => void openProject(p.id)}>
              打开写作
            </button>
          </div>
        ))}
      </div>

      {/* 新建表单 */}
      <div className="card h-fit flex flex-col gap-2">
        <h3 className="card-title">
          <Plus size={16} aria-hidden="true" /> 新建作品
        </h3>
        <input
          className="input"
          placeholder="作品名（必填）"
          value={form.name}
          maxLength={60}
          onChange={(e) => setForm({ ...form, name: e.target.value })}
        />
        <input
          className="input"
          placeholder="题材：玄幻 / 都市 / 悬疑 / 科幻…"
          value={form.genre}
          maxLength={40}
          onChange={(e) => setForm({ ...form, genre: e.target.value })}
        />
        <textarea
          className="input h-28 resize-none"
          placeholder="一句话灵感：比如「外卖员觉醒了能听见手机预备言的系统」"
          value={form.description}
          maxLength={2000}
          onChange={(e) => setForm({ ...form, description: e.target.value })}
        />
        <button className="btn btn-primary" disabled={busy || !form.name.trim()} onClick={() => void submit()}>
          <Plus size={14} aria-hidden="true" /> 创建作品
        </button>
      </div>
    </div>
  );
};

/* ══ 工作区 ═════════════════════════════════════════════════ */

const NovelWorkspace: React.FC = () => {
  const project = useNovelStore((s) => s.project)!;
  const outlines = useNovelStore((s) => s.outlines);
  const chapters = useNovelStore((s) => s.chapters);
  const progress = useNovelStore((s) => s.progress);
  const busy = useNovelStore((s) => s.busy);
  const closeProject = useNovelStore((s) => s.closeProject);
  const generateOutline = useNovelStore((s) => s.generateOutline);
  const generateAllChapters = useNovelStore((s) => s.generateAllChapters);
  const addChapter = useNovelStore((s) => s.addChapter);
  const pollProgress = useNovelStore((s) => s.pollProgress);

  // 进度轮询：3s 一拍（生成排队/进行中时刷新章节状态与正文回填）
  useEffect(() => {
    const timer = window.setInterval(() => void pollProgress(), 3000);
    return () => window.clearInterval(timer);
  }, [pollProgress]);

  const hasOutline = outlines.length > 0;
  const currentTask = progress?.current ?? null;

  const doExport = async (fmt: 'txt' | 'md') => {
    try {
      const { exportBook } = await import('@/services/novelApi');
      const r = await exportBook(project.id, fmt);
      downloadText(r.filename, r.content);
    } catch {
      /* 错误已由 api.ts 抛出，store 轮询侧不覆盖此处；静默即可 */
    }
  };

  return (
    <div className="flex flex-col gap-3 mt-4">
      {/* 工具栏 */}
      <div className="card flex items-center gap-2 flex-wrap">
        <button className="btn btn-ghost btn-sm" onClick={closeProject}>
          <ArrowLeft size={14} aria-hidden="true" /> 书架
        </button>
        <span className="font-medium">{project.name}</span>
        <span className="text-xs text-white/40">
          {project.genre || '未设题材'} · {project.description.slice(0, 30) || '无简介'}
        </span>
        <span className="flex-1" />
        <button
          className="btn btn-secondary btn-sm"
          disabled={busy}
          onClick={() => {
            if (hasOutline && !window.confirm(
              '重新生成会替换现有大纲树和待写章节（已写正文的章节保留）。继续？',
            )) {
              return;
            }
            void generateOutline();
          }}
        >
          <Sparkles size={14} aria-hidden="true" /> {hasOutline ? '重新生成大纲' : 'AI 生成大纲'}
        </button>
        <button
          className="btn btn-secondary btn-sm"
          disabled={chapters.length === 0}
          title="跳过已完成章节，只生成未完成的（想全部重做请点章节里的重新生成）"
          onClick={() => void generateAllChapters(true)}
        >
          <Play size={14} aria-hidden="true" /> 生成未完成章节
        </button>
        <button
          className="btn btn-ghost btn-sm"
          onClick={() => void addChapter(`新章节`, '')}
        >
          <Plus size={14} aria-hidden="true" /> 新增章
        </button>
        <button className="btn btn-ghost btn-sm" onClick={() => void doExport('txt')}>
          <Download size={14} aria-hidden="true" /> TXT
        </button>
        <button className="btn btn-ghost btn-sm" onClick={() => void doExport('md')}>
          <Download size={14} aria-hidden="true" /> MD
        </button>
      </div>

      {currentTask && (
        <div className="card border border-sky-400/40 text-sky-200 text-sm flex items-center gap-2">
          <Loader2 size={14} className="animate-spin" aria-hidden="true" />
          正在执行：{currentTask.kind === 'chapter' ? '章节生成' : currentTask.kind === 'outline' ? '大纲生成' : '角色设计'}
          {progress && progress.queue.length > 0 && `（后面还排着 ${progress.queue.length} 个任务）`}
          <span className="flex-1" />
          <button className="btn btn-ghost btn-sm" onClick={() => void useNovelStore.getState().cancelTask(currentTask.task_id)}>
            <Square size={12} aria-hidden="true" /> 取消
          </button>
        </div>
      )}

      {!hasOutline && (
        <div className="card text-white/60 text-sm">
          这本书还没有大纲。点上方「AI 生成大纲」，AI 会先出「卷 → 章细纲」的树，
          每一节都可以改；满意后再逐章或批量生成正文。
        </div>
      )}

      {/* 三栏 */}
      <div className="grid grid-cols-[280px_1fr_300px] gap-3 items-start">
        <OutlinePanel />
        <ChapterPanel />
        <SidePanel />
      </div>
    </div>
  );
};

/* ══ 左栏：大纲树 ═══════════════════════════════════════════ */

const OutlinePanel: React.FC = () => {
  const outlines = useNovelStore((s) => s.outlines);
  const saveOutline = useNovelStore((s) => s.saveOutline);
  const removeOutline = useNovelStore((s) => s.removeOutline);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState({ title: '', content: '' });

  const tree = useMemo(() => {
    const book = outlines.find((o) => o.level === 'book');
    const volumes = outlines.filter((o) => o.level === 'volume');
    const children = outlines.filter((o) => o.level === 'chapter_outline');
    return { book, volumes, children };
  }, [outlines]);

  const startEdit = (n: NovelOutlineNode) => {
    setEditing(n.id);
    setDraft({ title: n.title, content: n.content });
  };

  const renderNode = (n: NovelOutlineNode, indent: number) => {
    const isOpen = !collapsed[n.id];
    const kids = tree.children.filter((c) => c.parent_id === n.id);
    return (
      <div key={n.id} style={{ marginLeft: indent * 12 }}>
        <div className="flex items-center gap-1 py-0.5 group">
          {kids.length > 0 ? (
            <button
              className="text-white/40 hover:text-white"
              onClick={() => setCollapsed({ ...collapsed, [n.id]: isOpen })}
              aria-label={isOpen ? '收起' : '展开'}
            >
              {isOpen ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
            </button>
          ) : (
            <span className="w-[13px]" />
          )}
          <button
            className="flex-1 text-left text-xs truncate hover:text-[var(--color-primary,#FF6B9D)]"
            title={n.title}
            onClick={() => setCollapsed({ ...collapsed, [n.id]: !isOpen })}
          >
            {n.level === 'book' ? (
              <BookOpen size={12} className="inline mr-1 -mt-0.5" aria-hidden="true" />
            ) : null}
            {n.title}
          </button>
          <button
            className="btn btn-ghost btn-sm opacity-0 group-hover:opacity-100"
            title="编辑"
            onClick={() => startEdit(n)}
          >
            改
          </button>
          <button
            className="btn btn-ghost btn-sm opacity-0 group-hover:opacity-100 text-rose-300"
            title="删除（章细纲会一并移除该节点）"
            onClick={() => {
              if (window.confirm(`删除大纲节点「${n.title}」？`)) void removeOutline(n.id);
            }}
          >
            <Trash2 size={12} aria-hidden="true" />
          </button>
        </div>
        {editing === n.id && (
          <div className="flex flex-col gap-1 mb-2">
            <input
              className="input text-xs"
              value={draft.title}
              maxLength={120}
              onChange={(e) => setDraft({ ...draft, title: e.target.value })}
            />
            <textarea
              className="input text-xs h-24 resize-none"
              value={draft.content}
              maxLength={8000}
              onChange={(e) => setDraft({ ...draft, content: e.target.value })}
            />
            <div className="flex gap-1">
              <button
                className="btn btn-primary btn-sm"
                onClick={() => {
                  void saveOutline(n.id, draft);
                  setEditing(null);
                }}
              >
                <Save size={12} aria-hidden="true" /> 保存
              </button>
              <button className="btn btn-ghost btn-sm" onClick={() => setEditing(null)}>
                取消
              </button>
            </div>
          </div>
        )}
        {isOpen && kids.map((k) => renderNode(k, indent + 1))}
      </div>
    );
  };

  if (!tree.book) {
    return (
      <div className="card text-white/40 text-xs">
        尚无大纲。生成后这里会出现「作品 → 卷 → 章细纲」的可编辑树。
      </div>
    );
  }
  return (
    <div className="card">
      <h3 className="card-title">大纲树</h3>
      <div className="max-h-[60vh] overflow-auto">
        {tree.book && renderNode(tree.book, 0)}
        {tree.volumes.map((v) => renderNode(v, 1))}
      </div>
    </div>
  );
};

/* ══ 中栏：章节列表 + 编辑器 ═══════════════════════════════ */

const ChapterPanel: React.FC = () => {
  const chapters = useNovelStore((s) => s.chapters);
  const chapter = useNovelStore((s) => s.chapter);
  const selectChapter = useNovelStore((s) => s.selectChapter);
  const saveChapter = useNovelStore((s) => s.saveChapter);
  const generateChapter = useNovelStore((s) => s.generateChapter);
  const cancelTask = useNovelStore((s) => s.cancelTask);
  const removeChapter = useNovelStore((s) => s.removeChapter);
  const progress = useNovelStore((s) => s.progress);
  const [title, setTitle] = useState('');
  const [content, setContent] = useState('');
  const [dirty, setDirty] = useState(false);

  // 切换选中章 → 同步编辑器内容
  useEffect(() => {
    if (chapter) {
      setTitle(chapter.title);
      setContent(chapter.content);
      setDirty(false);
    }
  }, [chapter?.id, chapter?.status, chapter]); // eslint-disable-line react-hooks/exhaustive-deps

  const currentTaskForChapter = progress?.current?.chapter_id === chapter?.id
    ? progress?.current
    : null;

  if (chapters.length === 0) {
    return (
      <div className="card text-white/40 text-sm">
        还没有章节。先生成大纲（会自动建好待写章节），或用「新增章」手工添加。
      </div>
    );
  }

  return (
    <div className="card flex flex-col gap-2">
      <div className="flex gap-1 flex-wrap max-h-28 overflow-auto">
        {chapters.map((c) => {
          const meta = STATUS_META[c.status];
          return (
            <button
              key={c.id}
              className={`btn btn-sm ${chapter?.id === c.id ? 'btn-primary' : 'btn-ghost'} border ${meta.cls} leading-none`}
              title={`${meta.label}${c.error ? `：${c.error}` : ''}`}
              onClick={() => void selectChapter(c.id)}
            >
              {meta.icon} 第{c.chapter_index}章 {c.title || '（未命名）'}
            </button>
          );
        })}
      </div>

      {chapter ? (
        <div className="flex flex-col gap-2">
          <div className="flex items-center gap-2">
            <input
              className="input flex-1"
              value={title}
              maxLength={120}
              onChange={(e) => {
                setTitle(e.target.value);
                setDirty(true);
              }}
            />
            <span className="text-xs text-white/40">{content.length} 字</span>
          </div>
          <textarea
            className="input font-medium leading-relaxed"
            style={{ minHeight: '48vh' }}
            placeholder="点上方章节 chip 选一章；「生成」让 AI 按细纲写正文，写完可自由修改后保存。"
            value={content}
            onChange={(e) => {
              setContent(e.target.value);
              setDirty(true);
            }}
          />
          {chapter.status === 'generating' && (
            <div className="h-1.5 rounded bg-white/10 overflow-hidden">
              <div
                className="h-full bg-sky-400 transition-all"
                style={{ width: `${Math.round((chapter.progress || 0) * 100)}%` }}
              />
            </div>
          )}
          {chapter.status === 'error' && chapter.error && (
            <div className="text-xs text-rose-300">{chapter.error}</div>
          )}
          <div className="flex items-center gap-2">
            {chapter.status !== 'generating' && (
              <button className="btn btn-primary btn-sm" onClick={() => void generateChapter(chapter.id)}>
                <Play size={12} aria-hidden="true" /> {chapter.status === 'done' ? '重新生成本章' : '生成本章'}
              </button>
            )}
            {currentTaskForChapter && (
              <button
                className="btn btn-secondary btn-sm"
                onClick={() => void cancelTask(currentTaskForChapter.task_id)}
              >
                <Square size={12} aria-hidden="true" /> 取消生成
              </button>
            )}
            <button
              className="btn btn-secondary btn-sm"
              disabled={!dirty || chapter.status === 'generating'}
              onClick={async () => {
                await saveChapter({ title, content });
                setDirty(false);
              }}
            >
              <Save size={12} aria-hidden="true" /> 保存修改
            </button>
            <span className="flex-1" />
            {chapter.summary && (
              <span className="text-xs text-white/40 truncate max-w-[40%]" title={chapter.summary}>
                前情：{chapter.summary}
              </span>
            )}
            <button
              className="btn btn-ghost btn-sm text-rose-300"
              onClick={() => {
                if (window.confirm('删除本章？')) void removeChapter(chapter.id);
              }}
            >
              <Trash2 size={12} aria-hidden="true" />
            </button>
          </div>
        </div>
      ) : (
        <div className="text-white/40 text-sm">选一章开始写作。</div>
      )}
    </div>
  );
};

/* ══ 右栏：角色 / 伏笔 / 世界观 ═════════════════════════════ */

type SideTab = 'characters' | 'foreshadows' | 'worldbuild';

const SidePanel: React.FC = () => {
  const [tab, setTab] = useState<SideTab>('characters');
  const tabs: { key: SideTab; label: string; icon: React.ReactNode }[] = [
    { key: 'characters', label: '角色', icon: <Users size={13} aria-hidden="true" /> },
    { key: 'foreshadows', label: '伏笔', icon: <Bookmark size={13} aria-hidden="true" /> },
    { key: 'worldbuild', label: '世界观', icon: <BookOpen size={13} aria-hidden="true" /> },
  ];
  return (
    <div className="card flex flex-col gap-2">
      <div className="flex gap-1">
        {tabs.map((t) => (
          <button
            key={t.key}
            className={`btn btn-sm flex-1 ${tab === t.key ? 'btn-secondary' : 'btn-ghost'}`}
            onClick={() => setTab(t.key)}
          >
            {t.icon} {t.label}
          </button>
        ))}
      </div>
      {tab === 'characters' && <CharactersPanel />}
      {tab === 'foreshadows' && <ForeshadowsPanel />}
      {tab === 'worldbuild' && <WorldbuildPanel />}
    </div>
  );
};

const CharactersPanel: React.FC = () => {
  const characters = useNovelStore((s) => s.characters);
  const generateCharacters = useNovelStore((s) => s.generateCharacters);
  const addCharacter = useNovelStore((s) => s.addCharacter);
  const removeCharacter = useNovelStore((s) => s.removeCharacter);
  const [basis, setBasis] = useState('');
  const [form, setForm] = useState({ name: '', role: '', summary: '' });

  return (
    <div className="flex flex-col gap-2">
      <div className="flex gap-1">
        <input
          className="input flex-1 text-xs"
          placeholder="给 AI 的补充要求（可空）"
          value={basis}
          onChange={(e) => setBasis(e.target.value)}
        />
        <button className="btn btn-secondary btn-sm" onClick={() => void generateCharacters(basis)}>
          <Sparkles size={12} aria-hidden="true" /> AI 设计
        </button>
      </div>
      <div className="flex flex-col gap-1 max-h-[46vh] overflow-auto">
        {characters.length === 0 && (
          <div className="text-white/40 text-xs">还没有角色卡。点「AI 设计」一键出 3~6 个主要角色。</div>
        )}
        {characters.map((c) => (
          <div key={c.id} className="rounded border border-white/10 p-2 text-xs group">
            <div className="flex items-center gap-1">
              <span className="font-medium">{c.name}</span>
              {c.role && <span className="text-white/40">（{c.role}）</span>}
              <span className="flex-1" />
              <button
                className="btn btn-ghost btn-sm opacity-0 group-hover:opacity-100 text-rose-300"
                onClick={() => void removeCharacter(c.id)}
              >
                <Trash2 size={11} aria-hidden="true" />
              </button>
            </div>
            <div className="text-white/50 mt-0.5">{c.summary}</div>
          </div>
        ))}
      </div>
      <div className="border-t border-white/10 pt-2 flex flex-col gap-1">
        <div className="flex gap-2">
          <input
            className="input flex-1 min-w-0 text-xs"
            placeholder="角色名"
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
          />
          <input
            className="shrink-0 w-20 rounded border border-white/15 bg-white/5 px-1 text-xs"
            placeholder="定位"
            value={form.role}
            onChange={(e) => setForm({ ...form, role: e.target.value })}
          />
        </div>
        <textarea
          className="input text-xs h-14 resize-none"
          placeholder="人设：外貌/性格/动机/口癖"
          value={form.summary}
          onChange={(e) => setForm({ ...form, summary: e.target.value })}
        />
        <button
          className="btn btn-ghost btn-sm"
          disabled={!form.name.trim()}
          onClick={async () => {
            await addCharacter(form.name, form.role, form.summary);
            setForm({ name: '', role: '', summary: '' });
          }}
        >
          <Plus size={12} aria-hidden="true" /> 手工添加角色
        </button>
      </div>
    </div>
  );
};

const ForeshadowsPanel: React.FC = () => {
  const foreshadows = useNovelStore((s) => s.foreshadows);
  const check = useNovelStore((s) => s.foreshadowCheck);
  const chapters = useNovelStore((s) => s.chapters);
  const addForeshadow = useNovelStore((s) => s.addForeshadow);
  const setForeshadowStatus = useNovelStore((s) => s.setForeshadowStatus);
  const removeForeshadow = useNovelStore((s) => s.removeForeshadow);
  const [desc, setDesc] = useState('');
  const [planted, setPlanted] = useState('');

  return (
    <div className="flex flex-col gap-2">
      {check && check.unrecalled.length > 0 && (
        <div className="text-xs text-amber-300 border border-amber-400/30 rounded p-2">
          有 {check.unrecalled.length} 个伏笔未回收
          {check.dangling_payoff.length > 0 && `，${check.dangling_payoff.length} 个回收指向已删章节`}——
          烂尾预警，记得在后续章节兑现。
        </div>
      )}
      <div className="flex flex-col gap-1 max-h-[46vh] overflow-auto">
        {foreshadows.length === 0 && (
          <div className="text-white/40 text-xs">
            伏笔账本是空的。写到这里埋了钩子就登记一条，AI 体检会提醒你别忘了回收。
          </div>
        )}
        {foreshadows.map((f) => (
          <div key={f.id} className="rounded border border-white/10 p-2 text-xs group">
            <div className="flex items-start gap-2">
              <span className="flex-1 min-w-0 break-words leading-relaxed">{f.description}</span>
              <select
                className="shrink-0 w-20 rounded border border-white/15 bg-white/5 px-1 py-0.5"
                value={f.status}
                onChange={(e) => void setForeshadowStatus(f.id, e.target.value as NovelForeshadow['status'])}
              >
                <option value="planted">埋设</option>
                <option value="payoff">已回收</option>
                <option value="dropped">弃用</option>
              </select>
              <button
                className="shrink-0 btn btn-ghost btn-sm opacity-0 group-hover:opacity-100 text-rose-300"
                onClick={() => void removeForeshadow(f.id)}
              >
                <Trash2 size={11} aria-hidden="true" />
              </button>
            </div>
          </div>
        ))}
      </div>
      <div className="border-t border-white/10 pt-2 flex flex-col gap-1">
        <textarea
          className="input text-xs h-14 resize-none"
          placeholder="伏笔内容：如「主角玉佩在雷夜会发烫」"
          value={desc}
          onChange={(e) => setDesc(e.target.value)}
        />
        <div className="flex gap-2">
          <select
            className="flex-1 min-w-0 rounded border border-white/15 bg-white/5 px-1 py-0.5 text-xs"
            value={planted}
            onChange={(e) => setPlanted(e.target.value)}
          >
            <option value="">埋设章节（可选）</option>
            {chapters.map((c) => (
              <option key={c.id} value={c.id}>
                第{c.chapter_index}章 {c.title}
              </option>
            ))}
          </select>
          <button
            className="shrink-0 btn btn-ghost btn-sm"
            disabled={!desc.trim()}
            onClick={async () => {
              await addForeshadow(desc, planted);
              setDesc('');
              setPlanted('');
            }}
          >
            <Plus size={12} aria-hidden="true" /> 登记
          </button>
        </div>
      </div>
    </div>
  );
};

const WorldbuildPanel: React.FC = () => {
  const importWorldbuild = useNovelStore((s) => s.importWorldbuild);
  const setNotice = useNovelStore((s) => s.setNotice);
  const setErr = useNovelStore((s) => s.setErr);
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);

  return (
    <div className="flex flex-col gap-2">
      <div className="text-xs text-white/40">
        把世界观设定（力量体系/地理/组织/历史）贴进下面，导入知识库。
        之后每章生成时，AI 会按本章内容自动检索相关设定注入——
        过滤、去重、质检全部走知识库管线。
      </div>
      <textarea
        className="input text-xs h-52 resize-none"
        placeholder={'例：\n修炼体系：炼气→筑基→金丹→元婴，每一境界分初中后期……\n地理：九州大陆，主角出生在北境雪州……'}
        value={text}
        onChange={(e) => setText(e.target.value)}
      />
      <button
        className="btn btn-primary btn-sm"
        disabled={busy || text.trim().length < 20}
        onClick={async () => {
          setBusy(true);
          try {
            const added = await importWorldbuild(text);
            setNotice(`世界观已入库：新增 ${added} 条（低质量片段已被质检闸过滤）`);
            setText('');
          } catch (e) {
            setErr((e as Error).message);
          } finally {
            setBusy(false);
          }
        }}
      >
        <Sparkles size={12} aria-hidden="true" /> 导入知识库
      </button>
    </div>
  );
};

export default NovelPage;
