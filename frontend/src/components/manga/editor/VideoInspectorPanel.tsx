// 本项目仅供学习使用，商业授权请+Q 3559331368
/* ==========================================================================
 * VideoInspectorPanel.tsx —— 漫剧编辑器右栏·视频详情面板
 * --------------------------------------------------------------------------
 * 2026-10-07 用户令功能修改：视频独立右栏面板（点表格视频缩略图进入，
 * 与分镜图详情面板互斥）；视觉/结构对齐 InspectorPanel（图2 范式）：
 *   1. 头部：← 返回 + 「镜 N 视频详情」+ 状态徽标 + 关闭
 *   2. 视频预览：干净封面框（悬停自动播放，点击大窗播放，右键下载）
 *   3. 重新生成视频：主按钮（生成中按钮内进度填充，同生图按钮范式）
 *   4. 历史记录：可折叠（默认收起，同分镜图面板），≤5 版（后端
 *      _prune_videos 自动裁剪旧版+文件）；点缩略视频大窗播放该版，
 *      右键/角标下载 MP4
 * ========================================================================== */

import { useEffect, useState } from 'react';
import {
  ArrowLeft,
  ChevronDown,
  Clapperboard,
  Download,
  History,
  RefreshCw,
  X,
} from 'lucide-react';
import { useAppStore } from '@/stores/useAppStore';
import useMangaStore from '@/stores/useMangaStore';
import type { MangaVideoTask } from '@/stores/manga/types';
import type { StoryboardRow } from '@/types';
import { getErrorMessage, reportActionError, reportBgError } from '@/utils/errors';

interface VideoInspectorPanelProps {
  rowId: string;
  onBack: () => void;
  onGenerateVideo: (row: StoryboardRow) => void;
}

/** 右键下载（fetch→blob 保文件名，同 InspectorPanel 范式） */
function downloadByUrl(url: string, filename: string, tag: string) {
  fetch(url)
    .then((r) => r.blob())
    .then((b) => {
      const u = URL.createObjectURL(b);
      const a = document.createElement('a');
      a.href = u;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(u);
    })
    .catch((err) => reportBgError(`VideoInspectorPanel.download(${tag})`, err));
}

function formatTime(ts?: number) {
  if (!ts) return '';
  const d = new Date(ts > 1e12 ? ts : ts * 1000);
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleString();
}

/** 该任务是否已有成品视频 */
function hasVideo(t?: MangaVideoTask): boolean {
  return !!(t && t.status === 'done' && t.download_url);
}

export default function VideoInspectorPanel({ rowId, onBack, onGenerateVideo }: VideoInspectorPanelProps) {
  const rows = useMangaStore((s) => s.rows);
  const videoTasks = useMangaStore((s) => s.videoTasks);
  const showToast = useAppStore((s) => s.showToast);
  const row = rows.find((r) => r.id === rowId);
  const [busy, setBusy] = useState(false);
  const [videoPreview, setVideoPreview] = useState<string | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);

  // 行最新任务（生成中进度/完成后主预览的数据源）
  const latest: MangaVideoTask | undefined = (() => {
    const list = videoTasks.filter((t) => t.row_id === rowId);
    if (!list.length) return undefined;
    return list.reduce((a, b) =>
      ((Number(b.created_at ?? 0) > Number(a.created_at ?? 0)) ? b : a));
  })();
  /** 完成版历史（最新在前；后端已裁剪至 5，前端再截一道防旧数据） */
  const history = videoTasks
    .filter((t) => t.row_id === rowId && t.status === 'done' && t.download_url)
    .sort((a, b) => Number(b.created_at ?? 0) - Number(a.created_at ?? 0))
    .slice(0, 5);
  const mainUrl = latest?.download_url ?? '';
  const generating = latest?.status === 'generating' || latest?.status === 'pending';

  // 切行时关大窗
  useEffect(() => {
    setVideoPreview(null);
  }, [rowId]);

  if (!row) return null;

  const shotNo = row.shot_number;
  const mp4Name = (t: MangaVideoTask) =>
    `镜${shotNo}_${formatTime(t.created_at).replace(/[/\s:]/g, '-') || t.task_id.slice(0, 8)}.mp4`;

  const handleGenerate = () => {
    if (busy || generating) return;
    setBusy(true);
    try {
      onGenerateVideo(row);
      showToast(`镜 ${shotNo} 视频已加入生成队列`, 'success');
    } catch (err: unknown) {
      reportActionError(err, '视频生成');
      showToast(getErrorMessage(err, '视频生成失败'), 'error');
    } finally {
      setBusy(false);
    }
  };

  return (
    <aside className="manga-ed-inspector">
      {/* 1. 头部：← 返回 + 标题 + 状态 + 关闭（同分镜图面板） */}
      <div className="manga-ed-inspector-head">
        <div className="flex items-center" style={{ gap: 'var(--space-1)', minWidth: 0 }}>
          <button type="button" className="btn-icon" style={{ width: 24, height: 24 }} title="返回" onClick={onBack}>
            <ArrowLeft size={14} />
          </button>
          <h3 className="manga-ed-inspector-title ellipsis">
            镜 {shotNo} 视频详情
            <span className={`badge ${generating ? 'warning' : latest?.status === 'error' ? 'error' : 'success'}`} style={{ marginLeft: 6 }}>
              {generating
                ? `${Math.round((latest?.progress ?? 0) * 100)}%`
                : latest
                  ? (latest.status === 'done' ? '已完成' : latest.status === 'error' ? '失败' : latest.status === 'cancelled' ? '已取消' : latest.status)
                  : '未生成'}
            </span>
          </h3>
        </div>
        <button type="button" className="btn-icon" style={{ width: 26, height: 26 }} title="关闭详情" aria-label="关闭详情" onClick={onBack}>
          <X size={14} />
        </button>
      </div>

      {/* 2. 视频预览：干净封面框（悬停播放，点击大窗，右键下载）——
          同分镜图预览「点击开灯箱」范式，不裸露播放器控件 */}
      <div className="manga-asset-preview">
        {mainUrl ? (
          <button
            type="button"
            className="manga-asset-preview-btn"
            title="点击大窗播放（右键下载 MP4）"
            onClick={() => setVideoPreview(mainUrl)}
            onContextMenu={(e) => {
              e.preventDefault();
              downloadByUrl(mainUrl, `镜${shotNo}_${new Date().toISOString().slice(0, 10)}.mp4`, 'main');
            }}
          >
            <video
              key={mainUrl}
              src={mainUrl}
              muted
              playsInline
              preload="metadata"
              onMouseEnter={(e) => { void e.currentTarget.play().catch(() => { /* 自动播放被拒时静默 */ }); }}
              onMouseLeave={(e) => { e.currentTarget.pause(); e.currentTarget.currentTime = 0; }}
            />
            {generating && (
              <span className="manga-insp-preview-tag">生成中 {Math.round((latest?.progress ?? 0) * 100)}%</span>
            )}
          </button>
        ) : (
          <div className="manga-asset-preview-empty">
            <Clapperboard size={22} />
            {generating ? '视频生成中…' : '该镜还没有视频，点击下方按钮生成'}
          </div>
        )}
      </div>

      {/* 3. 重新生成视频：主按钮（生成中填充进度，同生图按钮范式） */}
      <button
        type="button"
        className="btn btn-primary manga-asset-gen-btn"
        disabled={busy || generating}
        title={hasVideo(latest) ? '重新生成视频：产出新版本，旧版保留（最多 5 版）' : '生成本镜视频'}
        onClick={handleGenerate}
      >
        {generating ? (
          <>
            <span
              className="manga-gen-btn-fill"
              style={{ width: `${Math.round((latest?.progress ?? 0) * 100)}%` }}
              aria-hidden="true"
            />
            <span className="manga-gen-btn-content">
              <span className="spinner manga-mini-spin" />
              {`生成中 ${Math.round((latest?.progress ?? 0) * 100)}%`}
            </span>
          </>
        ) : (
          <>
            <RefreshCw size={14} />
            {hasVideo(latest) ? '重新生成视频' : '生成视频'}
          </>
        )}
      </button>

      {/* 4. 历史记录：可折叠（默认收起，同分镜图面板），≤5 版 */}
      <div className="manga-history">
        <button
          type="button"
          className="manga-history-head"
          aria-expanded={historyOpen}
          onClick={() => setHistoryOpen((v) => !v)}
        >
          <History size={13} aria-hidden="true" />
          <span style={{ flex: 1, textAlign: 'left' }}>历史记录（{history.length}）</span>
          <ChevronDown size={14} className={`manga-dock-chevron${historyOpen ? ' open' : ''}`} aria-hidden="true" />
        </button>
        {historyOpen && (
          <div className="manga-history-body">
            {!history.length ? (
              <div className="manga-history-empty">暂无视频版本</div>
            ) : (
              <div className="flex flex-col gap-2">
                {history.map((k) => (
                  <div
                    key={k.task_id}
                    style={{
                      borderRadius: 'var(--radius-md)',
                      overflow: 'hidden',
                      border: k.download_url === mainUrl ? '1px solid var(--color-primary)' : '1px solid var(--color-border-light)',
                      boxShadow: k.download_url === mainUrl ? '0 0 0 1px var(--color-primary)' : undefined,
                    }}
                  >
                    <video
                      src={k.download_url ?? ''}
                      muted
                      playsInline
                      preload="metadata"
                      title="点击大窗播放该版本，右键下载"
                      onClick={() => setVideoPreview(k.download_url ?? null)}
                      onContextMenu={(e) => {
                        e.preventDefault();
                        downloadByUrl(k.download_url ?? '', mp4Name(k), 'history');
                      }}
                      onMouseEnter={(e) => { void e.currentTarget.play().catch(() => { /* 静默 */ }); }}
                      onMouseLeave={(e) => { e.currentTarget.pause(); e.currentTarget.currentTime = 0; }}
                      style={{ width: '100%', aspectRatio: '16/9', objectFit: 'cover', background: '#000', display: 'block', cursor: 'pointer' }}
                    />
                    <div style={{ padding: 'var(--space-2)' }}>
                      <div className="flex items-center gap-2" style={{ fontSize: 'var(--font-size-xs)' }}>
                        <span style={{ fontWeight: 600 }}>{formatTime(k.created_at)}</span>
                        {k.degraded && <span className="badge" title={k.degrade_reason || ''}>降级</span>}
                        {k.download_url === mainUrl && <span className="badge primary">当前</span>}
                        <a
                          href={k.download_url ?? ''}
                          download
                          title="下载 MP4"
                          style={{ marginLeft: 'auto', display: 'flex', color: 'var(--color-text-secondary)' }}
                          onClick={(e) => e.stopPropagation()}
                        >
                          <Download size={12} aria-hidden="true" />
                        </a>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      {/* 视频大窗播放（点击预览/历史条目打开，同表格原大窗标记） */}
      {videoPreview && (
        <div className="manga-media-preview" onClick={() => setVideoPreview(null)}>
          <div className="manga-media-preview-body" onClick={(e) => e.stopPropagation()}>
            <video src={videoPreview} controls autoPlay />
            <div className="manga-media-preview-ops">
              <a href={videoPreview} download onClick={(e) => e.stopPropagation()}>下载 MP4</a>
              <button type="button" onClick={() => setVideoPreview(null)}>关闭</button>
            </div>
          </div>
        </div>
      )}
    </aside>
  );
}
