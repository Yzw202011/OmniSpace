/* ==========================================================================
 * OmniSpace AI v2.3.1 —— 剧本导入整页（参考 omnipspace-demo ScriptImport）
 * --------------------------------------------------------------------------
 * 触发时机：打开项目且分镜表为空（0 行）时，代替工作台整页展示。
 * 布局：返回 + 大标题/副标题 + 单卡片（字数统计 + 大文本域 + 操作行）
 * 三种真实导入路径（必须完成其一才能进入分镜编辑，2026-08-23 用户裁定
 * 移除「跳过」：空分镜表手动逐行编辑体验差，剧本导入是流程第一环）：
 *   1. AI 自动分镜：预览 → 确认 两步流程（POST auto-split，确定性按行切分）
 *   2. 直接导入：POST /manga/storyboard/import（按行追加）
 *   3. DSL 文件上传：POST /comic/script/import-dsl（.txt/.dsl ≤10MB）
 * 完成后 rows>0 自动进入工作台；左上角返回 = 关闭项目回作品库。
 * ========================================================================== */

import { useCallback, useRef, useState } from 'react';
import { ChevronLeft, FileUp, Wand2 } from 'lucide-react';
import { useAppStore } from '@/stores/useAppStore';
import { useMangaStore } from '@/stores/useMangaStore';
import * as mangaApi from '@/services/mangaApi';
import { getErrorMessage as errMsg, reportBgError } from '@/utils/errors';
import type { StoryboardRow } from '@/types';
import { SplitProgressBar } from './SplitProgressBar';

/** DSL 语法示例（与后端 import-dsl 的 shot: 分镜标记口径一致） */
const DSL_EXAMPLE = `shot: 清晨的教室，樱花瓣沿窗飘落，空镜
shot: 主角推门而入，逆光剪影，脚步停顿
shot: 特写：课桌上的信封，手指微颤拿起`;

/** 分镜表行数硬上限（后端 STORYBOARD_MAX_ROWS，2026-08-23 50 → 200 配套 10000 字剧本） */
const MAX_ROWS = 200;

/** 剧本字数建议上限（超限警告，2026-08-23 用户裁定 3000 → 10000） */
const SCRIPT_WARN_CHARS = 10000;

interface ScriptImportProps {
  /** 完成导入进入工作台 */
  onDone: () => void;
}

export function ScriptImport({ onDone }: ScriptImportProps) {
  const showToast = useAppStore((s) => s.showToast);
  const currentProject = useMangaStore((s) => s.currentProject);
  const rows = useMangaStore((s) => s.rows);
  const importScript = useMangaStore((s) => s.importScript);
  const autoSplitPreview = useMangaStore((s) => s.autoSplitPreview);
  const autoSplitCommit = useMangaStore((s) => s.autoSplitCommit);
  const fetchRows = useMangaStore((s) => s.fetchRows);
  const splitting = useMangaStore((s) => s.splitting);

  const [script, setScript] = useState('');
  const [importing, setImporting] = useState(false);
  /** AI 镜头级分镜预览结果（dry_run，2026-08-23 真分镜改造） */
  const [splitPreview, setSplitPreview] = useState<{
    splitId: string;
    rows: StoryboardRow[];
    engine: string;
    truncated: boolean;
  } | null>(null);
  /** 切分发起时刻（SplitProgressBar 渐近进度基准） */
  const [splitStartedAt, setSplitStartedAt] = useState(0);
  /** DSL 上传 */
  const [uploading, setUploading] = useState(false);
  const [strict, setStrict] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  // 直接导入（按行追加）
  const handleImport = useCallback(() => {
    const text = script.trim();
    if (!text) {
      showToast('请先粘贴剧本文本', 'warning');
      return;
    }
    setImporting(true);
    importScript(text)
      .then((added) => {
        showToast(`剧本导入完成，新增 ${added} 行分镜`, 'success');
        onDone();
      })
      .catch((err) => showToast(errMsg(err, '剧本导入失败'), 'error'))
      .finally(() => setImporting(false));
     
  }, [script, importScript, onDone, showToast]);

  // AI 镜头级分镜第一步：dry-run 预览（后端真实推理，场景转换/正反打/
  // 关键动作各成独立镜头，输出景别+时长+画面描述+台词；长剧本自动分块）
  const handleSplitPreview = useCallback(() => {
    const text = script.trim();
    if (!text) {
      showToast('请先粘贴剧本文本', 'warning');
      return;
    }
    setSplitStartedAt(Date.now());
    autoSplitPreview(text)
      .then((res) => {
        if (res.engine === 'fallback') {
          showToast('AI 模型不可用，已降级为按行切分', 'warning');
        } else if (res.engine === 'ai-partial') {
          showToast('部分片段切分降级为按行（模型繁忙），可重新切分', 'warning');
        }
        if (res.truncated) {
          showToast(`分镜上限 ${MAX_ROWS} 行，已截断保留前 ${res.count} 镜`, 'warning');
        }
        setSplitPreview(res);
      })
      .catch((err) => showToast(errMsg(err, 'AI 分镜预览失败'), 'error'));
  }, [script, autoSplitPreview, showToast]);

  // AI 自动分镜第二步：确认提交（复用预览 split_id，免二次推理）
  const handleAutoSplit = useCallback(() => {
    if (!splitPreview) return;
    autoSplitCommit(splitPreview.splitId)
      .then((added) => {
        showToast(`AI 分镜完成，新增 ${added} 镜`, 'success');
        onDone();
      })
      .catch((err) => showToast(errMsg(err, 'AI 分镜确认失败'), 'error'));
  }, [splitPreview, autoSplitCommit, onDone, showToast]);

  // DSL 文件上传导入
  const handleUpload = useCallback(
    (file: File) => {
      if (!currentProject) return;
      setUploading(true);
      mangaApi
        .importDslFile(currentProject.id, file, strict)
        .then(async (res) => {
          showToast(`DSL 导入完成，新增 ${res.added} 行`, 'success');
          await fetchRows().catch((err) => reportBgError('ScriptImport.fetchRows', err));
          onDone();
        })
        .catch((err) => showToast(errMsg(err, 'DSL 导入失败'), 'error'))
        .finally(() => {
          setUploading(false);
          if (fileRef.current) fileRef.current.value = '';
        });
    },
     
    [currentProject, strict, fetchRows, onDone, showToast],
  );

  const closeProject = useMangaStore((s) => s.closeProject);

  return (
    <div className="manga-picker">
      {/* 页头（返回 = 关闭项目回作品库；不导入不进编辑器） */}
      <div className="flex items-center gap-3 mb-6">
        <button type="button" className="btn-icon" onClick={closeProject} aria-label="返回作品库" title="返回作品库">
          <ChevronLeft size={18} />
        </button>
        <div>
          <h1 style={{ margin: 0, fontSize: 'var(--font-size-xl)', fontWeight: 'var(--font-weight-bold)' }}>
            剧本录入 · {currentProject?.name ?? ''}
          </h1>
          <p className="text-[var(--color-text-secondary)]" style={{ margin: '4px 0 0', fontSize: 'var(--font-size-sm)' }}>
            粘贴或上传剧本完成导入后进入下一环节（上限 {MAX_ROWS} 镜）；剧本是分镜创作的第一步，不可跳过
          </p>
        </div>
      </div>

      {/* 主卡片 */}
      <div className="card" style={{ padding: 'var(--space-5)' }}>
        <div className="flex items-center justify-between mb-3">
          <span className="text-[var(--color-text-secondary)]" style={{ fontSize: 'var(--font-size-sm)' }}>
            {script.length} 字
          </span>
          <span className="badge info">AI 镜头级切分 · 支持万字剧本</span>
        </div>

        {splitPreview === null ? (
          <>
            <textarea
              className="input"
              rows={14}
              style={{ resize: 'vertical', width: '100%' }}
              value={script}
              onChange={(e) => setScript(e.target.value)}
              placeholder={'第一幕：深夜，便利店的灯光在雨中晕开。林晚推开门，风铃轻响……\n每行一个分镜场景'}
              aria-label="剧本文本"
              disabled={importing || splitting}
            />
            {script.length > SCRIPT_WARN_CHARS && (
              <span className="badge warning mt-2">
                剧本 {script.length} 字，超过 {SCRIPT_WARN_CHARS} 字建议拆分为多集以保证分镜质量
              </span>
            )}
            <div className="flex mt-4 gap-2" style={{ justifyContent: 'flex-end' }}>
              <button
                type="button"
                className="btn btn-secondary"
                onClick={handleImport}
                disabled={importing || splitting || !script.trim()}
              >
                {importing ? '导入中…' : '直接导入'}
              </button>
              <button
                type="button"
                className="btn btn-primary"
                onClick={handleSplitPreview}
                disabled={importing || splitting || !script.trim()}
              >
                <Wand2 size={15} />
                {splitting ? 'AI 切分中…（约 1~5 分钟，按剧本长度）' : 'AI 切分分镜'}
              </button>
            </div>
            <SplitProgressBar active={splitting} startedAt={splitStartedAt} />
          </>
        ) : (
          <>
            <div className="text-[var(--color-text-tertiary)] mb-2" style={{ fontSize: 'var(--font-size-xs)' }}>
              AI 镜头级切分完成：将新增 {splitPreview.rows.length} 镜（镜号{' '}
              {rows.length + 1} ~ {rows.length + splitPreview.rows.length}），确认后写入
              {splitPreview.engine === 'fallback' && '（当前为按行降级结果）'}：
            </div>
            <ul
              className="rounded-lg border border-[var(--color-border-light)] divide-y divide-[var(--color-divider)]"
              style={{ maxHeight: 320, overflow: 'auto', margin: 0, padding: 0, listStyle: 'none' }}
            >
              {splitPreview.rows.map((row, i) => (
                <li
                  key={row.id}
                  className="flex gap-2 items-baseline"
                  style={{ padding: 'var(--space-2) var(--space-3)' }}
                >
                  <span className="text-[var(--color-primary)]" style={{ fontWeight: 600, flexShrink: 0 }}>
                    #{rows.length + i + 1}
                  </span>
                  {row.camera_type && (
                    <span className="badge info" style={{ flexShrink: 0 }}>{row.camera_type}</span>
                  )}
                  {(row.duration ?? 0) > 0 && (
                    <span
                      className="text-[var(--color-text-tertiary)]"
                      style={{ flexShrink: 0, fontSize: 'var(--font-size-xs)' }}
                    >
                      {row.duration}s
                    </span>
                  )}
                  <span className="ellipsis" title={row.description} style={{ fontSize: 'var(--font-size-sm)', flex: 1, minWidth: 0 }}>
                    {row.description}
                    {row.original_dialogue && (
                      <span className="text-[var(--color-text-secondary)]"> · {row.original_dialogue}</span>
                    )}
                  </span>
                </li>
              ))}
            </ul>
            <div className="flex mt-4 gap-2" style={{ justifyContent: 'flex-end' }}>
              <button
                type="button"
                className="btn btn-secondary"
                onClick={() => setSplitPreview(null)}
                disabled={splitting}
              >
                返回修改
              </button>
              <button
                type="button"
                className="btn btn-primary"
                onClick={handleAutoSplit}
                disabled={splitting}
              >
                {splitting ? (
                  <span className="spinner" style={{ width: 15, height: 15 }} />
                ) : (
                  <Wand2 size={15} />
                )}
                {splitting ? '写入中…' : `确认导入 ${splitPreview.rows.length} 镜`}
              </button>
            </div>
          </>
        )}
      </div>

      {/* DSL 上传卡片 */}
      <div className="card mt-4" style={{ padding: 'var(--space-5)' }}>
        <h3 className="card-title flex items-center gap-2">
          <FileUp size={15} className="text-[var(--color-primary)]" />
          DSL 剧本文件
        </h3>
        <div
          className="rounded-lg border border-[var(--color-border-light)] mb-3"
          style={{ background: 'var(--color-input-bg)', padding: 'var(--space-3)' }}
        >
          <div className="flex items-center justify-between mb-1">
            <span style={{ fontSize: 'var(--font-size-xs)', color: 'var(--color-text-secondary)', fontWeight: 500 }}>
              语法示例（.txt / .dsl ≤10MB，每个镜头以 shot: 开头）
            </span>
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={() => {
                navigator.clipboard
                  .writeText(DSL_EXAMPLE)
                  .then(() => showToast('DSL 示例已复制到剪贴板', 'success'))
                  .catch(() => showToast('复制失败，请手动选择文本复制', 'error'));
              }}
            >
              复制示例
            </button>
          </div>
          <pre className="text-[var(--color-text-tertiary)]" style={{ fontSize: 'var(--font-size-xs)', margin: 0, whiteSpace: 'pre-wrap' }}>
{DSL_EXAMPLE}
          </pre>
        </div>
        <div className="flex items-center gap-3">
          <input
            ref={fileRef}
            type="file"
            accept=".txt,.dsl"
            style={{ display: 'none' }}
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) handleUpload(f);
            }}
          />
          <button
            type="button"
            className="btn btn-secondary"
            disabled={uploading}
            onClick={() => fileRef.current?.click()}
          >
            {uploading ? '上传中…' : '选择文件上传'}
          </button>
          <label className="flex items-center gap-2 cursor-pointer" style={{ fontSize: 'var(--font-size-xs)', color: 'var(--color-text-secondary)' }}>
            <input
              type="checkbox"
              checked={strict}
              onChange={(e) => setStrict(e.target.checked)}
              disabled={uploading}
            />
            严格模式（语法错误即整体拒绝）
          </label>
        </div>
      </div>
    </div>
  );
}

export default ScriptImport;
