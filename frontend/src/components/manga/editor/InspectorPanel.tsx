// 本项目仅供学习使用，商业授权请+Q 3559331368
/* ==========================================================================
 * InspectorPanel.tsx —— 漫剧编辑器右栏·分镜详情面板（竞品 yl.man-tui.com 对齐）
 * --------------------------------------------------------------------------
 * 2026-08-25 用户裁定重构：对齐竞品「分镜详情」形态（同 AssetDetailPanel
 * 范式），移除旧「AI 助手 / 绑定与音色」区块——资产绑定归分镜表资产列，
 * 音色绑定降级为底部轻量入口。现结构：
 *   1. 头部：← 返回 + 「镜 N 详情」+ 生成状态徽标 + 关闭
 *   2. 预览区：当前关键帧大图（点击开灯箱）；无图空态引导编辑描述词
 *   3. 描述词：textarea 失焦保存 + 「✦ AI 生成描述」按钮
 *   4. AI 生图主按钮（全宽 btn-primary：无版生成本 / 有版重新生成）
 *   5. 历史记录折叠区：关键帧版本列表（当前徽标 / 回退 / 删除）
 *   6. 底部：音色绑定轻量入口（音色抽屉唯一入口，保留）
 * AI 前置守卫：行未持久化（本地新建行）时先全量保存再调单行端点。
 * ========================================================================== */

import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowLeft, ChevronDown, History, ImagePlus, Mic, RotateCcw, Sparkles, Trash2, X } from 'lucide-react';
import { useAppStore } from '@/stores/useAppStore';
import { useMangaStore } from '@/stores/useMangaStore';
import {
  aiDescribe,
  deleteKeyframe,
  generateKeyframe,
  getMediaUrl,
  regenerateKeyframe,
  replaceKeyframeImage,
  rollbackKeyframe,
} from '@/services/mangaApi';
import { ROW_GEN_STATUS_LABELS } from '@/constants/statusLabels';
import { getErrorMessage, reportActionError, reportBgError } from '@/utils/errors';
import { warmupFeature } from '@/services/modelApi';
import { useWarmupStore } from '@/stores/useWarmupStore';
import { readPromptPrefix } from './batchOps';
import { waitPaintReady } from './waitPaintReady';
import { useGenProgress } from './useGenProgress';
import AssetLightbox from './AssetLightbox';

function formatTime(ts?: number) {
  if (!ts) return '';
  const d = new Date(ts > 1e12 ? ts : ts * 1000);
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleString();
}

/* 错误消息提取/呈现统一走 @/utils/errors（TASK-P2-08） */

export default function InspectorPanel({ onBack, onOpenVoice }: { onBack?: () => void; onOpenVoice: () => void }) {
  const showToast = useAppStore((s) => s.showToast);
  const currentProject = useMangaStore((s) => s.currentProject);
  const rows = useMangaStore((s) => s.rows);
  const selectedRowId = useMangaStore((s) => s.selectedRowId);
  const setSelectedRow = useMangaStore((s) => s.setSelectedRow);
  const updateRow = useMangaStore((s) => s.updateRow);
  const saveRows = useMangaStore((s) => s.saveRows);
  const keyframes = useMangaStore((s) => (selectedRowId ? s.keyframes[selectedRowId] : undefined));
  const fetchKeyframes = useMangaStore((s) => s.fetchKeyframes);
  const invalidateKeyframes = useMangaStore((s) => s.invalidateKeyframes);

  const row = rows.find((r) => r.id === selectedRowId);

  const [busy, setBusy] = useState<'describe' | ''>('');
  const [descDraft, setDescDraft] = useState('');
  const [generating, setGenerating] = useState(false);
  const [busyId, setBusyId] = useState('');
  const [lightbox, setLightbox] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  // 替换图片（2026-10-07 用户令）：本地上传 → 新版本落库
  const replaceFileRef = useRef<HTMLInputElement>(null);
  const [replacing, setReplacing] = useState(false);
  // WS 实时进度（后端逐镜/采样步级广播 → 按钮内进度条）
  const genProgress = useGenProgress('keyframe', selectedRowId ?? undefined, generating);
  // 刷新恢复态收敛（2026-10-12）：恢复的 generating 由 WS done/error 事件
  // 关闸（正常生成流自有 await 收敛，不经过这里）
  useEffect(() => {
    if (genProgress?.status === 'done' || genProgress?.status === 'error') {
      setGenerating(false);
    }
  }, [genProgress?.status]);

  // 选中行切换：拉取关键帧 + 描述词草稿跟随行数据
  const lastSyncedDescRef = useRef('');
  useEffect(() => {
    if (!selectedRowId) return;
    const desc = row?.description ?? '';
    lastSyncedDescRef.current = desc;
    setDescDraft(desc);
    fetchKeyframes(selectedRowId).catch((err) =>
      reportBgError('InspectorPanel.fetchKeyframes', err),
    );
    // 刷新/重进项目恢复生成中态（2026-10-12 用户令）：图像队列里有本行
    // 的关键帧在飞任务 → 恢复 generating，进度由 WS task_progress 续播
    // （后端广播跨页面存活），done 事件经下方监听收敛
    let alive = true;
    import('@/services/mangaApi').then(({ getPaintQueue }) => getPaintQueue())
      .then((snap) => {
        if (!alive) return;
        const prefix = `kf:${selectedRowId.slice(0, 12)}:`;
        const inFlight = [...snap.running, ...snap.pending].some(
          (t) => (t.kind === 'keyframe' || String(t.task_id).startsWith('kf:'))
            && String(t.task_id).startsWith(prefix),
        );
        if (inFlight) setGenerating(true);
      })
      .catch(() => { /* 探测失败静默（生成中态丢一次刷新感知） */ });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedRowId]);

  // 行描述词被外部更新（行内列编辑/AI 生成/保存回写）→ 草稿跟随刷新。
  // 脏检查：用户正在本面板编辑（草稿≠上次同步值）时不覆盖，防打断输入
  //（2026-10-07 用户实报：行内改了描述词，右侧详情仍显示旧词）
  useEffect(() => {
    const desc = row?.description ?? '';
    // 行数据瞬断保护（2026-10-07 实弹踩雷）：rows 重置/轮询空窗时
    // desc 短暂为空——此时清草稿会让后续失焦把空值写库（描述词被静默
    // 清空→原文直出兜底）。行数据回来后本 effect 会正常跟随
    if (!desc && lastSyncedDescRef.current) return;
    if (desc === lastSyncedDescRef.current) return;
    if (descDraft === lastSyncedDescRef.current) setDescDraft(desc);
    lastSyncedDescRef.current = desc;
  }, [row?.description, descDraft]);

  /** AI 前置：本地新建行先全量持久化（单行端点要求服务端已有该行） */
  const ensurePersisted = useCallback(async () => {
    if (selectedRowId?.startsWith('row_')) {
      await saveRows(useMangaStore.getState().rows);
    }
  }, [selectedRowId, saveRows]);

  /** 描述词提交（可等待）：与行数据不同才提交，失败向上抛 */
  const commitDescriptionAwait = useCallback((): Promise<void> => {
    if (!row) return Promise.resolve();
    // 空草稿防误清（2026-10-07 实弹踩雷）：草稿为空而行值非空=瞬断/
    // 异常态，不提交（真想清空走整表编辑；空描述词会触发原文直出兜底）
    if (!descDraft.trim() && row.description.trim()) return Promise.resolve();
    if (descDraft === row.description) return Promise.resolve();
    return updateRow(row.id, { description: descDraft, is_ai_generated: false })
      .then(() => undefined)
      .catch((err: unknown) => {
        reportActionError(err, '描述词保存');
        throw err;
      });
  }, [row, descDraft, updateRow]);

  /** 描述词失焦保存（fire-and-forget，错误已 toast） */
  const commitDescription = useCallback(() => {
    commitDescriptionAwait().catch(() => { /* 已 reportActionError */ });
  }, [commitDescriptionAwait]);

  /** 替换图片（2026-10-07 用户令）：选本地图 → data URL 上传 →
   *  新版本落库为当前版；成功后刷新版本列表。FileReader 异步读入，
   *  上传失败 toast 具体原因。 */
  const handleReplaceFile = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (!file || !selectedRowId) return;
    if (file.size > 30 * 1024 * 1024) {
      showToast('图片过大（超过 30MB），请压缩后再试', 'warning');
      return;
    }
    setReplacing(true);
    const reader = new FileReader();
    reader.onerror = () => {
      setReplacing(false);
      showToast('图片读取失败', 'error');
    };
    reader.onload = () => {
      replaceKeyframeImage(selectedRowId, String(reader.result))
        .then((r) => {
          showToast(`已替换为当前版本 v${r.version}`, 'success');
          invalidateKeyframes(selectedRowId);
          return fetchKeyframes(selectedRowId);
        })
        .catch((err: unknown) =>
          showToast(getErrorMessage(err, '替换失败'), 'error'))
        .finally(() => setReplacing(false));
    };
    reader.readAsDataURL(file);
  }, [selectedRowId, showToast, invalidateKeyframes, fetchKeyframes]);

  const handleDescribe = useCallback(() => {
    if (!currentProject || !row) return;
    if (!row.original_dialogue.trim()) {
      showToast('该行没有台词，AI 描述需要原始台词作为输入', 'warning');
      return;
    }
    // 2026-08-25 用户裁定：未绑定资产的行不允许生成分镜描述词（后端同门槛双保险）
    if (!(row.asset_ids?.length ?? 0) && !row.asset_id) {
      showToast('该行未绑定资产，先在分镜表资产列绑定角色/场景/道具', 'warning');
      return;
    }
    setBusy('describe');
    ensurePersisted()
      .then(() => aiDescribe(row.id, currentProject.id, readPromptPrefix()))
      .then((description) => {
        if (description) {
          setDescDraft(description);
          // 描述词落库失败会让「已生成」toast 变成误导，必须 TOAST 级透出
          updateRow(row.id, { description, is_ai_generated: true }).catch((err) =>
            reportActionError(err, '描述词保存'),
          );
        }
        showToast('AI 描述已生成', 'success');
      })
      .catch((err: unknown) => showToast(getErrorMessage(err, 'AI 描述生成失败'), 'error'))
      .finally(() => setBusy(''));
  }, [currentProject, row, ensurePersisted, updateRow, showToast]);

  // 自动续跑中标记：等待模型加载期间保持「生成中」按钮态，防止 finally 提前收敛
  const autoRetryingRef = useRef(false);

  const handleGenerateKeyframe = useCallback(
    () => {
      if (!currentProject || !selectedRowId) return;
      // 有历史版本 = 重生成（产出新版本，旧版保留可回退），否则首版生成
      const existing = useMangaStore.getState().keyframes[selectedRowId];
      const regenerate = (existing?.length ?? 0) > 0;

      const runOnce = (allowAutoRetry: boolean) => {
        setGenerating(true);
        // 描述词先落库再生成（2026-10-07 用户令修竞态）：打完字不失焦
        // 直接点生成时，后端曾按旧描述词出图且无任何提示——保存失败
        // 则中止生成（autoRetry 正则不匹配此错误，不会重试）
        commitDescriptionAwait()
          .then(() => ensurePersisted())
          .then(() =>
            regenerate
              ? regenerateKeyframe({ row_id: selectedRowId, project_id: currentProject.id })
              : generateKeyframe({ row_id: selectedRowId, project_id: currentProject.id }),
          )
          .then(() => {
            showToast(regenerate ? '已重新生成新版本' : '分镜图已生成', 'success');
            invalidateKeyframes(selectedRowId);
            return fetchKeyframes(selectedRowId);
          })
          .catch((err: unknown) => {
            const msg = getErrorMessage(err, '分镜图生成失败');
            // 模型未加载类失败（2026-08-31 用户需求「立刻加载+告知，
            // 且不要用户再点一次」）：立即点火后台预热 + 弹加载进度
            // 弹窗，就绪后自动重试一次本次生成
            if (allowAutoRetry && /MODEL_LOAD_FAILED|PAINT_ENGINE_NOT_READY|绘画模型未就绪|模型未加载|未处于已加载/.test(msg)) {
              autoRetryingRef.current = true;
              showToast(
                `${msg}。正在自动加载绘画模型（约 0.5-2 分钟），就绪后将自动继续生成，无需重新点击`,
                'info',
              );
              void warmupFeature('paint')
                .then((r) => {
                  if (r?.started) useWarmupStore.getState().begin(undefined, 'paint');
                })
                .catch(() => { /* 预热点火失败静默：等待窗口内后端任务会再兜底装载 */ });
              void waitPaintReady(150_000).then((ready) => {
                autoRetryingRef.current = false;
                if (ready) {
                  runOnce(false); // 就绪 → 自动续跑（仅一次，防循环）
                } else {
                  setGenerating(false);
                  showToast(
                    `${msg}。等待模型加载超时：可稍后再点一次「生成」，`
                    + '或到「模型管理 → AI 绘画」手动加载',
                    'warning',
                  );
                }
              });
            } else {
              showToast(msg, 'error');
            }
          })
          .finally(() => {
            if (!autoRetryingRef.current) setGenerating(false);
          });
      };

      runOnce(true);
    },
    [currentProject, selectedRowId, ensurePersisted, invalidateKeyframes, fetchKeyframes, showToast],
  );

  const handleKeyframeOp = useCallback(
    (keyframeId: string, version: number, op: 'rollback' | 'delete') => {
      if (!selectedRowId) return;
      setBusyId(keyframeId);
      const call = op === 'rollback' ? rollbackKeyframe(keyframeId) : deleteKeyframe(keyframeId);
      call
        .then(() => {
          showToast(op === 'rollback' ? `已回退到 v${version}` : `已删除 v${version}`, 'success');
          invalidateKeyframes(selectedRowId);
          return fetchKeyframes(selectedRowId);
        })
        .catch((err: unknown) => showToast(getErrorMessage(err, op === 'rollback' ? '回退失败' : '删除失败'), 'error'))
        .finally(() => setBusyId(''));
    },
    [selectedRowId, invalidateKeyframes, fetchKeyframes, showToast],
  );

  if (!currentProject || !row) return null;

  const versions = keyframes ?? [];
  const hasVersions = versions.length > 0;
  /** 当前关键帧：is_current 优先，回退首版 */
  const current = versions.find((k) => k.is_current) ?? versions[0];
  const currentUrl = current?.file_path
    ? getMediaUrl(current.file_path, `${current.version}-${current.created_at}`)
    : '';

  /** 右键下载（2026-10-07 用户令：分镜图预览/历史缩略图右击可存）
   *  fetch→blob 下载（a.download 跨源 URL 会忽略文件名，走 blob 保名） */
  const downloadByUrl = (url: string, filename: string) => {
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
      .catch((err) => reportBgError('InspectorPanel.download', err));
  };

  return (
    <aside className="manga-ed-inspector">
      {/* 1. 头部：← 返回 + 标题 + 状态 + 关闭 */}
      <div className="manga-ed-inspector-head">
        <div className="flex items-center" style={{ gap: 'var(--space-1)', minWidth: 0 }}>
          <button
            type="button"
            className="btn-icon"
            style={{ width: 24, height: 24 }}
            title="返回"
            aria-label="返回"
            onClick={() => { setSelectedRow(null); onBack?.(); }}
          >
            <ArrowLeft size={14} />
          </button>
          <h3 className="manga-ed-inspector-title ellipsis">
            镜 {row.shot_number} 详情
            <span className={`badge ${row.generation_status === 'done' ? 'success' : row.generation_status === 'error' ? 'error' : row.generation_status === 'generating' ? 'warning' : 'primary'}`} style={{ marginLeft: 6 }}>
              {ROW_GEN_STATUS_LABELS[row.generation_status] ?? row.generation_status}
            </span>
          </h3>
        </div>
        <button type="button" className="btn-icon" style={{ width: 26, height: 26 }} title="关闭详情" aria-label="关闭详情" onClick={() => { setSelectedRow(null); onBack?.(); }}>
          <X size={14} />
        </button>
      </div>

      {/* 2. 预览区：当前关键帧大图（点击开灯箱） */}
      <div className="manga-asset-preview">
        {currentUrl ? (
          <button
            type="button"
            className="manga-asset-preview-btn"
            title="点击放大预览（右键下载）"
            onClick={() => setLightbox(true)}
            onContextMenu={(e) => {
              e.preventDefault();
              downloadByUrl(currentUrl, `镜${row.shot_number}-v${current?.version ?? 1}.png`);
            }}
          >
            <img src={currentUrl} alt={`镜 ${row.shot_number} 分镜图`} loading="lazy" />
            {current && !current.is_current && (
              <span className="manga-insp-preview-tag">v{current.version}</span>
            )}
          </button>
        ) : (
          <div className="manga-asset-preview-empty">
            <ImagePlus size={22} />
            暂无分镜图，编辑下方描述后点击生成
          </div>
        )}
      </div>

      {/* 3. 描述词：AI 生成（左）+ 替换图片（右）并排；textarea 失焦保存 */}
      <div className="flex gap-2" style={{ width: '100%' }}>
        <button
          type="button"
          className="btn btn-secondary btn-sm"
          style={{ flex: 1 }}
          disabled={busy !== '' || (!(row?.asset_ids?.length ?? 0) && !row?.asset_id)}
          title={
            !(row?.asset_ids?.length ?? 0) && !row?.asset_id
              ? '未绑定资产：先在分镜表资产列绑定角色/场景/道具才能生成描述词'
              : '按原始台词 + 绑定资产 + 风格前缀生成 A/B/C 结构化分镜描述词（Qwen3-VL）'
          }
          onClick={handleDescribe}
        >
          <Sparkles size={13} />
          {busy === 'describe' ? '生成中…' : 'AI 生成描述'}
        </button>
        <button
          type="button"
          className="btn btn-secondary btn-sm"
          style={{ flex: 1 }}
          disabled={replacing || generating}
          title="替换图片：上传本地图片作为该镜当前分镜图（旧版保留可回退）"
          onClick={() => replaceFileRef.current?.click()}
        >
          <ImagePlus size={13} />
          {replacing ? '上传中…' : '替换图片'}
        </button>
      </div>
      <div className="card manga-insp-card">
        <div className="manga-insp-title">描述词（A 全局风格 / B 世界观 / C 分镜时间轴）</div>
        <textarea
          className="input"
          rows={9}
          value={descDraft}
          maxLength={4000}
          style={{ resize: 'vertical' }}
          placeholder="A/B/C 结构化分镜描述词，AI 生图按此出图…"
          onChange={(e) => setDescDraft(e.target.value)}
          onBlur={commitDescription}
        />
      </div>

      {/* 4. AI 生图主按钮（全宽；有版本 = 重新生成出新版，旧版保留可回退；
          生成中显示 WS 实时进度条填充 + 镜序标签） */}
      {(() => {
        const curKf = keyframes?.find((k) => k.is_current) ?? keyframes?.[0];
        if (curKf?.source_mode !== 'fallback') return null;
        return (
          <p
            className="text-secondary"
            style={{ margin: 0, fontSize: 'var(--font-size-xs)', padding: '2px 0' }}
            title="该行未生成描述词，此图按剧本原文直接生成；建议先生成描述词再重新生成分镜图以获得一致性与细节"
          >
            ⚠ 当前图为兜底模式（原文直出，未使用描述词）
          </p>
        );
      })()}
      <button
        type="button"
        className="btn btn-primary manga-asset-gen-btn"
        disabled={generating}
        title={hasVersions ? '重新生成分镜图：产出新版本，旧版保留可回退' : '按描述词生成分镜图（SDXL）'}
        onClick={handleGenerateKeyframe}
      >
        {generating ? (
          <>
            {genProgress ? (
              <span
                className="manga-gen-btn-fill"
                style={{ width: `${genProgress.percent}%` }}
                aria-hidden="true"
              />
            ) : null}
            <span className="manga-gen-btn-content">
              <span className="spinner manga-mini-spin" />
              {genProgress
                ? `${genProgress.label || '生成中'} ${genProgress.percent}%`
                : '生成中…'}
            </span>
          </>
        ) : (
          <>
            <ImagePlus size={14} />
            {hasVersions ? '重新生成分镜图' : '生成分镜图'}
          </>
        )}
      </button>
      <input
        ref={replaceFileRef}
        type="file"
        accept="image/png,image/jpeg,image/webp"
        style={{ display: 'none' }}
        onChange={handleReplaceFile}
      />

      {/* 5. 历史记录折叠区：关键帧版本列表 */}
      <div className="manga-history">
        <button
          type="button"
          className="manga-history-head"
          aria-expanded={historyOpen}
          onClick={() => setHistoryOpen((v) => !v)}
        >
          <History size={13} />
          <span style={{ flex: 1, textAlign: 'left' }}>历史记录{hasVersions ? `（${versions.length}）` : ''}</span>
          <ChevronDown size={14} className={`manga-dock-chevron${historyOpen ? ' open' : ''}`} />
        </button>
        {historyOpen && (
          <div className="manga-history-body">
            {!hasVersions ? (
              <div className="manga-history-empty">暂无版本</div>
            ) : (
              <div className="flex flex-col gap-2">
                {versions.map((k) => (
                  <div
                    key={k.keyframe_id}
                    style={{
                      borderRadius: 'var(--radius-md)',
                      overflow: 'hidden',
                      border: k.is_current ? '1px solid var(--color-primary)' : '1px solid var(--color-border-light)',
                      boxShadow: k.is_current ? '0 0 0 1px var(--color-primary)' : undefined,
                    }}
                  >
                    {k.file_path ? (
                      <img
                        src={getMediaUrl(k.file_path, `${k.version}-${k.created_at}`)}
                        alt={`v${k.version}`}
                        loading="lazy"
                        title="右键下载"
                        onContextMenu={(e) => {
                          e.preventDefault();
                          downloadByUrl(
                            getMediaUrl(k.file_path, `${k.version}-${k.created_at}`),
                            `镜${row.shot_number}-v${k.version}.png`,
                          );
                        }}
                        style={{ width: '100%', aspectRatio: '16/9', objectFit: 'cover', background: 'var(--color-input-bg)', display: 'block' }}
                      />
                    ) : (
                      <div className="flex items-center justify-center" style={{ width: '100%', aspectRatio: '16/9', background: 'var(--color-input-bg)', fontSize: 'var(--font-size-xs)', color: 'var(--color-text-tertiary)' }}>
                        {k.status === 'error' ? '生成失败' : '无图像'}
                      </div>
                    )}
                    <div style={{ padding: 'var(--space-2)' }}>
                      <div className="flex items-center gap-2" style={{ fontSize: 'var(--font-size-xs)' }}>
                        <span style={{ fontWeight: 600 }}>v{k.version}</span>
                        {k.is_current && <span className="badge primary">当前</span>}
                        <span style={{ marginLeft: 'auto', fontSize: 10, color: 'var(--color-text-tertiary)' }}>{formatTime(k.created_at)}</span>
                      </div>
                      {k.error && (
                        <div className="ellipsis" title={k.error} style={{ marginTop: 2, fontSize: 10, color: 'var(--color-error)' }}>
                          {k.error}
                        </div>
                      )}
                      <div className="flex gap-1 mt-2">
                        {!k.is_current && (
                          <button type="button" className="btn btn-secondary btn-sm flex-1" disabled={busyId === k.keyframe_id} onClick={() => handleKeyframeOp(k.keyframe_id, k.version, 'rollback')} title="回退：将此版本置为当前">
                            <RotateCcw size={11} />
                            回退
                          </button>
                        )}
                        <button
                          type="button"
                          className="btn btn-secondary btn-sm flex-1"
                          style={{ color: 'var(--color-error)' }}
                          disabled={busyId === k.keyframe_id}
                          onClick={() => handleKeyframeOp(k.keyframe_id, k.version, 'delete')}
                          title={k.is_current ? '删除当前版本（自动回退上一版）' : '删除此版本'}
                        >
                          <Trash2 size={11} />
                          删除
                        </button>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      {/* 6. 音色绑定轻量入口（音色抽屉唯一入口，差异化功能保留） */}
      <button
        type="button"
        className="btn btn-ghost btn-sm"
        style={{ width: '100%', justifyContent: 'center' }}
        title="打开音色绑定抽屉（配音角色/情绪）"
        onClick={onOpenVoice}
      >
        <Mic size={13} />
        音色绑定
      </button>

      {/* 灯箱：当前关键帧大图 */}
      {lightbox && currentUrl && (
        <AssetLightbox srcs={[currentUrl]} onClose={() => setLightbox(false)} />
      )}
    </aside>
  );
}
