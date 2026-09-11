// 本项目仅供学习使用，商业授权请+Q 3559331368
/* ==========================================================================
 * OmniSpace AI v2.3.1 —— 导入抽屉（工作台右侧，向分镜表追加剧本）
 * --------------------------------------------------------------------------
 * 紧凑版剧本导入（整页版见 ScriptImport.tsx，空项目首入时展示）：
 *   - 粘贴文本：AI 自动分镜（预览→确认）/ 直接导入（按行追加，上限 50 行）
 *   - DSL 文件上传：POST /comic/script/import-dsl
 * ========================================================================== */

import { useCallback, useRef, useState } from 'react';
import { FileUp, Wand2 } from 'lucide-react';
import { useAppStore } from '@/stores/useAppStore';
import { useMangaStore } from '@/stores/useMangaStore';
import * as mangaApi from '@/services/mangaApi';
import { getErrorMessage as errMsg, reportBgError } from '@/utils/errors';
import type { StoryboardRow } from '@/types';
import { SplitProgressBar } from '../SplitProgressBar';
import DrawerFrame from './DrawerFrame';

/** 分镜表行数硬上限（后端 STORYBOARD_MAX_ROWS，2026-08-23 50 → 200） */
const MAX_ROWS = 200;

interface ImportDrawerProps {
  onClose: () => void;
}

export function ImportDrawer({ onClose }: ImportDrawerProps) {
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
  /** AI 镜头级分镜预览（dry_run，2026-08-23 真分镜改造） */
  const [splitPreview, setSplitPreview] = useState<{
    splitId: string;
    rows: StoryboardRow[];
    engine: string;
    truncated: boolean;
  } | null>(null);
  const [uploading, setUploading] = useState(false);
  /** 切分发起时刻（SplitProgressBar 渐近进度基准） */
  const [splitStartedAt, setSplitStartedAt] = useState(0);
  const fileRef = useRef<HTMLInputElement>(null);

  // 直接导入
  const handleImport = useCallback(() => {
    const text = script.trim();
    if (!text) {
      showToast('请先粘贴剧本文本', 'warning');
      return;
    }
    setImporting(true);
    importScript(text)
      .then((added) => {
        setScript('');
        setSplitPreview(null);
        showToast(`剧本导入完成，新增 ${added} 行`, 'success');
      })
      .catch((err) => showToast(errMsg(err, '剧本导入失败'), 'error'))
      .finally(() => setImporting(false));

  }, [script, importScript, showToast]);

  // AI 镜头级分镜：dry-run 预览（后端真实推理）
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

  // AI 自动分镜：确认提交（复用预览 split_id）
  const handleAutoSplit = useCallback(() => {
    if (!splitPreview) return;
    autoSplitCommit(splitPreview.splitId)
      .then((added) => {
        setScript('');
        setSplitPreview(null);
        showToast(`AI 分镜完成，新增 ${added} 镜`, 'success');
      })
      .catch((err) => showToast(errMsg(err, 'AI 分镜确认失败'), 'error'));

  }, [splitPreview, autoSplitCommit, showToast]);

  // DSL 上传
  const handleUpload = useCallback(
    (file: File) => {
      if (!currentProject) return;
      setUploading(true);
      mangaApi
        .importDslFile(currentProject.id, file)
        .then(async (res) => {
          showToast(`DSL 导入完成，新增 ${res.added} 行`, 'success');
          await fetchRows().catch((err) => reportBgError('ImportDrawer.fetchRows', err));
        })
        .catch((err) => showToast(errMsg(err, 'DSL 导入失败'), 'error'))
        .finally(() => {
          setUploading(false);
          if (fileRef.current) fileRef.current.value = '';
        });
    },
     
    [currentProject, fetchRows, showToast],
  );

  return (
    <DrawerFrame title="剧本导入" subtitle={`· 当前 ${rows.length}/${MAX_ROWS} 行`} onClose={onClose}>
      {splitPreview === null ? (
        <div className="flex flex-col gap-3">
          <textarea
            className="input"
            rows={10}
            style={{ resize: 'vertical' }}
            placeholder={'粘贴剧本文本…\n每行一个分镜场景'}
            value={script}
            onChange={(e) => setScript(e.target.value)}
            disabled={importing || splitting}
          />
          <div className="flex gap-2">
            <button
              type="button"
              className="btn btn-secondary btn-sm flex-1"
              disabled={importing || splitting || !script.trim()}
              onClick={handleImport}
            >
              {importing ? '导入中…' : '直接导入'}
            </button>
            <button
              type="button"
              className="btn btn-primary btn-sm flex-1"
              disabled={importing || splitting || !script.trim()}
              onClick={handleSplitPreview}
            >
              <Wand2 size={13} />
              {splitting ? 'AI 切分中…' : 'AI 切分'}
            </button>
          </div>
          <SplitProgressBar active={splitting} startedAt={splitStartedAt} />

          <div style={{ borderTop: '1px solid var(--color-divider)', paddingTop: 'var(--space-3)' }}>
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
              className="btn btn-secondary btn-sm btn-block"
              disabled={uploading}
              onClick={() => fileRef.current?.click()}
            >
              <FileUp size={13} />
              {uploading ? '上传中…' : '上传 DSL 剧本文件'}
            </button>
            <div style={{ marginTop: 6, fontSize: 10, color: 'var(--color-text-tertiary)' }}>
              .txt / .dsl ≤10MB，每个镜头以 shot: 开头
            </div>
          </div>
        </div>
      ) : (
        <div className="flex flex-col gap-3">
          <div style={{ fontSize: 'var(--font-size-xs)', color: 'var(--color-text-tertiary)' }}>
            AI 镜头级切分完成：将新增 {splitPreview.rows.length} 镜（镜号{' '}
            {rows.length + 1} ~ {rows.length + splitPreview.rows.length}）
            {splitPreview.engine === 'fallback' && '（按行降级结果）'}：
          </div>
          <ul
            className="rounded-lg border border-[var(--color-border-light)] divide-y divide-[var(--color-divider)]"
            style={{ maxHeight: 300, overflow: 'auto', margin: 0, padding: 0, listStyle: 'none' }}
          >
            {splitPreview.rows.map((row, i) => (
              <li
                key={row.id}
                className="flex gap-2 items-baseline"
                style={{ padding: 'var(--space-2) var(--space-3)' }}
              >
                <span style={{ color: 'var(--color-primary)', fontWeight: 600, flexShrink: 0, fontSize: 'var(--font-size-xs)' }}>
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
                <span className="ellipsis" title={row.description} style={{ fontSize: 'var(--font-size-xs)', flex: 1, minWidth: 0 }}>
                  {row.description}
                  {row.original_dialogue && (
                    <span className="text-[var(--color-text-secondary)]"> · {row.original_dialogue}</span>
                  )}
                </span>
              </li>
            ))}
          </ul>
          <div className="flex gap-2">
            <button type="button" className="btn btn-secondary btn-sm flex-1" onClick={() => setSplitPreview(null)} disabled={splitting}>
              返回修改
            </button>
            <button type="button" className="btn btn-primary btn-sm flex-1" onClick={handleAutoSplit} disabled={splitting}>
              {splitting ? '写入中…' : `确认导入 ${splitPreview.rows.length} 镜`}
            </button>
          </div>
        </div>
      )}
    </DrawerFrame>
  );
}

export default ImportDrawer;
