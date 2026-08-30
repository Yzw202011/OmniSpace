/* ==========================================================================
 * StoryboardTable.tsx —— 漫剧编辑器中央·表格式分镜（竞品编辑器布局）
 * --------------------------------------------------------------------------
 * 列：☑ │ 序号(圆标+⟳重词/🔒锁定) │ 原文 │ 分镜描述词 │ 出场人物 │ 场景 │ 道具 │ 分镜图 │ 视频 │ 操作
 *  - 原文/描述词：点击单元格内联编辑，失焦/Ctrl+Enter 提交（1.2s 防抖全量保存）；
 *    原文只读态角色名高亮（manga-entity：资产库角色名 ∪ 行 characters 命中名）
 *  - 资产三列：row.asset_ids × 资产库按 kind 过滤，缩略图卡横排堆叠 + 虚线「+」空位；
 *    点缩略图 → 资产详情面板；点「+」 → 设置 bindingTarget，右侧资产面板进入绑定模式
 *  - 序号列：⟳ 重新生成描述词（单行 aiDescribe，锁定/忙碌禁用）；🔒 锁定切换
 *    （锁定后批量操作自动跳过此行，整行降透明度 + 左侧锁色条）
 *  - 分镜图：当前关键帧缩略图（点击开分镜详情）；无图「生成」占位同开详情，
 *    生成统一在详情面板操作（2026-08-25 用户裁定：行内不直接触发生图）
 *  - 视频：任务状态/进度/下载如实展示（真实轮询），无则虚线「生成」
 *  - 操作列：详情(检查器) / 导演台 / 上移 / 下移 / 删除
 *  - 勾选多行弹出批量条：批量生词 / 批量生图 / 批量删除（串行真实调用，锁定行跳过）
 * 保存契约沿用 PUT /manga/storyboard/{pid} 全量同步（增/删/移立即保存）。
 * ========================================================================== */

import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import {
  ArrowDown,
  ArrowUp,
  Clapperboard,
  Download,
  ImagePlus,
  Lock,
  LockOpen,
  Plus,
  RefreshCw,
  SlidersHorizontal,
  Trash2,
  Wand2,
  X,
} from 'lucide-react';
import { useAppStore } from '@/stores/useAppStore';
import { useMangaStore } from '@/stores/useMangaStore';
import { aiDescribe, assetMediaVersion, getMediaUrl } from '@/services/mangaApi';


// 右键直接下载（视频/分镜图/资产图通用，2026-08-30）
function downloadMedia(url: string, filename: string) {
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
}
import type { ComicAsset, ComicAssetKind, KeyframeItem, ShotSaveStatus, StoryboardRow as StoryboardRowData } from '@/types';
import type { MangaVideoTask } from '@/stores/useMangaStore';
import { VIDEO_STATUS_LABELS } from '@/constants/statusLabels';
import { getErrorMessage, reportBgError } from '@/utils/errors';
import { batchDescribe, batchKeyframes, readPromptPrefix } from './batchOps';

/** 分镜行数硬上限（后端 STORYBOARD_MAX_ROWS，2026-08-23 50 → 200） */
const MAX_ROWS = 200;
/** 文本编辑防抖间隔 */
const SAVE_DEBOUNCE = 1200;

/** 保存状态（顶栏状态点）：类型真源在 @/types（TASK-P2-07 迁出） */
export type { ShotSaveStatus };

/* 错误消息提取统一走 @/utils/errors（TASK-P2-08） */

function escapeRegExp(s: string) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** 原文只读态：命中角色名渲染为彩色高亮 span（资产库角色名 ∪ 行 characters） */
function renderEntityHighlight(text: string, names: string[]): ReactNode {
  const unique = [...new Set(names.map((n) => n.trim()).filter(Boolean))].sort(
    (a, b) => b.length - a.length,
  );
  if (!text || unique.length === 0) return text;
  const pattern = new RegExp(`(${unique.map(escapeRegExp).join('|')})`, 'g');
  const hit = new Set(unique);
  return text.split(pattern).map((part, i) =>
    hit.has(part) ? (
      // 高亮名为纯文本片段，key 用索引即可（列表静态无重排）
      <span key={i} className="manga-entity">
        {part}
      </span>
    ) : (
      part
    ),
  );
}

/* ------------------------------ 内联编辑单元格 ------------------------------ */

interface EditableCellProps {
  value: string;
  placeholder: string;
  /** AI 生成内容铺薄荷绿底（§14 约束12） */
  ai?: boolean;
  /** 只读态实体高亮名表（原文列角色名高亮） */
  highlightNames?: string[];
  /** 提交（失焦 / Ctrl+Enter） */
  onCommit: (v: string) => void;
}

function EditableCell({ value, placeholder, ai, highlightNames, onCommit }: EditableCellProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);

  // 外部值变化（AI 写回/撤销）且非编辑中：同步草稿
  useEffect(() => {
    if (!editing) setDraft(value);
  }, [value, editing]);

  const commit = () => {
    setEditing(false);
    if (draft !== value) onCommit(draft);
  };

  if (!editing) {
    return (
      <div
        className={`manga-cell-text${ai ? ' ai' : ''}${!value ? ' empty' : ''}`}
        title={ai ? 'AI 生成内容，点击编辑' : '点击编辑'}
        onClick={(e) => {
          e.stopPropagation();
          setDraft(value);
          setEditing(true);
        }}
      >
        {value ? (
          highlightNames && highlightNames.length > 0 ? (
            renderEntityHighlight(value, highlightNames)
          ) : (
            value
          )
        ) : (
          <span className="manga-cell-ph">{placeholder}</span>
        )}
      </div>
    );
  }
  return (
    <textarea
      className="manga-cell-editor"
      value={draft}
      rows={Math.min(10, Math.max(3, draft.split('\n').length + 1))}
      maxLength={2000}
      autoFocus
      onClick={(e) => e.stopPropagation()}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
          e.preventDefault();
          commit();
        }
        if (e.key === 'Escape') {
          setDraft(value);
          setEditing(false);
        }
      }}
    />
  );
}

/* ------------------------------ 资产单元格（出场人物/场景/道具） ------------------------------ */

interface AssetCellProps {
  row: StoryboardRowData;
  kind: ComicAssetKind;
  assets: ComicAsset[];
  /** 点缩略图 → 打开资产详情面板 */
  onOpenAsset: (assetId: string) => void;
  /** 点「+」 → 进入绑定模式（bindingTarget + 选中行） */
  onStartBind: () => void;
  /** 点右上角 X → 解绑该资产（toggle 语义：已绑定即解绑） */
  onUnbindAsset: (assetId: string, rowId: string) => void;
}

function AssetCell({ row, kind, assets, onOpenAsset, onStartBind, onUnbindAsset }: AssetCellProps) {
  const ids = row.asset_ids ?? (row.asset_id ? [row.asset_id] : []);
  const bound = ids
    .map((id) => assets.find((a) => a.asset_id === id))
    .filter((a): a is ComicAsset => !!a && a.kind === kind);
  return (
    <div className="manga-asset-cell">
      {bound.map((a) => (
        <div key={a.asset_id} className="manga-mini-card-wrap">
          <button
            type="button"
            className="manga-mini-card"
            title={`查看资产「${a.name}」详情`}
            onClick={(e) => {
              e.stopPropagation();
              onOpenAsset(a.asset_id);
            }}
          >
            {a.file_path ? (
              <img
              src={getMediaUrl(a.file_path, assetMediaVersion(a))}
              alt={a.name}
              loading="lazy"
              title={`资产「${a.name}」——右键下载`}
              onContextMenu={(e) => {
                e.preventDefault();
                e.stopPropagation();
                downloadMedia(getMediaUrl(a.file_path, assetMediaVersion(a)), `${a.name}.png`);
              }}
            />
            ) : (
              <span className="manga-mini-card-ph">无图</span>
            )}
            <span className="manga-mini-card-name ellipsis">{a.name}</span>
          </button>
          <button
            type="button"
            className="manga-mini-card-unbind"
            title={`解绑「${a.name}」（仅从本镜移除，不删除资产本身）`}
            aria-label={`解绑「${a.name}」`}
            onClick={(e) => {
              e.stopPropagation();
              onUnbindAsset(a.asset_id, row.id);
            }}
          >
            <X size={8} strokeWidth={3} />
          </button>
        </div>
      ))}
      <button
        type="button"
        className="manga-mini-add"
        title="绑定资产：点击后在右侧资产面板点选绑定/解绑"
        onClick={(e) => {
          e.stopPropagation();
          onStartBind();
        }}
      >
        <Plus size={14} />
      </button>
    </div>
  );
}

/* ------------------------------ 分镜行（React.memo 行级渲染单元） ------------------------------ */

/**
 * 行 props 稳定化约定（默认浅比较即可命中 memo）：
 *  - row 来自 store rows 数组：updateRow / updateField / syncRowGenerationStatus / bindAssetToRow
 *    均为 map 单行替换，未修改行保持对象引用；applyStructure 仅对 shot_number 实际变化的行重建对象
 *  - currentKeyframe / videoTask 为 keyframes / videoTasks 数组元素引用，未更新时引用不变
 *  - characterNames 经父级 useMemo 稳定化（随 assets 变化才更新）
 *  - 回调全部为 store action 或父级 useCallback 常量
 * 注：本项目未启用 React Compiler，手动 memo/useCallback 在此 50 行 × 10 列渲染热点属合理例外。
 */
interface StoryboardRowProps {
  row: StoryboardRowData;
  isSelected: boolean;
  isChecked: boolean;
  isFirst: boolean;
  isLast: boolean;
  structSaving: boolean;
  /** 行级 AI 操作忙碌键（`${rowId}:${op}`；'' 为空闲） */
  busyKey: string;
  /** 资产库（store 数组引用，fetch 时才变化） */
  assets: ComicAsset[];
  /** 当前关键帧（keyframesMap 元素引用） */
  currentKeyframe: KeyframeItem | undefined;
  /** 行最新视频任务（videoTasks 元素引用，无任务为 undefined） */
  videoTask: MangaVideoTask | undefined;
  /** 原文列高亮名表（资产库角色名，父级 useMemo） */
  characterNames: string[];
  onSelectRow: (rowId: string) => void;
  onToggleCheck: (rowId: string) => void;
  onDescribe: (row: StoryboardRowData) => void;
  onToggleLock: (row: StoryboardRowData) => void;
  onUpdateField: (rowId: string, patch: Partial<StoryboardRowData>) => void;
  onOpenAsset: (assetId: string) => void;
  onStartBind: (rowId: string, kind: ComicAssetKind) => void;
  /** 点缩略图右上角 X → 解绑资产（toggle 语义：已绑定即解绑） */
  onUnbindAsset: (assetId: string, rowId: string) => void;
  onOpenInspector: (rowId: string) => void;
  onMove: (rowId: string, dir: -1 | 1) => void;
  onRemove: (rowId: string) => void;
  onGenerateVideo: (row: StoryboardRowData) => void;
}

// 纯提取原内联行 JSX：DOM 结构与 className 完全不变，仅变量改名（r→row、task→videoTask、curKf→currentKeyframe）
const StoryboardRow = memo(function StoryboardRow({
  row,
  isSelected,
  isChecked,
  isFirst,
  isLast,
  structSaving,
  busyKey,
  assets,
  currentKeyframe,
  videoTask,
  characterNames,
  onSelectRow,
  onToggleCheck,
  onDescribe,
  onToggleLock,
  onUpdateField,
  onOpenAsset,
  onStartBind,
  onUnbindAsset,
  onOpenInspector,
  onMove,
  onRemove,
  onGenerateVideo,
}: StoryboardRowProps) {
  const locked = row.is_locked === true;
  const [videoPreview, setVideoPreview] = useState<string | null>(null);
  return (
    <div
      className={`manga-sb-tr manga-sb-row${isSelected ? ' selected' : ''}${locked ? ' locked' : ''}`}
      onClick={() => onSelectRow(row.id)}
    >
      {/* 勾选 */}
      <div className="manga-sb-td c-check" onClick={(e) => e.stopPropagation()}>
        <input
          type="checkbox"
          className="manga-checkbox"
          aria-label={`选择镜 ${row.shot_number}`}
          checked={isChecked}
          onChange={() => onToggleCheck(row.id)}
        />
      </div>

      {/* 序号：圆标 + 竖排工具（⟳重词 / 🔒锁定） */}
      <div className="manga-sb-td c-idx">
        <span className="manga-idx-badge">{row.shot_number}</span>
        <span className="manga-idx-tools">
          <button
            type="button"
            className="manga-row-tool"
            title={
              locked
                ? '行已锁定，解锁后才能重新生成'
                : !(row.asset_ids?.length ?? 0) && !row.asset_id
                  ? '未绑定资产：先在资产列绑定角色/场景/道具才能生成描述词'
                  : row.description
                    ? '重新生成描述词'
                    : 'AI 生成描述词'
            }
            aria-label={`镜 ${row.shot_number} 重新生成描述词`}
            disabled={busyKey !== '' || locked || (!(row.asset_ids?.length ?? 0) && !row.asset_id)}
            onClick={(e) => {
              e.stopPropagation();
              void onDescribe(row);
            }}
          >
            {busyKey === `${row.id}:describe` ? <span className="spinner manga-mini-spin" /> : <RefreshCw size={14} />}
          </button>
          <button
            type="button"
            className={`manga-row-tool${locked ? ' lock-on' : ''}`}
            title={locked ? '解锁（批量操作将处理此行）' : '锁定后批量操作跳过此行'}
            aria-label={`镜 ${row.shot_number} ${locked ? '解锁' : '锁定'}`}
            aria-pressed={locked}
            onClick={(e) => {
              e.stopPropagation();
              onToggleLock(row);
            }}
          >
            {locked ? <Lock size={14} /> : <LockOpen size={14} />}
          </button>
        </span>
      </div>

      {/* 原文（只读态角色名高亮） */}
      <div className="manga-sb-td c-text">
        <EditableCell
          value={row.original_dialogue}
          placeholder="原文台词 / 剧本内容…"
          highlightNames={[...characterNames, ...row.characters]}
          onCommit={(v) => onUpdateField(row.id, { original_dialogue: v })}
        />
      </div>

      {/* 分镜描述词 */}
      <div className="manga-sb-td c-desc">
        <EditableCell
          value={row.description}
          placeholder="分镜描述词（可点序号旁的 ⟳ 生成）…"
          ai={row.is_ai_generated}
          onCommit={(v) => onUpdateField(row.id, { description: v, is_ai_generated: false })}
        />
      </div>

      {/* 出场人物 */}
      <div className="manga-sb-td c-char">
        <AssetCell
          row={row}
          kind="character"
          assets={assets}
          onOpenAsset={onOpenAsset}
          onStartBind={() => onStartBind(row.id, 'character')}
          onUnbindAsset={onUnbindAsset}
        />
      </div>

      {/* 场景 */}
      <div className="manga-sb-td c-scene">
        <AssetCell
          row={row}
          kind="scene"
          assets={assets}
          onOpenAsset={onOpenAsset}
          onStartBind={() => onStartBind(row.id, 'scene')}
          onUnbindAsset={onUnbindAsset}
        />
      </div>

      {/* 道具 */}
      <div className="manga-sb-td c-prop">
        <AssetCell
          row={row}
          kind="prop"
          assets={assets}
          onOpenAsset={onOpenAsset}
          onStartBind={() => onStartBind(row.id, 'prop')}
          onUnbindAsset={onUnbindAsset}
        />
      </div>

      {/* 分镜图（当前关键帧） */}
      <div className="manga-sb-td c-image" onClick={(e) => e.stopPropagation()}>
        {currentKeyframe?.file_path ? (
          <button type="button" className="manga-thumb manga-thumb-btn" title="查看/管理关键帧版本" onClick={() => onOpenInspector(row.id)}>
            <img
              src={getMediaUrl(currentKeyframe.file_path, `${currentKeyframe.version}-${currentKeyframe.created_at}`)}
              alt={`镜 ${row.shot_number} 分镜图`}
              loading="lazy"
              title="左键查看/管理版本，右键下载"
              onContextMenu={(e) => {
                e.preventDefault();
                e.stopPropagation();
                downloadMedia(getMediaUrl(currentKeyframe.file_path, `${currentKeyframe.version}-${currentKeyframe.created_at}`), `镜${row.shot_number}_分镜图_v${currentKeyframe.version}.png`);
              }}
            />
            <span className="manga-thumb-name">v{currentKeyframe.version}</span>
          </button>
        ) : (
          <button
            type="button"
            className="manga-gen-btn"
            title="打开分镜详情，编辑描述词后生成分镜图"
            onClick={() => onOpenInspector(row.id)}
          >
            <ImagePlus size={15} />
            生成
          </button>
        )}
      </div>

      {/* 视频 */}
      <div className="manga-sb-td c-video" onClick={(e) => e.stopPropagation()}>
        {videoTask && (videoTask.status === 'generating' || videoTask.status === 'pending') ? (
          <div className="manga-video-prog" title={`${VIDEO_STATUS_LABELS[videoTask.status]} ${Math.round(videoTask.progress * 100)}%`}>
            <span className="manga-video-prog-bar" style={{ width: `${Math.round(videoTask.progress * 100)}%` }} />
            <span className="manga-video-prog-text">{Math.round(videoTask.progress * 100)}%</span>
          </div>
        ) : videoTask && videoTask.status === 'done' && videoTask.download_url ? (
          // 已生成视频：缩略视频框（悬停预览，角标下载/重新生成）
          <div
              className="manga-video-thumb"
              title={`镜 ${row.shot_number} 视频（悬停预览，点击大窗播放，右键下载）`}
              onClick={(e) => { e.stopPropagation(); setVideoPreview(videoTask.download_url ?? null); }}
              onContextMenu={(e) => {
                e.preventDefault();
                e.stopPropagation();
                downloadMedia(videoTask.download_url ?? "", `镜${row.shot_number}_${new Date().toISOString().slice(0, 10)}.mp4`);
              }}
            >
            <video
              src={videoTask.download_url}
              muted
              playsInline
              preload="metadata"
              onMouseEnter={(e) => { void e.currentTarget.play().catch(() => { /* 自动播放被拒时静默 */ }); }}
              onMouseLeave={(e) => { e.currentTarget.pause(); e.currentTarget.currentTime = 0; }}
            />
            <a
              className="manga-video-dl"
              href={videoTask.download_url}
              download
              title="下载 MP4"
              onClick={(e) => e.stopPropagation()}
            >
              <Download size={11} aria-hidden="true" />
            </a>
            <button
              type="button"
              className="manga-video-redo"
              title="重新生成视频"
              disabled={busyKey !== ''}
              onClick={(e) => {
                e.stopPropagation();
                onGenerateVideo(row);
              }}
            >
              <RefreshCw size={11} aria-hidden="true" />
            </button>
          </div>
        ) : videoTask && videoTask.status === 'error' ? (
          <button
            type="button"
            className="manga-gen-btn error"
            title={videoTask.error || '生成失败，点击重试'}
            onClick={() => onGenerateVideo(row)}
          >
            失败重试
          </button>
        ) : (
          <button
            type="button"
            className="manga-gen-btn"
            disabled={row.generation_status === 'generating'}
            title="生成该镜视频"
            onClick={() => onGenerateVideo(row)}
          >
            <Clapperboard size={14} />
            生成
          </button>
        )}
      </div>

      {/* 操作 */}
      <div className="manga-sb-td c-ops" onClick={(e) => e.stopPropagation()}>
        <button type="button" className="manga-row-tool" title="分镜详情（预览/描述词/版本历史）" aria-label={`镜 ${row.shot_number} 详情`} onClick={() => onOpenInspector(row.id)}>
          <SlidersHorizontal size={14} />
        </button>
        <button type="button" className="manga-row-tool" title="上移" aria-label="上移" disabled={isFirst || structSaving} onClick={() => onMove(row.id, -1)}>
          <ArrowUp size={14} />
        </button>
        <button type="button" className="manga-row-tool" title="下移" aria-label="下移" disabled={isLast || structSaving} onClick={() => onMove(row.id, 1)}>
          <ArrowDown size={14} />
        </button>
        <button type="button" className="manga-row-tool danger" title="删除本镜" aria-label="删除本镜" disabled={structSaving} onClick={() => onRemove(row.id)}>
          <Trash2 size={14} />
        </button>
      </div>

      {/* 视频预览大窗（点击缩略框打开） */}
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
    </div>
  );
});

/* ------------------------------ 主组件 ------------------------------ */

export interface StoryboardTableProps {
  /** 保存状态变化（顶栏状态点） */
  onSaveStatus?: (s: ShotSaveStatus) => void;
  /** 生成视频 */
  onGenerateVideo: (row: StoryboardRowData) => void;
  /** 打开行检查器（详情/关键帧/音色） */
  onOpenInspector: (rowId: string) => void;
}

export default function StoryboardTable({ onSaveStatus, onGenerateVideo, onOpenInspector }: StoryboardTableProps) {
  const showToast = useAppStore((s) => s.showToast);
  const currentProject = useMangaStore((s) => s.currentProject);
  const rows = useMangaStore((s) => s.rows);
  const loading = useMangaStore((s) => s.loading);
  const selectedRowId = useMangaStore((s) => s.selectedRowId);
  const setSelectedRow = useMangaStore((s) => s.setSelectedRow);
  const saveRows = useMangaStore((s) => s.saveRows);
  const updateRow = useMangaStore((s) => s.updateRow);
  const keyframesMap = useMangaStore((s) => s.keyframes);
  const fetchKeyframes = useMangaStore((s) => s.fetchKeyframes);
  const assets = useMangaStore((s) => s.assets);
  const videoTasks = useMangaStore((s) => s.videoTasks);
  const setSelectedAsset = useMangaStore((s) => s.setSelectedAsset);
  const setBindingTarget = useMangaStore((s) => s.setBindingTarget);
  const bindAssetToRow = useMangaStore((s) => s.bindAssetToRow);

  /** 结构操作（增/删/移）保存中 */
  const [structSaving, setStructSaving] = useState(false);
  /** 行级 AI 操作忙碌（rowId:op） */
  const [busyKey, setBusyKey] = useState('');
  /** 勾选集合（批量条） */
  const [checked, setChecked] = useState<ReadonlySet<string>>(new Set());
  /** 批量执行中 */
  const [batching, setBatching] = useState(false);
  /** 防抖定时器 */
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // 卸载清理
  useEffect(() => () => { if (debounceRef.current) clearTimeout(debounceRef.current); }, []);

  // 项目打开后：并行拉取未缓存行的关键帧（≤50 行上限，本地 SQLite 低开销）
  useEffect(() => {
    if (loading) return;
    const st = useMangaStore.getState();
    st.rows.forEach((r) => {
      if (!st.keyframes[r.id]) {
        fetchKeyframes(r.id).catch((err) => reportBgError('StoryboardTable.prefetchKeyframes', err));
      }
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loading, currentProject?.id]);

  /** 全量保存（统一出口），返回 Promise 供调用方 finally */
  // useCallback 稳定化：行组件 props 引用恒定是 memo 命中前提（Compiler 未启用的手动优化）
  const persist = useCallback(
    (next: StoryboardRowData[]): Promise<void> => {
      onSaveStatus?.('saving');
      return saveRows(next)
        .then(() => onSaveStatus?.('saved'))
        .catch((err: unknown) => {
          onSaveStatus?.('error');
          showToast(getErrorMessage(err, '分镜保存失败'), 'error');
        });
    },
    [onSaveStatus, saveRows, showToast],
  );

  /** 文本编辑：更新 store + 防抖保存（map 单行替换，未修改行引用不变） */
  const updateField = useCallback(
    (rowId: string, patch: Partial<StoryboardRowData>) => {
      const next = useMangaStore.getState().rows.map((r) => (r.id === rowId ? { ...r, ...patch } : r));
      useMangaStore.setState({ rows: next });
      onSaveStatus?.('dirty');
      if (debounceRef.current) clearTimeout(debounceRef.current);
      debounceRef.current = setTimeout(() => persist(useMangaStore.getState().rows), SAVE_DEBOUNCE);
    },
    [onSaveStatus, persist],
  );

  /** 结构操作（增/删/移）：重编号 + 立即保存 */
  const applyStructure = useCallback(
    (next: StoryboardRowData[]) => {
      // 清除待执行的防抖保存，防止旧快照覆盖结构变更
      if (debounceRef.current) {
        clearTimeout(debounceRef.current);
        debounceRef.current = null;
      }
      // 仅 shot_number 实际变化的行重建对象，其余行保持引用以命中行级 memo
      const renumbered = next.map((r, i) => (r.shot_number === i + 1 ? r : { ...r, shot_number: i + 1 }));
      useMangaStore.setState({ rows: renumbered });
      setStructSaving(true);
      persist(renumbered).finally(() => setStructSaving(false));
    },
    [persist],
  );

  /** 添加分镜（末尾） */
  const addShot = () => {
    const cur = useMangaStore.getState().rows;
    if (cur.length >= MAX_ROWS) {
      showToast(`已达最大行数限制（${MAX_ROWS} 镜）`, 'warning');
      return;
    }
    const newRow: StoryboardRowData = {
      id: `row_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
      shot_number: cur.length + 1,
      original_dialogue: '',
      description: '',
      characters: [],
      scene: '',
      props: [],
      voice_id: '',
      voice_emotion: '',
      director_stage_done: false,
      generation_status: 'pending',
      is_ai_generated: false,
      asset_ids: [],
      is_locked: false,
    };
    applyStructure([...cur, newRow]);
    setSelectedRow(newRow.id);
  };

  /** 删除分镜 */
  const removeShot = useCallback(
    (rowId: string) => {
      if (!window.confirm('确定删除该分镜？此操作不可恢复')) return;
      const cur = useMangaStore.getState().rows;
      applyStructure(cur.filter((r) => r.id !== rowId));
      // selectedRowId 经 getState 调用时读取，避免闭包依赖破坏 useCallback 引用稳定
      if (useMangaStore.getState().selectedRowId === rowId) setSelectedRow(null);
    },
    [applyStructure, setSelectedRow],
  );

  /** 上/下移 */
  const moveShot = useCallback(
    (rowId: string, dir: -1 | 1) => {
      const cur = [...useMangaStore.getState().rows];
      const idx = cur.findIndex((r) => r.id === rowId);
      const target = idx + dir;
      if (idx < 0 || target < 0 || target >= cur.length) return;
      [cur[idx], cur[target]] = [cur[target], cur[idx]];
      applyStructure(cur);
    },
    [applyStructure],
  );

  /** AI 前置：本地新建行先全量持久化（仅读 getState，无捕获依赖） */
  const ensurePersisted = useCallback(async () => {
    const st = useMangaStore.getState();
    if (st.rows.some((r) => r.id.startsWith('row_'))) {
      await st.saveRows(st.rows);
    }
  }, []);

  /** 行内 AI 重新生成描述词（序号列 ⟳ 按钮；custom 提示词配置经 prompt_prefix 透传）
   *  2026-08-25 用户裁定：未绑定资产的行不允许生成分镜描述词（后端同门槛双保险） */
  const handleDescribe = useCallback(
    async (row: StoryboardRowData) => {
      if (!currentProject) return;
      if (row.is_locked) {
        showToast('该行已锁定，解锁后才能重新生成描述词', 'warning');
        return;
      }
      if (!row.original_dialogue.trim()) {
        showToast('先填写原文台词，AI 生词需要台词作为输入', 'warning');
        return;
      }
      if (!(row.asset_ids?.length ?? 0) && !row.asset_id) {
        showToast('该行未绑定资产，先在资产列绑定角色/场景/道具才能生成描述词', 'warning');
        return;
      }
      setBusyKey(`${row.id}:describe`);
      try {
        await ensurePersisted();
        const description = await aiDescribe(row.id, currentProject.id, readPromptPrefix());
        if (description) {
          await updateRow(row.id, { description, is_ai_generated: true });
        }
        showToast(`镜 ${row.shot_number} 描述词已生成`, 'success');
      } catch (err: unknown) {
        showToast(getErrorMessage(err, 'AI 生词失败'), 'error');
      } finally {
        setBusyKey('');
      }
    },
    [currentProject, showToast, ensurePersisted, updateRow],
  );

  /** 行锁定切换（锁定后批量操作自动跳过此行，PUT 单行持久化） */
  const handleToggleLock = useCallback(
    (row: StoryboardRowData) => {
      if (row.id.startsWith('row_')) {
        showToast('新分镜保存后才能锁定', 'info');
        return;
      }
      updateRow(row.id, { is_locked: !row.is_locked })
        .then(() => showToast(row.is_locked ? `镜 ${row.shot_number} 已解锁` : `镜 ${row.shot_number} 已锁定，批量操作将跳过`, 'success'))
        .catch((err: unknown) => showToast(getErrorMessage(err, '锁定状态更新失败'), 'error'));
    },
    [showToast, updateRow],
  );

  /** 勾选切换 */
  const toggleCheck = useCallback((rowId: string) => {
    setChecked((prev) => {
      const next = new Set(prev);
      if (next.has(rowId)) next.delete(rowId);
      else next.add(rowId);
      return next;
    });
  }, []);

  /** 全选/清空 */
  const toggleCheckAll = () => {
    setChecked((prev) => (prev.size === rows.length ? new Set() : new Set(rows.map((r) => r.id))));
  };

  /** 资产列「+」：选中行并设置绑定目标（useCallback 稳定化供行组件，语义与原内联一致） */
  const handleStartBind = useCallback(
    (rowId: string, kind: ComicAssetKind) => {
      setSelectedRow(rowId);
      setBindingTarget({ rowId, kind });
    },
    [setSelectedRow, setBindingTarget],
  );

  /** 资产缩略图右上角 X：解绑（toggle 语义——已绑定即解绑；useCallback 稳定化供行组件） */
  const handleUnbindAsset = useCallback(
    (assetId: string, rowId: string) => {
      const assetName = assets.find((a) => a.asset_id === assetId)?.name ?? '资产';
      bindAssetToRow(assetId, rowId)
        .then(() => {
          const still = useMangaStore
            .getState()
            .rows.find((r) => r.id === rowId)
            ?.asset_ids?.includes(assetId);
          if (!still) showToast(`已解绑「${assetName}」`, 'success');
          else showToast(`已绑定「${assetName}」`, 'info');
        })
        .catch((err: unknown) => showToast(getErrorMessage(err, '解绑失败'), 'error'));
    },
    [assets, bindAssetToRow, showToast],
  );

  /** 批量操作（批量条；锁定行由 batchOps 计入 skipped） */
  const runBatch = async (kind: 'describe' | 'keyframe' | 'delete') => {
    if (!currentProject || batching) return;
    const targets = rows.filter((r) => checked.has(r.id));
    if (targets.length === 0) return;
    if (kind === 'delete') {
      const msg = targets.length === 1
        ? '确定删除该分镜？此操作不可恢复'
        : `确定删除选中的 ${targets.length} 个分镜？此操作不可恢复`;
      if (!window.confirm(msg)) return;
      applyStructure(rows.filter((r) => !checked.has(r.id)));
      setChecked(new Set());
      showToast(`已删除 ${targets.length} 镜`, 'success');
      return;
    }
    setBatching(true);
    const lockedCount = targets.filter((r) => r.is_locked).length;
    showToast(
      `${kind === 'describe' ? '批量生词' : '批量生图'}：处理 ${targets.length} 行${lockedCount ? `（${lockedCount} 行锁定自动跳过）` : ''}…`,
      'info',
    );
    try {
      const res = kind === 'describe'
        ? await batchDescribe(targets, currentProject.id)
        : await batchKeyframes(targets, currentProject.id);
      const parts = [`成功 ${res.done}`];
      if (res.skipped) parts.push(`跳过 ${res.skipped}`);
      if (res.failed) parts.push(`失败 ${res.failed}`);
      if (res.firstError) parts.push(`原因：${res.firstError}`);
      showToast(parts.join(' · '), res.failed ? 'warning' : 'success');
    } catch (err) {
      showToast(getErrorMessage(err, '批量执行失败'), 'error');
    } finally {
      setBatching(false);
    }
  };

  /** 行最新视频任务（按创建时间取最新一笔，不依赖后端排序方向） */
  const latestTask = (rowId: string) => {
    const list = videoTasks.filter((t) => t.row_id === rowId);
    if (!list.length) return undefined;
    return list.reduce((a, b) => ((Number(b.created_at ?? 0) > Number(a.created_at ?? 0)) ? b : a));
  };

  /** 原文列高亮名表：资产库角色名（行内与 row.characters 求并集；useMemo 稳定化，引用随 assets 变化才更新） */
  const characterNames = useMemo(() => assets.filter((a) => a.kind === 'character').map((a) => a.name), [assets]);

  return (
    <div className="manga-sb-wrap">
      {/* 批量条（勾选多行出现） */}
      {checked.size > 0 && (
        <div className="manga-batchbar">
          <span className="manga-batchbar-count">已选 {checked.size} 镜</span>
          <button type="button" className="btn btn-secondary btn-sm" disabled={batching} onClick={() => void runBatch('describe')}>
            <Wand2 size={13} />
            批量生词
          </button>
          <button type="button" className="btn btn-secondary btn-sm" disabled={batching} onClick={() => void runBatch('keyframe')}>
            <ImagePlus size={13} />
            批量生图
          </button>
          <button type="button" className="btn btn-secondary btn-sm manga-batchbar-danger" disabled={batching || structSaving} onClick={() => void runBatch('delete')}>
            <Trash2 size={13} />
            批量删除
          </button>
          <button type="button" className="manga-taskbar-action" style={{ marginLeft: 'auto' }} onClick={() => setChecked(new Set())}>
            清空选择
          </button>
        </div>
      )}

      <div className="manga-sb">
        {/* 表头 */}
        <div className="manga-sb-tr manga-sb-head">
          <div className="manga-sb-td c-check">
            <input
              type="checkbox"
              className="manga-checkbox"
              aria-label="全选分镜"
              checked={rows.length > 0 && checked.size === rows.length}
              onChange={toggleCheckAll}
            />
          </div>
          <div className="manga-sb-td c-idx">序号</div>
          <div className="manga-sb-td c-text">原文</div>
          <div className="manga-sb-td c-desc">分镜描述词</div>
          <div className="manga-sb-td c-char">出场人物</div>
          <div className="manga-sb-td c-scene">场景</div>
          <div className="manga-sb-td c-prop">道具</div>
          <div className="manga-sb-td c-image">分镜图</div>
          <div className="manga-sb-td c-video">视频</div>
          <div className="manga-sb-td c-ops">操作</div>
        </div>

        {/* 数据行（行级 memo：仅 props 变化的行重渲染；curKf/task 为数组元素引用，未更新时稳定） */}
        {rows.map((r, idx) => {
          const kfs = keyframesMap[r.id];
          return (
            <StoryboardRow
              key={r.id}
              row={r}
              isSelected={selectedRowId === r.id}
              isChecked={checked.has(r.id)}
              isFirst={idx === 0}
              isLast={idx === rows.length - 1}
              structSaving={structSaving}
              busyKey={busyKey}
              assets={assets}
              currentKeyframe={kfs?.find((k) => k.is_current)}
              videoTask={latestTask(r.id)}
              characterNames={characterNames}
              onSelectRow={setSelectedRow}
              onToggleCheck={toggleCheck}
              onDescribe={handleDescribe}
              onToggleLock={handleToggleLock}
              onUpdateField={updateField}
              onOpenAsset={setSelectedAsset}
              onStartBind={handleStartBind}
              onUnbindAsset={handleUnbindAsset}
              onOpenInspector={onOpenInspector}
              onMove={moveShot}
              onRemove={removeShot}
              onGenerateVideo={onGenerateVideo}
            />
          );
        })}

        {/* 新建分镜（底部居中虚线长条按钮） */}
        {rows.length < MAX_ROWS && (
          <div className="manga-row-create-wrap">
            <button type="button" className="manga-row-create" disabled={structSaving} onClick={addShot}>
              <Plus size={15} />
              新建分镜
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
