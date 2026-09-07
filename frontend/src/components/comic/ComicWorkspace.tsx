/* ==========================================================================
 * OmniSpace AI v2.3.1 —— 漫画工作台（漫画模块 M1，2026-09-07）
 * --------------------------------------------------------------------------
 * 分格网格 + 角色引用栏。全部走漫剧模块既有端点（零新增生成链）：
 *   - 分格 = storyboard_rows（全量保存增删；单行 PUT 改画面描述）
 *   - 分格出图 = 关键帧链 generate/regenerate（comfy+PuLID 身份硬锁，
 *     角色资产含四视图 closeup 时自动触发 face_ref —— M0 验证结论）
 *   - 角色 = comic_assets（新建走 generate-turnaround：四视图一次到位，
 *     closeup 是 PuLID 人脸锚的判据源，不用单体立绘）
 *   - 画风 = 项目级 art_style，切换后重新生成分格即按新画风出图
 * ========================================================================== */

import { useEffect, useMemo, useRef, useState } from 'react';
import {
  ArrowLeft, BookOpenText, Download, Loader2, Plus, Sparkles, Trash2, Upload, UserPlus, Wand2, X,
} from 'lucide-react';
import { useAppStore } from '@/stores/useAppStore';
import * as mangaApi from '@/services/mangaApi';
import {
  ART_STYLES, type ComicAsset, type ComicProject, type KeyframeItem, type StoryboardRow,
} from '@/types';
import { getErrorMessage, reportBgError } from '@/utils/errors';
import { Modal } from '../common/Modal';

interface Props {
  project: ComicProject;
  onExit: () => void;
  onProjectUpdated: (patch: Partial<ComicProject>) => void;
}

/** 新建一条分格行（后端全量保存时缺省字段自动补齐，这里给全量默认值） */
function makePanelRow(n: number): StoryboardRow {
  return {
    id: (crypto.randomUUID?.() ?? `${Date.now().toString(16)}${Math.random().toString(16).slice(2)}`).replace(/-/g, ''),
    shot_number: n,
    original_dialogue: '',
    description: '',
    characters: [],
    scene: '',
    props: [],
    voice_id: '',
    voice_emotion: '默认',
    director_stage_done: false,
    generation_status: 'pending',
    is_ai_generated: false,
    asset_ids: [],
  };
}

export default function ComicWorkspace({ project, onExit, onProjectUpdated }: Props) {
  const showToast = useAppStore((s) => s.showToast);
  const pid = project.project_id;

  const [loading, setLoading] = useState(true);
  const [rows, setRows] = useState<StoryboardRow[]>([]);
  const [chars, setChars] = useState<ComicAsset[]>([]);
  const [kfByRow, setKfByRow] = useState<Record<string, KeyframeItem>>({});
  const [genRows, setGenRows] = useState<Set<string>>(new Set());
  const [promptDraft, setPromptDraft] = useState<Record<string, string>>({});
  const [dialogueDraft, setDialogueDraft] = useState<Record<string, string>>({});
  const [batchRunning, setBatchRunning] = useState(false);
  const [deleteRowId, setDeleteRowId] = useState<string | null>(null);
  const [rowBusy, setRowBusy] = useState(false);

  // 新建角色表单（四视图，1~3 分钟）
  const [charFormOpen, setCharFormOpen] = useState(false);
  const [charName, setCharName] = useState('');
  const [charPrompt, setCharPrompt] = useState('');
  const [charCreating, setCharCreating] = useState(false);

  // AI 写分格（C2 本地路，2026-09-08）：故事梗概 → N 格画面描述
  const [scriptFormOpen, setScriptFormOpen] = useState(false);
  const [scriptStory, setScriptStory] = useState('');
  const [scriptPanels, setScriptPanels] = useState(4);
  const [scriptReplace, setScriptReplace] = useState(false);
  const [scriptGenerating, setScriptGenerating] = useState(false);

  // 上传角色图（C3）：名称+本地图片 → /comic/asset/upload
  const [uploadFormOpen, setUploadFormOpen] = useState(false);
  const [uploadName, setUploadName] = useState('');
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [upgradingId, setUpgradingId] = useState<string | null>(null);

  // 整页导出（C4）：PNG 长图 / PDF 多页
  const [exportMenuOpen, setExportMenuOpen] = useState(false);
  const [exporting, setExporting] = useState<string | null>(null);

  // 阅读预览（完整显示 contain + 预览中重新生成）
  const [previewOpen, setPreviewOpen] = useState(false);
  // 气泡拖拽中行 id（拖拽中不落库，抬手才存）；「resize」=拉伸宽度模式
  const bubbleDragRef = useRef<string | null>(null);
  const bubbleResizeRef = useRef<string | null>(null);

  const loadAll = () => {
    setLoading(true);
    void (async () => {
      try {
        const [rs, assets] = await Promise.all([
          mangaApi.getStoryboardRows(pid),
          mangaApi.listAssets(pid, 'character'),
        ]);
        setRows(rs);
        setChars(assets);
        const drafts: Record<string, string> = {};
        const dialogues: Record<string, string> = {};
        const kfMap: Record<string, KeyframeItem> = {};
        await Promise.all(rs.map(async (r) => {
          drafts[r.id] = r.description ?? '';
          dialogues[r.id] = r.original_dialogue ?? '';
          try {
            const kfs = await mangaApi.listKeyframes(r.id);
            const cur = kfs.find((k) => k.is_current) ?? kfs[0];
            if (cur) kfMap[r.id] = cur;
          } catch (err) {
            reportBgError('comic.listKeyframes', err);
          }
        }));
        setPromptDraft(drafts);
        setDialogueDraft(dialogues);
        setKfByRow(kfMap);
        setLoading(false);
      } catch (err) {
        setLoading(false);
        showToast(getErrorMessage(err, '漫画数据加载失败'), 'error');
      }
    })();
  };

  useEffect(() => {
    loadAll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pid]);

  const charById = useMemo(
    () => new Map(chars.map((c) => [c.asset_id, c])),
    [chars],
  );

  const styleKey = project.art_style ?? '';

  /* ---------------- 分格操作 ---------------- */

  const addPanel = async () => {
    if (rowBusy) return;
    setRowBusy(true);
    try {
      const next = await mangaApi.saveStoryboardRows(pid, [...rows, makePanelRow(rows.length + 1)]);
      setRows(next);
      const last = next[next.length - 1];
      if (last) setPromptDraft((d) => ({ ...d, [last.id]: last.description ?? '' }));
    } catch (err) {
      showToast(getErrorMessage(err, '添加分格失败'), 'error');
    } finally {
      setRowBusy(false);
    }
  };

  const removePanel = async () => {
    if (!deleteRowId) return;
    const rowId = deleteRowId;
    setDeleteRowId(null);
    setRowBusy(true);
    try {
      const next = await mangaApi.saveStoryboardRows(pid, rows.filter((r) => r.id !== rowId));
      setRows(next);
      setKfByRow((m) => {
        const { [rowId]: _drop, ...rest } = m;
        return rest;
      });
      showToast('分格已删除', 'success');
    } catch (err) {
      showToast(getErrorMessage(err, '删除分格失败'), 'error');
    } finally {
      setRowBusy(false);
    }
  };

  const savePrompt = async (row: StoryboardRow) => {
    const text = promptDraft[row.id] ?? '';
    if (text === (row.description ?? '')) return;
    try {
      await mangaApi.updateStoryboardRow(pid, row.id, { description: text });
      setRows((rs) => rs.map((r) => (r.id === row.id ? { ...r, description: text } : r)));
    } catch (err) {
      showToast(getErrorMessage(err, '画面描述保存失败'), 'error');
    }
  };

  const saveDialogue = async (row: StoryboardRow) => {
    const text = dialogueDraft[row.id] ?? '';
    if (text === (row.original_dialogue ?? '')) return;
    try {
      await mangaApi.updateStoryboardRow(pid, row.id, { original_dialogue: text });
      setRows((rs) => rs.map((r) => (r.id === row.id ? { ...r, original_dialogue: text } : r)));
    } catch (err) {
      showToast(getErrorMessage(err, '台词保存失败'), 'error');
    }
  };

  const bindChar = async (assetId: string, rowId: string) => {
    try {
      const resp = await mangaApi.bindAsset(assetId, rowId);
      setRows((rs) => rs.map((r) => (r.id === rowId ? { ...r, asset_ids: resp.asset_ids } : r)));
    } catch (err) {
      showToast(getErrorMessage(err, '角色引用失败'), 'error');
    }
  };

  const unbindChar = async (assetId: string, rowId: string) => {
    try {
      const resp = await mangaApi.unbindAsset(assetId, rowId);
      setRows((rs) => rs.map((r) => (r.id === rowId ? { ...r, asset_ids: resp.asset_ids } : r)));
    } catch (err) {
      showToast(getErrorMessage(err, '取消引用失败'), 'error');
    }
  };

  const genPanel = async (row: StoryboardRow) => {
    if (genRows.has(row.id) || batchRunning) return;
    const desc = (promptDraft[row.id] ?? '').trim();
    if (!desc) {
      showToast('请先填写本格画面描述', 'warning');
      return;
    }
    setGenRows((s) => new Set(s).add(row.id));
    try {
      const kf = kfByRow[row.id]
        ? await mangaApi.regenerateKeyframe({ row_id: row.id, project_id: pid })
        : await mangaApi.generateKeyframe({ row_id: row.id, project_id: pid });
      setKfByRow((m) => ({ ...m, [row.id]: kf }));
    } catch (err) {
      showToast(getErrorMessage(err, '分格生成失败'), 'error');
    } finally {
      setGenRows((s) => {
        const n = new Set(s);
        n.delete(row.id);
        return n;
      });
    }
  };

  const batchGenerate = async () => {
    if (batchRunning) return;
    const pending = rows.filter((r) => !kfByRow[r.id]);
    if (pending.length === 0) {
      showToast('所有分格都已有画面；需要换风格请逐格点「重新生成」', 'info');
      return;
    }
    setBatchRunning(true);
    let ok = 0;
    let fail = 0;
    for (const r of pending) {
      setGenRows((s) => new Set(s).add(r.id));
      try {
        const kf = await mangaApi.generateKeyframe({ row_id: r.id, project_id: pid });
        setKfByRow((m) => ({ ...m, [r.id]: kf }));
        ok += 1;
      } catch {
        fail += 1;
      } finally {
        setGenRows((s) => {
          const n = new Set(s);
          n.delete(r.id);
          return n;
        });
      }
    }
    setBatchRunning(false);
    showToast(
      fail ? `批量完成：成功 ${ok} 格、失败 ${fail} 格（可单格重试）` : `批量完成：${ok} 格全部生成`,
      fail ? 'warning' : 'success',
    );
  };

  /* ---------------- 角色操作 ---------------- */

  const createChar = async () => {
    const name = charName.trim();
    const prompt = charPrompt.trim();
    if (!name || !prompt) {
      showToast('角色名称与形象描述都要填写', 'warning');
      return;
    }
    setCharCreating(true);
    try {
      await mangaApi.generateTurnaround({ project_id: pid, name, prompt });
      const assets = await mangaApi.listAssets(pid, 'character');
      setChars(assets);
      setCharCreating(false);
      setCharFormOpen(false);
      setCharName('');
      setCharPrompt('');
      showToast(`角色「${name}」已生成（四视图，跨格一致性锚已就位）`, 'success');
    } catch (err) {
      setCharCreating(false);
      showToast(getErrorMessage(err, '角色生成失败'), 'error');
    }
  };

  /* ---------------- AI 写分格 ---------------- */

  const generateScript = async () => {
    const story = scriptStory.trim();
    if (!story) {
      showToast('请先填写故事梗概', 'warning');
      return;
    }
    setScriptGenerating(true);
    try {
      const res = await mangaApi.generateComicScript(pid, story, scriptPanels, scriptReplace);
      setRows(res.rows);
      const drafts: Record<string, string> = { ...promptDraft };
      for (const r of res.rows) drafts[r.id] = r.description ?? '';
      setPromptDraft(drafts);
      setScriptGenerating(false);
      setScriptFormOpen(false);
      setScriptStory('');
      showToast(`AI 已写好 ${res.generated} 格画面描述（可逐格修改后生成）`, 'success');
    } catch (err) {
      setScriptGenerating(false);
      showToast(getErrorMessage(err, 'AI 写分格失败'), 'error');
    }
  };

  /* ---------------- 上传角色（C3）与四视图升级 ---------------- */

  const refreshChars = async () => {
    try {
      setChars(await mangaApi.listAssets(pid, 'character'));
    } catch {
      /* 刷新失败静默，下次加载恢复 */
    }
  };

  const uploadChar = async () => {
    if (!uploadFile) {
      showToast('请选择角色图片（png/jpg/webp，≤10MB）', 'warning');
      return;
    }
    setUploading(true);
    try {
      await mangaApi.uploadCharacterAsset(pid, uploadName.trim() || '未命名角色', uploadFile);
      await refreshChars();
      setUploading(false);
      setUploadFormOpen(false);
      setUploadName('');
      setUploadFile(null);
      showToast('角色已上传（描述词将由 AI 按图自动补写）', 'success');
    } catch (err) {
      setUploading(false);
      showToast(getErrorMessage(err, '角色上传失败'), 'error');
    }
  };

  const upgradeFourViews = async (assetId: string, name: string) => {
    if (upgradingId) return;
    setUpgradingId(assetId);
    try {
      await mangaApi.regenerateAsset(assetId, { mode: 'four_views' });
      await refreshChars();
      setUpgradingId(null);
      showToast(`「${name}」四视图已生成，人脸锁已就位`, 'success');
    } catch (err) {
      setUpgradingId(null);
      showToast(getErrorMessage(err, `「${name}」四视图升级失败`), 'error');
    }
  };

  /* ---------------- 整页导出（C4） ---------------- */

  const exportPage = async (format: 'png' | 'pdf', layout: 'page' | 'grid2' = 'page') => {
    if (exporting) return;
    setExportMenuOpen(false);
    setExporting(format === 'pdf' ? (layout === 'grid2' ? 'PDF·2×2' : 'PDF') : 'PNG');
    try {
      const res = await mangaApi.exportComicPage(pid, format, true, layout);
      setExporting(null);
      // 浏览器直下（媒体白名单目录经 /manga/media 回读）
      const a = document.createElement('a');
      a.href = mangaApi.getMediaUrl(res.file_path);
      a.download = `comic_${pid.slice(0, 8)}.${format}`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      showToast(
        `已导出 ${res.pages} ${format === 'png' ? '格拼接' : '页'}`
        + (layout === 'grid2' ? '（每页 2×2 格）' : '')
        + (res.skipped_shots.length ? `，跳过 ${res.skipped_shots.length} 格未生成画面` : '')
        + (!res.baked_bubbles ? '（未找到中文字体，气泡未烘焙）' : ''),
        res.skipped_shots.length ? 'warning' : 'success',
      );
    } catch (err) {
      setExporting(null);
      showToast(getErrorMessage(err, '导出失败'), 'error');
    }
  };

  /* ---------------- 气泡拖拽定位（C4 尾巴） ---------------- */

  const bubblePos = (row: StoryboardRow): { x: number; y: number } => ({
    x: row.bubble_x ?? 0.05,
    y: row.bubble_y ?? 0.06,
  });

  const onBubblePointerDown = (e: React.PointerEvent, rowId: string) => {
    e.preventDefault();
    try {
      (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    } catch {
      // 指针已失活/合成事件时无法捕获——退化为全局 move 监听语义，
      // 拖拽仍由 pointermove/up 驱动（capture 只是防丢事件的增强）
    }
    bubbleDragRef.current = rowId;
  };

  const onBubblePointerMove = (e: React.PointerEvent) => {
    const bubble = e.currentTarget as HTMLElement;
    const host = bubble.parentElement;
    if (!host) return;
    const rect = host.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return;
    const resizeId = bubbleResizeRef.current;
    if (resizeId) {
      // 拉伸模式：宽度=指针横坐标相对格宽，左不越过气泡起点、右不出格
      const row = rows.find((r) => r.id === resizeId);
      if (!row) return;
      const x = row.bubble_x ?? 0.05;
      const w = Math.min(
        Math.max(0.15, (e.clientX - rect.left) / rect.width - x),
        1 - x - 0.02);
      setRows((rs) => rs.map((r) => (r.id === resizeId ? { ...r, bubble_w: w } : r)));
      return;
    }
    const rowId = bubbleDragRef.current;
    if (!rowId) return;
    // 按气泡实际宽高钳制，保证整个气泡留在格内（拖到右/下缘不溢出）
    const bwRatio = bubble.offsetWidth / rect.width;
    const bhRatio = bubble.offsetHeight / rect.height;
    const x = Math.min(Math.max(0.02, (e.clientX - rect.left) / rect.width),
                       Math.max(0.04, 0.98 - bwRatio));
    const y = Math.min(Math.max(0.02, (e.clientY - rect.top) / rect.height),
                       Math.max(0.04, 0.98 - bhRatio));
    setRows((rs) => rs.map((r) => (
      r.id === rowId ? { ...r, bubble_x: x, bubble_y: y } : r)));
  };

  const onBubblePointerUp = () => {
    const rowId = bubbleDragRef.current ?? bubbleResizeRef.current;
    bubbleDragRef.current = null;
    bubbleResizeRef.current = null;
    if (!rowId) return;
    const row = rows.find((r) => r.id === rowId);
    if (!row) return;
    const patch: { bubble_x?: number; bubble_y?: number; bubble_w?: number } = {};
    if (row.bubble_x != null && row.bubble_y != null) {
      patch.bubble_x = row.bubble_x;
      patch.bubble_y = row.bubble_y;
    }
    if (row.bubble_w != null) patch.bubble_w = row.bubble_w;
    if (!Object.keys(patch).length) return;
    mangaApi.updateStoryboardRow(pid, rowId, patch)
      .catch((err) => showToast(getErrorMessage(err, '气泡位置保存失败'), 'error'));
  };

  /* ---- 气泡拉伸（右下把手改宽度，高度随文字折行自适应） ---- */

  const onResizePointerDown = (e: React.PointerEvent, rowId: string) => {
    e.stopPropagation();  // 不触发气泡移动拖拽
    e.preventDefault();
    try {
      (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    } catch {
      // 合成/失活指针无法捕获——move/up 经气泡本体的共享通道驱动
    }
    bubbleResizeRef.current = rowId;
  };

  /* ---------------- 画风切换 ---------------- */

  const changeStyle = async (key: string) => {
    if (key === styleKey) return;
    try {
      await mangaApi.renameProject(pid, project.name, key);
      onProjectUpdated({ art_style: key });
      showToast('画风已切换；点「重新生成」即按新画风出图', 'success');
    } catch (err) {
      showToast(getErrorMessage(err, '画风切换失败'), 'error');
    }
  };

  /* ---------------- 渲染 ---------------- */

  const pendingCount = rows.filter((r) => !kfByRow[r.id]).length;

  return (
    <div className="page comic-workspace">
      <header className="comic-ws-header">
        <div className="comic-ws-title">
          <button type="button" className="btn btn-ghost btn-sm" onClick={onExit} title="返回漫画库">
            <ArrowLeft size={16} />
          </button>
          <BookOpenText size={18} />
          <span className="comic-ws-name" title={project.name}>{project.name}</span>
          <select
            className="input comic-ws-style"
            value={styleKey}
            onChange={(e) => void changeStyle(e.target.value)}
            title="项目级画风：切换后重新生成分格即生效"
          >
            <option value="">未选择画风</option>
            {ART_STYLES.map((s) => (
              <option key={s.key} value={s.key}>{s.label}</option>
            ))}
          </select>
        </div>
        <div className="comic-ws-ops">
          <button type="button" className="btn" onClick={() => setScriptFormOpen(true)}>
            <Sparkles size={15} /> AI 写分格
          </button>
          <button type="button" className="btn" onClick={() => setCharFormOpen(true)}>
            <UserPlus size={15} /> 新建角色
          </button>
          <div className="comic-export-wrap">
            <button
              type="button"
              className="btn"
              onClick={() => setPreviewOpen(true)}
              disabled={rows.length === 0}
              title="阅读预览：完整画面连续浏览，可逐格重新生成"
            >
              <BookOpenText size={15} /> 预览
            </button>
            <button
              type="button"
              className="btn"
              onClick={() => setExportMenuOpen((o) => !o)}
              disabled={exporting !== null || rows.length === 0}
              title="把已生成分格导出成成品"
            >
              <Download size={15} />
              {exporting ? `导出中（${exporting.toUpperCase()}）…` : '导出成册'}
            </button>
            {exportMenuOpen && (
              <div className="comic-export-menu" role="menu">
                <button type="button" role="menuitem" onClick={() => void exportPage('png', 'page')}>
                  PNG 长图（单列拼接）
                </button>
                <button type="button" role="menuitem" onClick={() => void exportPage('pdf', 'page')}>
                  PDF · 每格一页
                </button>
                <button type="button" role="menuitem" onClick={() => void exportPage('pdf', 'grid2')}>
                  PDF · 每页 2×2 格
                </button>
                <p className="text-secondary">台词气泡一并烘入</p>
              </div>
            )}
          </div>
          <button type="button" className="btn" onClick={() => void addPanel()} disabled={rowBusy}>
            <Plus size={15} /> 添加分格
          </button>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => void batchGenerate()}
            disabled={batchRunning || rowBusy || rows.length === 0}
          >
            {batchRunning ? <Loader2 size={15} className="spin" /> : <Sparkles size={15} />}
            {batchRunning ? '生成中…' : `生成未完成分格${pendingCount > 0 ? `（${pendingCount}）` : ''}`}
          </button>
        </div>
      </header>

      {loading ? (
        <div className="loading-block"><div className="spinner lg" /><div>漫画加载中…</div></div>
      ) : (
        <div className="comic-ws-body">
          {/* 分格网格 */}
          <div className="comic-panel-grid">
            {rows.map((row, idx) => {
              const kf = kfByRow[row.id];
              const generating = genRows.has(row.id);
              const boundIds = row.asset_ids ?? [];
              return (
                <div key={row.id} className="comic-panel card">
                  <div className="comic-panel-img">
                    {kf ? (
                      <img src={mangaApi.getMediaUrl(kf.file_path, kf.version)} alt={`第${idx + 1}格`} />
                    ) : (
                      <div className="comic-panel-placeholder">
                        <Wand2 size={26} style={{ opacity: 0.35 }} />
                        <span>{(promptDraft[row.id] ?? '').trim() ? '待生成' : '填写描述后生成'}</span>
                      </div>
                    )}
                    <span className="comic-panel-badge">第 {idx + 1} 格{kf ? ` · v${kf.version}` : ''}</span>
                    {(dialogueDraft[row.id] ?? '').trim() && (
                      <div
                        className="comic-bubble"
                        style={{
                          left: `${bubblePos(row).x * 100}%`,
                          top: `${bubblePos(row).y * 100}%`,
                          ...(row.bubble_w != null
                            ? { width: `${Math.round(row.bubble_w * 100)}%` }
                            : null),
                        }}
                        title="拖动移动位置；右下角把手拉伸宽度"
                        onPointerDown={(e) => onBubblePointerDown(e, row.id)}
                        onPointerMove={onBubblePointerMove}
                        onPointerUp={onBubblePointerUp}
                      >
                        {(dialogueDraft[row.id] ?? '').trim()}
                        <span
                          className="comic-bubble-resize"
                          aria-label="拉伸气泡宽度"
                          title="拖动拉伸宽度"
                          onPointerDown={(e) => onResizePointerDown(e, row.id)}
                        />
                      </div>
                    )}
                    {generating && (
                      <div className="comic-panel-gen">
                        <Loader2 size={22} className="spin" />
                        <span>生成中…（约 1~2 分钟）</span>
                      </div>
                    )}
                  </div>
                  <div className="comic-panel-body">
                    <textarea
                      className="input comic-panel-prompt"
                      rows={3}
                      maxLength={2000}
                      placeholder="本格画面描述（写清人物动作/场景/情绪；英文更稳）"
                      value={promptDraft[row.id] ?? ''}
                      onChange={(e) => setPromptDraft((d) => ({ ...d, [row.id]: e.target.value }))}
                      onBlur={() => void savePrompt(row)}
                    />
                    <input
                      className="input comic-panel-dialogue"
                      maxLength={200}
                      placeholder="台词 / 旁白（显示为气泡，导出时烘入）"
                      value={dialogueDraft[row.id] ?? ''}
                      onChange={(e) => setDialogueDraft((d) => ({ ...d, [row.id]: e.target.value }))}
                      onBlur={() => void saveDialogue(row)}
                    />
                    <div className="comic-panel-chars">
                      {boundIds.map((aid) => {
                        const c = charById.get(aid);
                        return (
                          <span key={aid} className="comic-chip" title={c?.prompt ?? aid}>
                            {c?.name ?? '未知资产'}
                            <button
                              type="button"
                              aria-label="取消引用"
                              onClick={() => void unbindChar(aid, row.id)}
                            >
                              <X size={11} />
                            </button>
                          </span>
                        );
                      })}
                      {chars.length > 0 && (
                        <select
                          className="comic-chip-add"
                          value=""
                          onChange={(e) => {
                            if (e.target.value) void bindChar(e.target.value, row.id);
                          }}
                          title="引用角色（生成时作一致性锚）"
                        >
                          <option value="">＋ 引用角色</option>
                          {chars
                            .filter((c) => !boundIds.includes(c.asset_id))
                            .map((c) => (
                              <option key={c.asset_id} value={c.asset_id}>{c.name}</option>
                            ))}
                        </select>
                      )}
                    </div>
                    <div className="comic-panel-ops">
                      <button
                        type="button"
                        className="btn btn-primary btn-sm"
                        onClick={() => void genPanel(row)}
                        disabled={generating || batchRunning}
                      >
                        {generating || batchRunning ? <Loader2 size={14} className="spin" /> : <Wand2 size={14} />}
                        {kf ? '重新生成' : '生成画面'}
                      </button>
                      <button
                        type="button"
                        className="btn btn-ghost btn-sm"
                        title="删除本格（含已生成画面）"
                        onClick={() => setDeleteRowId(row.id)}
                      >
                        <Trash2 size={14} />
                      </button>
                    </div>
                  </div>
                </div>
              );
            })}
            <button
              type="button"
              className="comic-panel comic-panel-add"
              onClick={() => void addPanel()}
              disabled={rowBusy}
            >
              <Plus size={24} />
              <span>添加分格</span>
            </button>
          </div>

          {/* 角色栏 */}
          <aside className="comic-rail">
            <div className="comic-rail-title">
              <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                角色（{chars.length}）
                <button
                  type="button"
                  className="comic-chip-add"
                  onClick={() => setUploadFormOpen(true)}
                  title="上传本地图片作为角色（手画/外部图均可）"
                >
                  上传角色图
                </button>
              </span>
              <span className="text-secondary" style={{ fontSize: 11 }}>
                {chars.length === 0 ? '新建或上传角色后，在分格里引用' : '在分格里引用作一致性锚'}
              </span>
            </div>
            {chars.map((c) => {
              const hasTurnaround = Boolean(
                (c.meta as { turnaround?: unknown } | null)?.turnaround);
              return (
                <div key={c.asset_id} className="comic-char-card">
                  <img src={mangaApi.getMediaUrl(c.file_path, c.created_at)} alt={c.name} />
                  <div className="comic-char-meta">
                    <div className="comic-char-name" title={c.name}>{c.name}</div>
                    <div className="comic-char-prompt" title={c.prompt}>{c.prompt}</div>
                    {!hasTurnaround && (
                      <button
                        type="button"
                        className="comic-char-upgrade"
                        disabled={upgradingId !== null}
                        title="生成本角色四视图（正面/侧面/背面/特写）——特写是跨格人脸锁的锚点"
                        onClick={() => void upgradeFourViews(c.asset_id, c.name)}
                      >
                        {upgradingId === c.asset_id ? '四视图生成中…（约1~3分钟）' : '升级四视图（人脸锁）'}
                      </button>
                    )}
                  </div>
                </div>
              );
            })}
            {chars.length === 0 && (
              <>
                <button type="button" className="comic-char-empty" onClick={() => setCharFormOpen(true)}>
                  <UserPlus size={20} />
                  <span>AI 新建第一个角色</span>
                </button>
                <button type="button" className="comic-char-empty" onClick={() => setUploadFormOpen(true)}>
                  <Upload size={20} />
                  <span>上传本地角色图</span>
                </button>
              </>
            )}
          </aside>
        </div>
      )}

      {/* 阅读预览（完整显示 contain + 预览中重新生成，C4 用户需求 2026-09-08） */}
      {previewOpen && (
        <div
          className="comic-preview"
          role="dialog"
          aria-label="漫画阅读预览"
          onClick={(e) => {
            if (e.target === e.currentTarget) setPreviewOpen(false);
          }}
        >
          <button
            type="button"
            className="comic-preview-close"
            aria-label="关闭预览"
            onClick={() => setPreviewOpen(false)}
          >
            <X size={20} />
          </button>
          <div className="comic-preview-body">
            {rows.map((row, idx) => {
              const kf = kfByRow[row.id];
              const generating = genRows.has(row.id);
              const pos = bubblePos(row);
              return (
                <div className="comic-preview-panel" key={row.id}>
                  <div className="comic-preview-imgwrap">
                    {kf ? (
                      <img
                        src={mangaApi.getMediaUrl(kf.file_path, kf.version)}
                        alt={`第${idx + 1}格`}
                      />
                    ) : (
                      <div className="comic-panel-placeholder">
                        <Wand2 size={26} style={{ opacity: 0.35 }} />
                        <span>{(row.description ?? '').trim() ? '待生成' : '填写描述后生成'}</span>
                      </div>
                    )}
                    {(row.original_dialogue ?? '').trim() && (
                      <div
                        className="comic-bubble"
                        style={{
                          left: `${pos.x * 100}%`,
                          top: `${pos.y * 100}%`,
                          ...(row.bubble_w != null
                            ? { width: `${Math.round(row.bubble_w * 100)}%` }
                            : null),
                        }}
                      >
                        {(row.original_dialogue ?? '').trim()}
                      </div>
                    )}
                    {generating && (
                      <div className="comic-panel-gen">
                        <Loader2 size={22} className="spin" />
                        <span>生成中…（约 1~2 分钟）</span>
                      </div>
                    )}
                  </div>
                  <div className="comic-preview-ops">
                    <span className="comic-preview-no">第 {idx + 1} 格{kf ? ` · v${kf.version}` : ''}</span>
                    <button
                      type="button"
                      className="btn btn-primary btn-sm"
                      onClick={() => void genPanel(row)}
                      disabled={generating || batchRunning}
                    >
                      {generating || batchRunning ? <Loader2 size={14} className="spin" /> : <Wand2 size={14} />}
                      {kf ? '重新生成' : '生成画面'}
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* 上传角色图弹窗（C3：本地图片登记为角色资产；描述词后台 AI 补写） */}
      {uploadFormOpen && (
        <Modal title="上传角色图" onClose={() => !uploading && setUploadFormOpen(false)} width={460}>
          <div className="flex flex-col gap-3">
            <div>
              <div className="text-secondary mb-2" style={{ fontSize: 'var(--font-size-xs)' }}>角色名称</div>
              <input
                className="input"
                value={uploadName}
                maxLength={100}
                placeholder="如：手绘·阿澄"
                onChange={(e) => setUploadName(e.target.value)}
              />
            </div>
            <div>
              <div className="text-secondary mb-2" style={{ fontSize: 'var(--font-size-xs)' }}>角色图片</div>
              <input
                className="input"
                type="file"
                accept="image/png,image/jpeg,image/webp"
                onChange={(e) => setUploadFile(e.target.files?.[0] ?? null)}
              />
            </div>
            <p className="text-secondary" style={{ fontSize: 11 }}>
              支持 png/jpg/webp（≤10MB）。上传后可直接在分格里引用作参考锚；
              点角色卡上「升级四视图」可进一步启用跨格人脸锁（PuLID）。
              角色描述词会由 AI 按图自动补写。
            </p>
            <div className="flex justify-end gap-2">
              <button type="button" className="btn" onClick={() => setUploadFormOpen(false)} disabled={uploading}>
                取消
              </button>
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => void uploadChar()}
                disabled={uploading || !uploadFile}
              >
                {uploading ? <Loader2 size={14} className="spin" /> : <Upload size={14} />}
                {uploading ? '上传中…' : '上传并登记'}
              </button>
            </div>
          </div>
        </Modal>
      )}

      {/* AI 写分格弹窗（本地对话引擎，首次含模型装载约 1~3 分钟） */}
      {scriptFormOpen && (
        <Modal title="AI 写分格" onClose={() => !scriptGenerating && setScriptFormOpen(false)} width={520}>
          <div className="flex flex-col gap-3">
            <div>
              <div className="text-secondary mb-2" style={{ fontSize: 'var(--font-size-xs)' }}>故事梗概</div>
              <textarea
                className="input"
                rows={5}
                maxLength={2000}
                placeholder="如：高中生小漫在旧书店发现一本会发光的日记，翻开后天色骤变，她被卷入了一场跨越百年的约定……"
                value={scriptStory}
                onChange={(e) => setScriptStory(e.target.value)}
              />
            </div>
            <div className="flex items-center gap-3 flex-wrap">
              <div className="text-secondary" style={{ fontSize: 'var(--font-size-xs)' }}>分格数量</div>
              <select
                className="input comic-ws-style"
                value={scriptPanels}
                onChange={(e) => setScriptPanels(Number(e.target.value))}
              >
                {[2, 4, 6, 8, 12].map((n) => (
                  <option key={n} value={n}>{n} 格</option>
                ))}
              </select>
              <label className="flex items-center gap-1" style={{ fontSize: 'var(--font-size-xs)', cursor: 'pointer' }}>
                <input
                  type="checkbox"
                  checked={scriptReplace}
                  onChange={(e) => setScriptReplace(e.target.checked)}
                  disabled={rows.length === 0}
                />
                <span className="text-secondary">清空现有分格后填充{rows.length === 0 ? '（当前无分格）' : `（现有 ${rows.length} 格）`}</span>
              </label>
            </div>
            <p className="text-secondary" style={{ fontSize: 11 }}>
              由本地对话模型把梗概拆成逐格画面描述（起承转合节奏），填好后可逐格修改再生成画面。
              首次使用含模型装载约 1~3 分钟。
            </p>
            <div className="flex justify-end gap-2">
              <button
                type="button"
                className="btn"
                onClick={() => setScriptFormOpen(false)}
                disabled={scriptGenerating}
              >
                取消
              </button>
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => void generateScript()}
                disabled={scriptGenerating || !scriptStory.trim()}
              >
                {scriptGenerating ? <Loader2 size={14} className="spin" /> : <Sparkles size={14} />}
                {scriptGenerating ? '分镜师创作中…' : '生成分格'}
              </button>
            </div>
          </div>
        </Modal>
      )}

      {/* 新建角色弹窗（四视图：front/side/back/closeup 一次到位，是 PuLID 人脸锚判据源） */}
      {charFormOpen && (
        <Modal title="新建角色" onClose={() => !charCreating && setCharFormOpen(false)} width={480}>
          <div className="flex flex-col gap-3">
            <div>
              <div className="text-secondary mb-2" style={{ fontSize: 'var(--font-size-xs)' }}>角色名称</div>
              <input
                className="input"
                value={charName}
                maxLength={30}
                placeholder="如：小满"
                onChange={(e) => setCharName(e.target.value)}
              />
            </div>
            <div>
              <div className="text-secondary mb-2" style={{ fontSize: 'var(--font-size-xs)' }}>
                形象描述（发型/发色/服饰/五官特征写具体，跨格一致性更稳）
              </div>
              <textarea
                className="input"
                rows={4}
                maxLength={2000}
                placeholder="如：短发黑发少女，红色发卡，黄色连帽衫，背双肩包"
                value={charPrompt}
                onChange={(e) => setCharPrompt(e.target.value)}
              />
            </div>
            <p className="text-secondary" style={{ fontSize: 11 }}>
              将生成四视图（正面/侧面/背面/特写，约 1~3 分钟）。特写视图是跨格人脸一致性
              （PuLID 身份锁）的锚点——分格生成时引用本角色即自动生效。
            </p>
            <div className="flex justify-end gap-2">
              <button type="button" className="btn" onClick={() => setCharFormOpen(false)} disabled={charCreating}>
                取消
              </button>
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => void createChar()}
                disabled={charCreating || !charName.trim() || !charPrompt.trim()}
              >
                {charCreating ? <Loader2 size={14} className="spin" /> : <UserPlus size={14} />}
                {charCreating ? '生成四视图中…' : '生成角色'}
              </button>
            </div>
          </div>
        </Modal>
      )}

      {/* 删除分格确认 */}
      {deleteRowId && (
        <Modal title="删除分格" onClose={() => setDeleteRowId(null)} width={400}>
          <div className="flex flex-col gap-3">
            <p>确定删除该分格？其已生成画面将一并清理，操作不可撤销。</p>
            <div className="flex justify-end gap-2">
              <button type="button" className="btn" onClick={() => setDeleteRowId(null)}>取消</button>
              <button type="button" className="btn btn-danger" onClick={() => void removePanel()}>
                确认删除
              </button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}
