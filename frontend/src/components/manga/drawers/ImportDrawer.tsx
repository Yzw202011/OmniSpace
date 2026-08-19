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
import DrawerFrame from './DrawerFrame';

/** 分镜表行数硬上限（后端 STORYBOARD_MAX_ROWS） */
const MAX_ROWS = 50;

interface ImportDrawerProps {
  onClose: () => void;
}

export function ImportDrawer({ onClose }: ImportDrawerProps) {
  const showToast = useAppStore((s) => s.showToast);
  const currentProject = useMangaStore((s) => s.currentProject);
  const rows = useMangaStore((s) => s.rows);
  const importScript = useMangaStore((s) => s.importScript);
  const autoSplit = useMangaStore((s) => s.autoSplit);
  const fetchRows = useMangaStore((s) => s.fetchRows);
  const splitting = useMangaStore((s) => s.splitting);

  const [script, setScript] = useState('');
  const [importing, setImporting] = useState(false);
  const [splitPreview, setSplitPreview] = useState<string[] | null>(null);
  const [uploading, setUploading] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const errMsg = (err: unknown, fallback: string): string =>
    err && typeof err === 'object' && 'message' in err
      ? (err as { message: string }).message
      : fallback;

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

  // AI 自动分镜：本地预览
  const handleSplitPreview = useCallback(() => {
    const text = script.trim();
    if (!text) {
      showToast('请先粘贴剧本文本', 'warning');
      return;
    }
    const segments = text.split('\n').map((s) => s.trim()).filter(Boolean);
    if (segments.length === 0) {
      showToast('剧本文本无有效行', 'warning');
      return;
    }
    if (rows.length + segments.length > MAX_ROWS) {
      showToast(
        `分镜表上限 ${MAX_ROWS} 行：当前 ${rows.length} 行，预览 ${segments.length} 行将超出`,
        'warning',
      );
      return;
    }
    setSplitPreview(segments);
  }, [script, rows.length, showToast]);

  // AI 自动分镜：确认提交
  const handleAutoSplit = useCallback(() => {
    const text = script.trim();
    if (!text) return;
    autoSplit(text)
      .then((added) => {
        setScript('');
        setSplitPreview(null);
        showToast(`AI 自动分镜完成，新增 ${added} 行`, 'success');
      })
      .catch((err) => showToast(errMsg(err, 'AI 自动分镜失败'), 'error'));
     
  }, [script, autoSplit, showToast]);

  // DSL 上传
  const handleUpload = useCallback(
    (file: File) => {
      if (!currentProject) return;
      setUploading(true);
      mangaApi
        .importDslFile(currentProject.id, file)
        .then(async (res) => {
          showToast(`DSL 导入完成，新增 ${res.added} 行`, 'success');
          await fetchRows().catch(() => undefined);
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
              AI 切分
            </button>
          </div>

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
            将新增 {splitPreview.length} 行（镜号 {rows.length + 1} ~ {rows.length + splitPreview.length}）：
          </div>
          <ul
            className="rounded-lg border border-[var(--color-border-light)] divide-y divide-[var(--color-divider)]"
            style={{ maxHeight: 300, overflow: 'auto', margin: 0, padding: 0, listStyle: 'none' }}
          >
            {splitPreview.map((seg, i) => (
              <li key={i} className="flex gap-2" style={{ padding: 'var(--space-2) var(--space-3)' }}>
                <span style={{ color: 'var(--color-primary)', fontWeight: 600, flexShrink: 0, fontSize: 'var(--font-size-xs)' }}>
                  #{rows.length + i + 1}
                </span>
                <span className="ellipsis" title={seg} style={{ fontSize: 'var(--font-size-xs)' }}>
                  {seg}
                </span>
              </li>
            ))}
          </ul>
          <div className="flex gap-2">
            <button type="button" className="btn btn-secondary btn-sm flex-1" onClick={() => setSplitPreview(null)} disabled={splitting}>
              返回修改
            </button>
            <button type="button" className="btn btn-primary btn-sm flex-1" onClick={handleAutoSplit} disabled={splitting}>
              {splitting ? '分镜中…' : `确认导入 ${splitPreview.length} 行`}
            </button>
          </div>
        </div>
      )}
    </DrawerFrame>
  );
}

export default ImportDrawer;
