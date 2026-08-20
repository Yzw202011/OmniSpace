/* ==========================================================================
 * OmniSpace AI v2.3.1 —— 漫剧编辑器（竞品式工作台，含独有 3D 导演台）
 * --------------------------------------------------------------------------
 * 布局（参考竞品 yl.man-tui.com/cartoon 编辑器，视觉=深色渐变+樱粉霓虹）：
 *   顶栏：返回 + 作品名/保存状态 | 剧本导入 · 模型配置 · 视频记录 · 导出 · 3D 导演台 CTA
 *   工序箭头条（chevron，竞品对齐）：角色推理→角色生图→分镜生词→分镜生图→生成视频
 *     （解说漫剧 6 步：角色推理→角色生图→故事生词→故事生图→视频生词→生成视频）
 *     点击=真实动作（实体推理/聚焦资产/批量生词/批量生图/视频确认弹窗），状态由真实数据推导
 *   视频任务条：真实轮询进度，支持取消/下载，降级如实标注（BK-014）
 *   主区：中央表格式分镜（StoryboardTable）｜右栏槽位
 *     右栏槽位优先级互斥：抽屉 > 资产详情(AssetDetailPanel) > 行检查器 > 资产面板(AssetDock)
 *   工序条工具：提示词设置（PromptModal）/ 视频生成记录（RecordsModal）；
 *     批量工序确认（BatchConfirmModal）含统计/提示词入口/实时进度；
 *     模型配置（ModelConfigModal）/ 生成视频确认（VideoConfirmModal）竞品居中弹层
 *   3D 导演台：全屏覆盖层（竞品没有的能力，顶栏主 CTA + 行内入口）
 * 空项目（0 行）首入：整页剧本录入（ScriptImport），可跳过。
 * ========================================================================== */

import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  ChevronLeft,
  Clapperboard,
  Download,
  FileUp,
  History,
  PlayCircle,
  ScrollText,
  Settings2,
  X,
} from 'lucide-react';
import { useAppStore } from '@/stores/useAppStore';
import { useMangaStore } from '@/stores/useMangaStore';
import { inferEntities } from '@/services/mangaApi';
import { SAVE_STATUS_LABELS, VIDEO_STATUS_LABELS } from '@/constants/statusLabels';
import type { ShotSaveStatus, StoryboardRow } from '@/types';
import { ScriptImport } from './ScriptImport';
import { DirectorStage } from './DirectorStage';
import { VoiceBinder } from './VoiceBinder';
import { DrawerFrame } from './drawers/DrawerFrame';
import { ImportDrawer } from './drawers/ImportDrawer';
import { VideoDrawer } from './drawers/VideoDrawer';
import { ExportDrawer } from './drawers/ExportDrawer';
import AssetDock from './editor/AssetDock';
import AssetDetailPanel from './editor/AssetDetailPanel';
import InspectorPanel from './editor/InspectorPanel';
import StoryboardTable from './editor/StoryboardTable';
import BatchConfirmModal from './editor/BatchConfirmModal';
import PromptModal from './editor/PromptModal';
import RecordsModal from './editor/RecordsModal';
import ModelConfigModal from './editor/ModelConfigModal';
import VideoConfirmModal from './editor/VideoConfirmModal';

/** 抽屉种类（"" 关闭，单开互斥，占用右栏槽位） */
type DrawerKind = '' | 'import' | 'video' | 'export' | 'voice';

/** 分镜行数硬上限（后端 STORYBOARD_MAX_ROWS） */
const MAX_ROWS = 50;

export default function MangaWorkspace() {
  const showToast = useAppStore((s) => s.showToast);
  const currentProject = useMangaStore((s) => s.currentProject);
  const closeProject = useMangaStore((s) => s.closeProject);
  const rows = useMangaStore((s) => s.rows);
  const loading = useMangaStore((s) => s.loading);
  const updateRow = useMangaStore((s) => s.updateRow);
  const generateVideo = useMangaStore((s) => s.generateVideo);
  const assets = useMangaStore((s) => s.assets);
  const assetsLoaded = useMangaStore((s) => s.assetsLoaded);
  const fetchAssets = useMangaStore((s) => s.fetchAssets);
  const videoTasks = useMangaStore((s) => s.videoTasks);
  const removeVideoTask = useMangaStore((s) => s.removeVideoTask);
  const cancelVideo = useMangaStore((s) => s.cancelVideo);
  const keyframesMap = useMangaStore((s) => s.keyframes);
  const selectedRowId = useMangaStore((s) => s.selectedRowId);
  const setSelectedRow = useMangaStore((s) => s.setSelectedRow);
  const selectedAssetId = useMangaStore((s) => s.selectedAssetId);
  const setSelectedAsset = useMangaStore((s) => s.setSelectedAsset);
  const setBindingTarget = useMangaStore((s) => s.setBindingTarget);

  /** 右栏抽屉（"" 关闭） */
  const [drawer, setDrawer] = useState<DrawerKind>('');
  /** 空项目剧本录入页：跳过后不再展示 */
  const [importSkipped, setImportSkipped] = useState(false);
  /** 3D 导演台覆盖层（独立于抽屉，全屏） */
  const [directorOpen, setDirectorOpen] = useState(false);
  /** 导演台联动分镜行 */
  const [directorShotId, setDirectorShotId] = useState<string | undefined>(undefined);
  /** 分镜表保存状态（顶栏状态点） */
  const [saveStatus, setSaveStatus] = useState<ShotSaveStatus>('idle');
  /** 行检查器打开的分镜行（null=右栏显示资产面板） */
  const [inspectRowId, setInspectRowId] = useState<string | null>(null);
  /** 批量工序确认（chevron 3/4/5） */
  const [confirmBatch, setConfirmBatch] = useState<'' | 'describe' | 'keyframe' | 'story_narrative' | 'story_keyframe' | 'video_narrative'>('');
  /** 提示词设置弹窗 */
  const [promptOpen, setPromptOpen] = useState(false);
  /** 视频生成记录弹窗 */
  const [recordsOpen, setRecordsOpen] = useState(false);
  /** 模型配置弹窗（顶栏入口，竞品居中弹层） */
  const [modelCfgOpen, setModelCfgOpen] = useState(false);
  /** 生成视频确认弹窗：null=关闭，数组=待生成的缺视频分镜行 ID（工序⑤） */
  const [videoConfirmIds, setVideoConfirmIds] = useState<string[] | null>(null);
  /** 角色推理执行中（工序①防重入） */
  const [inferring, setInferring] = useState(false);

  // 打开项目时预取资产（右栏资产面板 + 工序条状态判定）
  useEffect(() => {
    if (!currentProject || assetsLoaded) return;
    fetchAssets().catch(() => showToast('资产列表加载失败', 'error'));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentProject?.id]);

  // 抽屉开关切换（再次点击同一按钮 = 关闭；打开抽屉时清空资产详情/行检查器，槽位互斥）
  const toggleDrawer = useCallback(
    (kind: DrawerKind) => {
      setDrawer((prev) => {
        const next = prev === kind ? '' : kind;
        if (next) {
          setSelectedAsset(null);
          setInspectRowId(null);
        }
        return next;
      });
    },
    [setSelectedAsset],
  );

  // 打开导演台（可携带行 ID 联动镜头）
  const openDirector = useCallback((rowId?: string) => {
    setDirectorShotId(rowId);
    setDirectorOpen(true);
  }, []);

  // 导演台关闭：按真实操作标记该分镜行「导演台已完成」（PUT 单行持久化）
  const closeDirector = useCallback(() => {
    setDirectorOpen(false);
    if (directorShotId) {
      updateRow(directorShotId, { director_stage_done: true }).catch(() => undefined);
      setDirectorShotId(undefined);
    }
  }, [directorShotId, updateRow]);

  // 打开行检查器（右栏槽位：清空资产详情/绑定目标，与抽屉互斥）
  const openInspector = useCallback(
    (rowId: string) => {
      setSelectedRow(rowId);
      setInspectRowId(rowId);
      setSelectedAsset(null);
      setBindingTarget(null);
      setDrawer('');
    },
    [setSelectedRow, setSelectedAsset, setBindingTarget],
  );

  // 行内 🎥 → 视频生成
  const handleGenerateVideo = useCallback(
    (row: StoryboardRow) => {
      void generateVideo(row);
    },
    [generateVideo],
  );

  // 取消视频任务（toast 透出失败原因）
  const handleCancelVideo = useCallback(
    (taskId: string) => {
      cancelVideo(taskId).catch((err) => {
        const msg = err && typeof err === 'object' && 'message' in err ? (err as { message: string }).message : '取消失败';
        showToast(msg, 'error');
      });
    },
    [cancelVideo, showToast],
  );

  // 工序①角色推理：POST /comic/asset/infer-entities 从分镜行聚合角色/场景/道具资产桩
  const handleInferEntities = useCallback(async () => {
    if (!currentProject || inferring) return;
    // 无剧本原文（0 行）→ 引导先导入剧本并开导入抽屉
    if (rows.length === 0) {
      showToast('请先导入剧本原文，再执行角色推理', 'warning');
      setDrawer('import');
      return;
    }
    // 已有资产时二次确认（后端按 (kind,name) 去重复用，不覆盖已有资产）
    if (assets.length > 0 && !window.confirm('已有资产，将按剧本重新推断角色清单（已有资产保留），是否继续？')) {
      return;
    }
    setInferring(true);
    try {
      const res = await inferEntities(currentProject.id);
      await fetchAssets().catch(() => undefined);
      // 后端响应 items 仅含本次新建的资产桩，按 kind 统计「新增 N 个角色/场景/道具」
      // （注：mangaApi 声明的 created/reused 与后端实际形状不符，以 items 为可靠来源）
      const counts = { character: 0, scene: 0, prop: 0 };
      for (const it of res.items) {
        if (it.kind === 'character' || it.kind === 'scene' || it.kind === 'prop') counts[it.kind] += 1;
      }
      const total = counts.character + counts.scene + counts.prop;
      if (total === 0) {
        showToast('角色推理完成：未发现新实体，资产清单已是最新', 'info');
      } else {
        showToast(
          `角色推理完成：新增 ${counts.character} 个角色 · ${counts.scene} 个场景 · ${counts.prop} 个道具`,
          'success',
        );
      }
      // 聚焦资产面板槽位，展示推理结果
      setDrawer('');
      setInspectRowId(null);
      setSelectedAsset(null);
    } catch (err) {
      const msg = err && typeof err === 'object' && 'message' in err ? (err as { message: string }).message : '角色推理失败';
      showToast(msg, 'error');
    } finally {
      setInferring(false);
    }
  }, [currentProject, inferring, rows.length, assets.length, showToast, fetchAssets, setSelectedAsset]);

  // 工序⑤生成视频：统计缺视频的分镜行，0 条 toast，否则打开 VideoConfirmModal
  const handleVideoStage = useCallback(() => {
    if (rows.length === 0) {
      showToast('请先导入剧本原文，再生成视频', 'warning');
      setDrawer('import');
      return;
    }
    // 缺视频 = 无视频任务（pending）或上次失败（error，可重试）；
    // generating 在途 / done 已完成 / skipped 跳过 均不重复提交
    const ids = rows
      .filter((r) => r.generation_status === 'pending' || r.generation_status === 'error')
      .map((r) => r.id);
    if (ids.length === 0) {
      showToast('所有分镜均已有视频', 'info');
      return;
    }
    setVideoConfirmIds(ids);
  }, [rows, showToast]);

  /** 工序箭头（chevron，竞品对齐）：状态由行/资产真实数据推导，点击=真实动作；G1：根据 work_mode 动态渲染 5/6 步 */
  const stages = useMemo(() => {
    const describeDone = rows.length > 0 && rows.every((r) => r.description.trim());
    const keyframeDone = rows.length > 0 && rows.every((r) => (keyframesMap[r.id] ?? []).some((k) => k.is_current));
    const videoDone = rows.length > 0 && rows.every((r) => r.generation_status === 'done' || r.generation_status === 'skipped');
    // 角色推理完成 = 已有资产清单（实体桩已推断）
    const inferDone = assets.length > 0;
    // 角色生图完成 = 全部资产均已有图（资产桩 file_path 为空 = 无图，AssetDock 显示「无图像」）
    const assetImagesDone = assets.length > 0 && assets.every((a) => a.file_path);
    const isNarrative = currentProject?.work_mode === 'narrative';
    // 工序②角色生图：清空 selectedAssetId/inspectRowId/抽屉 → 右栏回落资产面板槽位
    const focusAssets = () => {
      setDrawer('');
      setInspectRowId(null);
      setSelectedAsset(null);
    };

    let list: { label: string; done: boolean; action: () => void }[];
    if (isNarrative) {
      // 解说漫剧 6 步：角色推理→角色生图→故事生词→故事生图→视频生词→生成视频
      const videoDescDone = rows.length > 0 && rows.every((r) => r.description.trim());
      list = [
        { label: '角色推理', done: inferDone, action: () => void handleInferEntities() },
        { label: '角色生图', done: assetImagesDone, action: focusAssets },
        { label: '故事生词', done: describeDone, action: () => setConfirmBatch('story_narrative') },
        { label: '故事生图', done: keyframeDone, action: () => setConfirmBatch('story_keyframe') },
        { label: '视频生词', done: videoDescDone, action: () => setConfirmBatch('video_narrative') },
        { label: '生成视频', done: videoDone, action: handleVideoStage },
      ];
    } else {
      // 普通漫剧 5 步（竞品对齐）：角色推理→角色生图→分镜生词→分镜生图→生成视频
      list = [
        { label: '角色推理', done: inferDone, action: () => void handleInferEntities() },
        { label: '角色生图', done: assetImagesDone, action: focusAssets },
        { label: '分镜生词', done: describeDone, action: () => setConfirmBatch('describe') },
        { label: '分镜生图', done: keyframeDone, action: () => setConfirmBatch('keyframe') },
        { label: '生成视频', done: videoDone, action: handleVideoStage },
      ];
    }
    const currentIndex = list.findIndex((s) => !s.done);
    return { list, currentIndex: currentIndex === -1 ? list.length - 1 : currentIndex };
     
  }, [rows, assets, keyframesMap, currentProject?.work_mode, handleInferEntities, handleVideoStage, setSelectedAsset]);

  // 右栏槽位互斥：资产详情打开 → 关闭行检查器/抽屉（最新点击胜出）
  useEffect(() => {
    if (!selectedAssetId) return;
    setInspectRowId(null);
    setDrawer('');
  }, [selectedAssetId]);

  // 右栏槽位互斥：绑定模式进入 → 让位资产面板（关闭抽屉/检查器/资产详情）
  const bindingTarget = useMangaStore((s) => s.bindingTarget);
  useEffect(() => {
    if (!bindingTarget) return;
    setDrawer('');
    setInspectRowId(null);
    setSelectedAsset(null);
  }, [bindingTarget, setSelectedAsset]);

  if (!currentProject) return null;

  // 空项目首入：整页剧本录入（跳过或完成后进入编辑器）
  if (!loading && rows.length === 0 && !importSkipped) {
    return <ScriptImport onDone={() => setImportSkipped(true)} />;
  }

  const doneCount = rows.filter((r) => r.generation_status === 'done').length;

  return (
    <div className="manga-wb">
      {/* ① 顶栏（竞品单行融合：返回/作品名 + 工序步骤条 + 右侧按钮） */}
      <div className="manga-wb-toolbar">
        <button type="button" className="btn-icon" title="返回作品库" aria-label="返回作品库" onClick={closeProject}>
          <ChevronLeft size={18} />
        </button>
        <div className="manga-wb-headline">
          <div className="flex items-center gap-2" style={{ minWidth: 0 }}>
            <span className="manga-wb-title ellipsis" title={currentProject.name}>
              《{currentProject.name}》
            </span>
            {saveStatus !== 'idle' && (
              <span className={`manga-save-dot${saveStatus === 'dirty' ? ' dirty' : saveStatus === 'saving' ? ' saving' : ''}`}>
                {SAVE_STATUS_LABELS[saveStatus]}
              </span>
            )}
          </div>
          <div className="manga-wb-sub">
            {rows.length}/{MAX_ROWS} 镜 · 视频 {doneCount}/{rows.length} 已完成
          </div>
        </div>

        {/* 工序箭头条（竞品 chevron，内嵌顶栏中部）：点击=真实动作 */}
        <div className="manga-pipe">
          {stages.list.map((s, i) => (
            <button
              key={s.label}
              type="button"
              className={`manga-pipe-step${s.done ? ' done' : i === stages.currentIndex ? ' current' : ''}`}
              title={`${s.label}${s.done ? '（已完成，点击可再次执行）' : '（点击执行）'}`}
              onClick={s.action}
            >
              <span className="manga-pipe-num">{i + 1}</span>
              {s.label}
            </button>
          ))}
        </div>

        <div className="manga-wb-actions">
          <button
            type="button"
            className="manga-pipe-tool"
            title="AI 生词提示词设置（默认/自定义前缀）"
            onClick={() => setPromptOpen(true)}
          >
            <ScrollText size={13} />
            提示词
          </button>
          <button
            type="button"
            className="manga-pipe-tool"
            title="项目视频生成记录（下载/重试）"
            onClick={() => setRecordsOpen(true)}
          >
            <History size={13} />
            生成记录
          </button>
          <button
            type="button"
            className="manga-pipe-tool"
            title="模型配置（推理/绘画/视频模型 + 视频默认画幅/时长）"
            onClick={() => setModelCfgOpen(true)}
          >
            <Settings2 size={13} />
            模型配置
          </button>
          <button
            type="button"
            className={`btn-icon${drawer === 'import' ? ' active' : ''}`}
            title="剧本导入"
            aria-label="剧本导入"
            onClick={() => toggleDrawer('import')}
          >
            <FileUp size={16} />
          </button>
          <button
            type="button"
            className={`btn-icon${drawer === 'video' ? ' active' : ''}`}
            title="视频任务"
            aria-label="视频任务"
            onClick={() => toggleDrawer('video')}
          >
            <PlayCircle size={16} />
          </button>
          <button
            type="button"
            className={`btn-icon${drawer === 'export' ? ' active' : ''}`}
            title="导出"
            aria-label="导出"
            onClick={() => toggleDrawer('export')}
          >
            <Download size={16} />
          </button>
          <button type="button" className="btn btn-primary btn-sm" onClick={() => openDirector(undefined)} title="打开 3D 导演台（机位/灯光/运动）">
            <Clapperboard size={14} />
            3D 导演台
          </button>
        </div>
      </div>

      {/* ③ 视频任务条 */}
      {videoTasks.length > 0 && (
        <div className="manga-taskbar">
          {videoTasks.map((t) => (
            <div key={t.task_id} className="manga-taskbar-row">
              <span className="text-secondary" style={{ flexShrink: 0 }}>
                分镜{t.shot_number}
              </span>
              <span className="text-tertiary ellipsis" style={{ maxWidth: 200 }} title={t.description}>
                {t.description}
              </span>
              <span className="flex-1 rounded" style={{ height: 6, background: 'var(--color-input-bg)', overflow: 'hidden' }}>
                <span
                  className="block h-full"
                  style={{
                    width: `${Math.round(t.progress * 100)}%`,
                    background: 'var(--color-primary)',
                    transition: 'width var(--duration-normal) var(--ease-out)',
                  }}
                />
              </span>
              <span className="text-secondary" style={{ flexShrink: 0, width: 96 }}>
                {VIDEO_STATUS_LABELS[t.status] ?? t.status}
                {t.status === 'generating' && ` ${Math.round(t.progress * 100)}%`}
              </span>
              {t.degraded && (
                <span className="badge warning" style={{ flexShrink: 0 }} title={t.degrade_reason || '降级管线产出'}>
                  降级
                </span>
              )}
              {t.status === 'error' && (
                <span className="text-error ellipsis" style={{ maxWidth: 160 }} title={t.error}>
                  {t.error || '生成失败'}
                </span>
              )}
              {(t.status === 'generating' || t.status === 'pending') && (
                <button type="button" className="manga-taskbar-action warn" title="取消任务" onClick={() => handleCancelVideo(t.task_id)}>
                  取消
                </button>
              )}
              {t.status === 'done' && t.download_url && (
                <a className="manga-taskbar-link" href={t.download_url} download>
                  下载 MP4
                </a>
              )}
              {(t.status === 'done' || t.status === 'error') && (
                <button type="button" className="manga-taskbar-action" title="移除记录" onClick={() => removeVideoTask(t.task_id)}>
                  <X size={13} aria-hidden="true" />
                </button>
              )}
            </div>
          ))}
        </div>
      )}

      {/* ④ 主区：中央表格式分镜 + 右栏槽位 */}
      <div className="manga-ed-body">
        <div className="manga-ed-center">
          {loading ? (
            <div className="loading-block" style={{ height: 160 }}>
              <span className="spinner" />
              分镜加载中…
            </div>
          ) : (
            <StoryboardTable
              onSaveStatus={setSaveStatus}
              onOpenDirector={openDirector}
              onGenerateVideo={handleGenerateVideo}
              onOpenInspector={openInspector}
            />
          )}
        </div>
        {/* 右栏槽位优先级互斥：抽屉 > 资产详情 > 行检查器 > 资产面板 */}
        {drawer === 'import' && <ImportDrawer onClose={() => setDrawer('')} />}
        {drawer === 'video' && <VideoDrawer onClose={() => setDrawer('')} />}
        {drawer === 'export' && <ExportDrawer onClose={() => setDrawer('')} />}
        {drawer === 'voice' && (
          <DrawerFrame title="音色绑定" subtitle={selectedRowId ? '· 已选中分镜行' : ''} onClose={() => setDrawer('')}>
            <VoiceBinder />
          </DrawerFrame>
        )}
        {!drawer && selectedAssetId && <AssetDetailPanel />}
        {!drawer && !selectedAssetId && inspectRowId && (
          <InspectorPanel onBack={() => setInspectRowId(null)} onOpenVoice={() => setDrawer('voice')} />
        )}
        {!drawer && !selectedAssetId && !inspectRowId && <AssetDock />}
      </div>

      {/* ⑤ 3D 导演台：全屏覆盖层 */}
      {directorOpen && (
        <div className="manga-director-overlay">
          <DirectorStage
            projectId={currentProject.id}
            shotId={directorShotId}
            onClose={closeDirector}
            onScreenshot={() => showToast('导演台截图已生成', 'success')}
          />
        </div>
      )}

      {/* ⑥ 批量工序确认（chevron 3/4，含提示词入口与实时进度） */}
      {confirmBatch && (
        <BatchConfirmModal kind={confirmBatch} onClose={() => setConfirmBatch('')} />
      )}

      {/* ⑦ 提示词设置（工序条工具） */}
      {promptOpen && <PromptModal onClose={() => setPromptOpen(false)} />}

      {/* ⑧ 视频生成记录（工序条工具） */}
      {recordsOpen && <RecordsModal onClose={() => setRecordsOpen(false)} />}

      {/* ⑨ 模型配置（顶栏入口，竞品居中弹层） */}
      {modelCfgOpen && <ModelConfigModal onClose={() => setModelCfgOpen(false)} />}

      {/* ⑩ 生成视频确认（工序⑤，竞品确认层；rowIds 已筛选缺视频行） */}
      <VideoConfirmModal
        open={videoConfirmIds !== null}
        rowIds={videoConfirmIds ?? []}
        onClose={() => setVideoConfirmIds(null)}
      />
    </div>
  );
}
