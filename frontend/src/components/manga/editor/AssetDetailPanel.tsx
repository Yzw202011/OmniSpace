/* ==========================================================================
 * AssetDetailPanel.tsx —— 漫剧编辑器右栏·资产详情面板（竞品 yl.man-tui.com 对齐）
 * --------------------------------------------------------------------------
 * 右栏槽位成员（优先级：抽屉 > 资产详情 > 行检查器 > 资产面板），字段顺序严格对齐竞品：
 *   1. 头部：← 返回（清除 selectedAssetId）+ 「{资产名} 详情」+ 类型徽标
 *   2. 预览区：有四视图（meta.turnaround 且 meta.canvas 非空）→ 单图整图四视图
 *      （one-pass 一图四格，PIL 已标角色名/视图标签，点击开灯箱）；否则单图 160px contain
 *   3. 角色切换器（仅角色）：select 列出本项目全部角色资产，切换 = setSelectedAsset
 *   4. 名称 input（失焦 PUT /comic/asset/{id} 保存）
 *   5. 按钮行：✦ 生成描述词（describeAsset）/ ☁ 替换图片（replaceAssetImage，asset_id 隔离落盘）
 *   6. 参考图行：上传AI参考图（multipart → img2img 保持人设）；已上传显示 60×60 缩略图 + 删除
 *   7. 描述词 textarea（失焦保存）
 *   8. AI 生图主按钮（全宽 btn-primary）：角色走四视图管线（无 meta.turnaround 首次
 *      携带 {mode:'four_views'} 升级，约 40-90s）；场景/道具走现有 regenerate
 *   9. 历史记录折叠区（展开时拉 history API，横向滚动 46×50 缩略图 + MM-DD HH:mm）
 *  10. 绑定音色卡（仅角色资产，差异化功能保留）
 * 所有资产生成/更新类调用成功后统一 fetchAssets() 刷新（以 library 端点为准）。
 * ========================================================================== */

import { useEffect, useRef, useState } from 'react';
import {
  ArrowLeft,
  ChevronDown,
  CloudUpload,
  Globe,
  History,
  ImagePlus,
  Mic,
  Sparkles,
  Trash2,
  Volume2,
} from 'lucide-react';
import { useAppStore } from '@/stores/useAppStore';
import { useMangaStore } from '@/stores/useMangaStore';
import { useWarmupStore } from '@/stores/useWarmupStore';
import { warmupFeature } from '@/services/modelApi';
import {
  assetMediaVersion,
  deleteAssetReference,
  describeAsset,
  fetchAssetHistory,
  getMediaUrl,
  regenerateAsset,
  replaceAssetImage,
  toGlobalAsset,
  updateAsset,
  uploadAssetReference,
  type AssetHistoryItem,
} from '@/services/mangaApi';
import { getErrorMessage } from '@/utils/errors';
import AssetLightbox from './AssetLightbox';
import { useGenProgress } from './useGenProgress';

/** 资产类型中文名 */
const KIND_LABELS: Record<string, string> = {
  character: '角色',
  scene: '场景',
  prop: '道具',
};

/** 上传允许扩展名（对齐后端 _ASSET_UPLOAD_EXTS） */
const UPLOAD_ACCEPT = '.png,.jpg,.jpeg,.webp';
/** 上传大小上限 10MB（对齐后端 _ASSET_UPLOAD_MAX_BYTES） */
const UPLOAD_MAX_BYTES = 10 * 1024 * 1024;

/** 历史记录时间戳格式化（MM-DD HH:mm；ts 秒/毫秒自适应） */
function formatHistoryTime(ts: number): string {
  const ms = ts < 1e12 ? ts * 1000 : ts;
  const d = new Date(ms);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export default function AssetDetailPanel() {
  const showToast = useAppStore((s) => s.showToast);
  const currentProject = useMangaStore((s) => s.currentProject);
  const asset = useMangaStore((s) => s.assets.find((a) => a.asset_id === s.selectedAssetId));
  const selectedAssetId = useMangaStore((s) => s.selectedAssetId);
  const setSelectedAsset = useMangaStore((s) => s.setSelectedAsset);
  const fetchAssets = useMangaStore((s) => s.fetchAssets);

  /** 名称/描述词本地草稿（失焦保存） */
  const [nameDraft, setNameDraft] = useState('');
  const [promptDraft, setPromptDraft] = useState('');
  /** 操作忙碌（describe/upload/regenerate/refUp/refDel 互斥） */
  const [busy, setBusy] = useState<'' | 'describe' | 'upload' | 'regenerate' | 'refUp' | 'refDel' | 'toGlobal'>('');
  /** 灯箱（null 关闭；srcs 为点击时快照，idx 为 srcs 索引） */
  const [lightbox, setLightbox] = useState<{ srcs: string[]; idx: number } | null>(null);
  /** 历史记录折叠区 */
  const [historyOpen, setHistoryOpen] = useState(false);
  const [historyItems, setHistoryItems] = useState<AssetHistoryItem[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  /** 隐藏文件输入（本地图片 / AI 参考图） */
  const fileRef = useRef<HTMLInputElement | null>(null);
  const refFileRef = useRef<HTMLInputElement | null>(null);
  // WS 实时进度（后端视图/采样步级广播 → 按钮内进度条）
  const genProgress = useGenProgress('asset', selectedAssetId ?? undefined, busy === 'regenerate');

  /* ------------------------------ 音色绑定（角色资产） ------------------- */
  const voices = useMangaStore((s) => s.voices);
  const voicesLoaded = useMangaStore((s) => s.voicesLoaded);
  const fetchVoices = useMangaStore((s) => s.fetchVoices);
  const bindVoice = useMangaStore((s) => s.bindVoice);
  const previewVoice = useMangaStore((s) => s.previewVoice);
  /** 下拉选中的音色 ID */
  const [voiceSel, setVoiceSel] = useState('');
  /** 音色操作忙碌（bind/preview 互斥） */
  const [voiceBusy, setVoiceBusy] = useState<'' | 'bind' | 'preview'>('');
  const audioRef = useRef<HTMLAudioElement | null>(null);

  const isCharacter = asset?.kind === 'character';
  /** 当前角色已绑定音色（character_id = 角色名，与 VoiceBinder 同语义） */
  const boundVoice = isCharacter
    ? voices.find((v) => v.character_id === asset.name)
    : undefined;

  // 角色资产打开时拉取音色列表（一次）
  useEffect(() => {
    if (isCharacter && !voicesLoaded) {
      void fetchVoices();
    }
  }, [isCharacter, voicesLoaded, fetchVoices]);

  // 音色列表/选中资产变化：下拉默认对齐当前绑定
  useEffect(() => {
    setVoiceSel(boundVoice?.id ?? '');
  }, [boundVoice?.id, asset?.asset_id]);

  // 卸载时停止试听
  useEffect(() => {
    return () => {
      audioRef.current?.pause();
      audioRef.current = null;
    };
  }, []);

  // 选中资产切换/服务端刷新：同步草稿（以 library 端点数据为准）
  useEffect(() => {
    setNameDraft(asset?.name ?? '');
    setPromptDraft(asset?.prompt ?? '');
  }, [asset?.asset_id, asset?.name, asset?.prompt, asset?.meta]);

  // 选中资产切换：重置灯箱/历史折叠（避免跨资产串状态）
  useEffect(() => {
    setLightbox(null);
    setHistoryOpen(false);
    setHistoryItems([]);
    setHistoryLoading(false);
  }, [asset?.asset_id]);

  if (!currentProject || !selectedAssetId || !asset) return null;

  const kindLabel = KIND_LABELS[asset.kind] ?? '资产';

  /* ------------------------------ 预览区数据推导 ------------------------------ */
  /** 角色多视图缓存破除版本信号（meta.seed / meta.regenerated_at） */
  const assetVer = assetMediaVersion(asset);
  /** 四视图整图 URL（meta.canvas：one-pass 一图四格，PIL 已标角色名/视图标签） */
  const canvas = asset.meta?.canvas;
  const canvasUrl = typeof canvas === 'string' && canvas
    ? getMediaUrl(canvas, assetVer)
    : '';
  /** 有四视图：meta.turnaround 为真且整图存在（缺 canvas 的老资产回退主图预览） */
  const hasTurnaround = isCharacter && !!asset.meta?.turnaround && !!canvasUrl;
  const mainUrl = asset.file_path ? getMediaUrl(asset.file_path, assetVer) : '';
  /** AI 参考图 URL（meta.reference_image 为真时：file_path 去文件名 + /reference.png） */
  const refUrl = (() => {
    if (!asset.meta?.reference_image || !asset.file_path) return '';
    const dir = asset.file_path.replace(/\\/g, '/').split('/').slice(0, -1).join('/');
    return getMediaUrl(dir ? `${dir}/reference.png` : 'reference.png', assetVer);
  })();

  /** 失焦保存（名称/描述词；仅变更时调用） */
  const commitField = (field: 'name' | 'prompt') => {
    const value = (field === 'name' ? nameDraft : promptDraft).trim();
    const original = field === 'name' ? asset.name : asset.prompt;
    if (value === original) return;
    if (field === 'name' && !value) {
      showToast('资产名称不能为空', 'warning');
      setNameDraft(asset.name);
      return;
    }
    updateAsset(asset.asset_id, { [field]: value })
      .then(() => {
        showToast(field === 'name' ? '资产名称已保存' : '描述词已保存', 'success');
        return fetchAssets();
      })
      .catch((err: unknown) => showToast(getErrorMessage(err, '保存失败'), 'error'));
  };

  /** ✦ 生成描述词（对话引擎扩写，DIALOG_NOT_READY 如实 toast） */
  const handleDescribe = () => {
    if (busy) return;
    setBusy('describe');
    describeAsset(asset.asset_id)
      .then(() => {
        showToast('描述词已生成', 'success');
        return fetchAssets();
      })
      .catch((err: unknown) => showToast(getErrorMessage(err, '描述词生成失败'), 'error'))
      .finally(() => setBusy(''));
  };

  /** ☁ 本地图片上传 */
  const handlePickFile = () => {
    if (busy) return;
    fileRef.current?.click();
  };

  /** 🌐 转为全局资产（跨项目复用）：迁移磁盘目录 + 脱离当前项目 */
  const handleToGlobal = () => {
    if (busy) return;
    setBusy('toGlobal');
    toGlobalAsset(asset.asset_id)
      .then(({ alreadyGlobal }) => {
        showToast(
          alreadyGlobal
            ? `「${asset.name}」已是全局资产`
            : `「${asset.name}」已转为全局资产，可在全部项目中使用`,
          alreadyGlobal ? 'info' : 'success',
        );
        // 转全局后资产脱离当前项目列表 → 清除选中回到资产面板
        setSelectedAsset(null);
        return fetchAssets();
      })
      .catch((err: unknown) => showToast(getErrorMessage(err, '转为全局资产失败'), 'error'))
      .finally(() => setBusy(''));
  };

  /** 上传前置校验（扩展名 + 10MB 上限，对齐后端约束） */
  const validateImageFile = (file: File): boolean => {
    const ext = `.${(file.name.split('.').pop() || '').toLowerCase()}`;
    if (!UPLOAD_ACCEPT.split(',').includes(ext)) {
      showToast('仅支持 png/jpg/jpeg/webp 图片文件', 'warning');
      return false;
    }
    if (file.size > UPLOAD_MAX_BYTES) {
      showToast('图片文件超过 10MB 上限', 'warning');
      return false;
    }
    return true;
  };

  const handleFileChange = (file: File | undefined) => {
    if (!file || !validateImageFile(file)) return;
    setBusy('upload');
    replaceAssetImage(asset.asset_id, file)
      .then(() => {
        showToast(
          isCharacter
            ? `「${asset.name}」图片已替换，描述词将按新图自动重写`
            : `「${asset.name}」图片已替换`,
          'success',
        );
        return fetchAssets();
      })
      .catch((err: unknown) => showToast(getErrorMessage(err, '图片替换失败'), 'error'))
      .finally(() => setBusy(''));
  };

  /** 上传 AI 参考图（四视图生成时走 img2img 保持人设） */
  const handleRefUpload = (file: File | undefined) => {
    if (!file || !validateImageFile(file)) return;
    setBusy('refUp');
    uploadAssetReference(asset.asset_id, file)
      .then(() => {
        showToast('AI 参考图已上传', 'success');
        return fetchAssets();
      })
      .catch((err: unknown) => showToast(getErrorMessage(err, '参考图上传失败'), 'error'))
      .finally(() => setBusy(''));
  };

  /** 删除 AI 参考图 */
  const handleRefDelete = () => {
    if (busy) return;
    setBusy('refDel');
    deleteAssetReference(asset.asset_id)
      .then(() => {
        showToast('AI 参考图已删除', 'success');
        return fetchAssets();
      })
      .catch((err: unknown) => showToast(getErrorMessage(err, '参考图删除失败'), 'error'))
      .finally(() => setBusy(''));
  };

  /** AI 生图（角色走四视图管线；场景/道具走现有 regenerate；degraded 如实展示） */
  const handleRegenerate = () => {
    if (busy) return;
    // P0 数据修复卡控：图片已替换但描述词尚未按新图重写完成 →
    // 禁止生图（旧词与新图脱节必出漂移——V41 事故教训）；
    // 用户可等待自动重写完成、点「生成描述词」或手动编辑描述词解除
    if (isCharacter && asset.meta?.prompt_stale) {
      showToast(
        '描述词尚未按新图重写，生图会与人物不符：请稍候自动重写完成，或点「生成描述词」/手动编辑描述词',
        'warning',
      );
      return;
    }
    if (!promptDraft.trim()) {
      showToast('请先填写描述词，再点击 AI 生图', 'warning');
      return;
    }
    // 本地草稿未保存时先落库，确保后端按最新描述词出图
    const persist = promptDraft.trim() !== asset.prompt
      ? updateAsset(asset.asset_id, {
          prompt: promptDraft.trim(),
        }).then(() => undefined)
      : Promise.resolve();
    // 角色资产四视图契约：无 meta.turnaround 首次携带 mode 升级；已有则传 {} 重生成四视图
    const regenBody = isCharacter
      ? (asset.meta?.turnaround ? {} : { mode: 'four_views' })
      : undefined;
    setBusy('regenerate');
    persist
      .then(() => regenerateAsset(asset.asset_id, regenBody))
      .then((res) => {
        if (res.degraded) {
          const reason = res.degrade_reason || '资产图为降级管线产出';
          // 引擎未就绪降级（2026-08-31 用户需求「立刻加载+告知」）：后端
          // 本次已自动尝试加载但失败 → 立即点火后台预热 + 弹加载进度窗，
          // 就绪后用户再点一次即出图（与 InspectorPanel 分镜生图同款语义）
          if (/未就绪|未加载|PAINT_ENGINE/.test(reason)) {
            void warmupFeature('paint')
              .then((r) => {
                if (r?.started) useWarmupStore.getState().begin(undefined, 'paint');
              })
              .catch(() => { /* 预热点火失败静默，指引已给 */ });
            showToast(
              `${reason}。正在后台自动加载绘画模型（弹窗可见进度），就绪后请再点一次「AI 生图」`,
              'warning',
            );
          } else {
            showToast(reason, 'warning');
          }
        } else {
          showToast(isCharacter ? '角色四视图已生成' : '资产图已生成', 'success');
        }
        return fetchAssets();
      })
      .catch((err: unknown) => showToast(getErrorMessage(err, '资产图生成失败'), 'error'))
      .finally(() => setBusy(''));
  };

  /** 历史记录折叠开关（展开时拉取，最新在前上限 12 条） */
  const toggleHistory = () => {
    const next = !historyOpen;
    setHistoryOpen(next);
    if (!next) return;
    setHistoryLoading(true);
    fetchAssetHistory(asset.asset_id)
      .then((items) => setHistoryItems(items))
      .catch((err: unknown) => showToast(getErrorMessage(err, '生成历史加载失败'), 'error'))
      .finally(() => setHistoryLoading(false));
  };

  /** 绑定音色到当前角色（character_id = 角色名） */
  const handleVoiceBind = () => {
    if (voiceBusy || !voiceSel || !isCharacter) return;
    setVoiceBusy('bind');
    bindVoice(voiceSel, asset.name)
      .then(() => showToast(`音色已绑定到「${asset.name}」`, 'success'))
      .catch((err: unknown) => showToast(getErrorMessage(err, '音色绑定失败'), 'error'))
      .finally(() => setVoiceBusy(''));
  };

  /** 试听选中音色（base64 本地播放；降级如实提示） */
  const handleVoicePreview = () => {
    if (voiceBusy || !voiceSel) return;
    const voice = voices.find((v) => v.id === voiceSel);
    if (!voice) return;
    setVoiceBusy('preview');
    previewVoice(voice.id, '你好，这是一段音色试听。', voice.emotion)
      .then(async (res) => {
        audioRef.current?.pause();
        const audio = new Audio(`data:audio/${res.format || 'wav'};base64,${res.audio}`);
        audioRef.current = audio;
        audio.onended = () => setVoiceBusy('');
        audio.onerror = () => setVoiceBusy('');
        await audio.play();
        if (res.degraded) {
          showToast(res.degrade_reason || '试听音频为降级占位（非真实音色）', 'warning');
        }
      })
      .catch((err: unknown) => {
        showToast(getErrorMessage(err, '试听失败'), 'error');
        setVoiceBusy('');
      });
  };

  return (
    <aside className="manga-ed-inspector manga-asset-detail">
      {/* 1. 头部：← 返回 + 标题 + 类型徽标 */}
      <div className="manga-ed-inspector-head">
        <button
          type="button"
          className="btn-icon"
          style={{ width: 26, height: 26 }}
          title="返回资产面板"
          aria-label="返回资产面板"
          onClick={() => setSelectedAsset(null)}
        >
          <ArrowLeft size={15} />
        </button>
        <h3 className="manga-ed-inspector-title ellipsis" style={{ flex: 1, minWidth: 0 }} title={asset.name}>
          {asset.name} 详情
        </h3>
        <span className="badge primary">{kindLabel}</span>
      </div>

      {/* 2. 预览区：四视图单图整图 / 单图 */}
      {hasTurnaround ? (
        <button
          type="button"
          className="manga-view-sheet"
          title="点击放大查看四视图整图（正面/侧面/背面/特写）"
          onClick={() => setLightbox({ srcs: [canvasUrl], idx: 0 })}
        >
          <img src={canvasUrl} alt={`${asset.name} 四视图`} loading="lazy" />
        </button>
      ) : (
        <div className="manga-asset-preview">
          {mainUrl ? (
            <button
              type="button"
              className="manga-asset-preview-btn"
              title="点击放大查看"
              onClick={() => setLightbox({ srcs: [mainUrl], idx: 0 })}
            >
              <img src={mainUrl} alt={asset.name} />
            </button>
          ) : (
            <div className="manga-asset-preview-empty">
              <ImagePlus size={22} />
              暂无图片，编辑下方描述后点击生成
            </div>
          )}
        </div>
      )}

      {/* 4. 名称（失焦保存） */}
      <div className="card manga-insp-card">
        <div className="manga-insp-title">名称</div>
        <input
          className="input"
          value={nameDraft}
          maxLength={100}
          placeholder="资产名称"
          onChange={(e) => setNameDraft(e.target.value)}
          onBlur={() => commitField('name')}
        />
      </div>

      {/* 5. 按钮行：✦ 生成描述词 / ☁ 本地图片 / 🌐 转为全局 */}
      <div className="flex gap-2">
        <button
          type="button"
          className="btn btn-secondary btn-sm flex-1"
          disabled={busy !== ''}
          title={isCharacter && mainUrl ? '视觉模型按当前资产图重写描述词（与图片内容严格一致）' : '对话引擎按资产名称扩写描述词'}
          onClick={handleDescribe}
        >
          <Sparkles size={13} />
          {busy === 'describe' ? '生成中…' : '生成描述词'}
        </button>
        <button
          type="button"
          className="btn btn-secondary btn-sm flex-1"
          disabled={busy !== ''}
          title="用本地图片替换当前资产的图片（png/jpg/jpeg/webp ≤10MB，不影响其他资产）"
          onClick={handlePickFile}
        >
          <CloudUpload size={13} />
          {busy === 'upload' ? '替换中…' : '替换图片'}
        </button>
        <button
          type="button"
          className="btn btn-secondary btn-sm flex-1"
          disabled={busy !== ''}
          title="转为全局资产：脱离当前项目，全部项目可复用（删除项目时全局资产保留）"
          onClick={handleToGlobal}
        >
          <Globe size={13} />
          {busy === 'toGlobal' ? '转换中…' : '转为全局'}
        </button>
        <input
          ref={fileRef}
          type="file"
          accept={UPLOAD_ACCEPT}
          style={{ display: 'none' }}
          aria-hidden="true"
          tabIndex={-1}
          onChange={(e) => {
            handleFileChange(e.target.files?.[0]);
            e.target.value = '';
          }}
        />
      </div>

      {/* 6. 参考图行：上传AI参考图 + 已上传缩略图/删除 */}
      <div className="manga-ref-row">
        <button
          type="button"
          className="btn btn-secondary btn-sm flex-1"
          disabled={busy !== ''}
          title="上传 AI 参考图（png/jpg/jpeg/webp ≤10MB；四视图生成时走 img2img 保持人设一致）"
          onClick={() => refFileRef.current?.click()}
        >
          <ImagePlus size={13} />
          {busy === 'refUp' ? '上传中…' : '上传AI参考图'}
        </button>
        {refUrl && (
          <span className="manga-ref-thumb-wrap">
            <img className="manga-ref-thumb" src={refUrl} alt="AI 参考图" />
            <button
              type="button"
              className="manga-ref-del"
              disabled={busy !== ''}
              title="删除 AI 参考图"
              aria-label="删除 AI 参考图"
              onClick={handleRefDelete}
            >
              {busy === 'refDel' ? <span className="spinner manga-mini-spin" /> : <Trash2 size={10} />}
            </button>
          </span>
        )}
        <input
          ref={refFileRef}
          type="file"
          accept={UPLOAD_ACCEPT}
          style={{ display: 'none' }}
          aria-hidden="true"
          tabIndex={-1}
          onChange={(e) => {
            handleRefUpload(e.target.files?.[0]);
            e.target.value = '';
          }}
        />
      </div>

      {/* 7. 描述词（失焦保存；prompt_stale = 图已换词待重写，VLM 重写中/待触发） */}
      <div className="card manga-insp-card">
        <div className="manga-insp-title">
          描述词
          {isCharacter && !!asset.meta?.prompt_stale && (
            <span
              className="badge warning"
              style={{ marginLeft: 'var(--space-2)' }}
              title="图片已替换，描述词将按新图自动重写；也可点「生成描述词」立即重写或手动编辑解除"
            >
              待按新图重写
            </span>
          )}
        </div>
        <textarea
          className="input"
          rows={5}
          value={promptDraft}
          maxLength={2000}
          style={{ resize: 'vertical' }}
          placeholder="资产外观/风格描述词，AI 生图按此出图…"
          onChange={(e) => setPromptDraft(e.target.value)}
          onBlur={() => commitField('prompt')}
        />
      </div>

      {/* 8. 底部主按钮：AI 生图（角色 = 四视图管线；生成中显示 WS 实时进度条） */}
      <button
        type="button"
        className="btn btn-primary manga-asset-gen-btn"
        disabled={busy !== ''}
        title={
          isCharacter
            ? '按描述词生成角色四视图（正面/侧面/背面/特写，约 1 分钟）'
            : '按描述词生成/重新生成资产图（SDXL）'
        }
        onClick={handleRegenerate}
      >
        {busy === 'regenerate' ? (
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
                ? `${genProgress.label || (isCharacter ? '生成四视图' : '生成中')} ${genProgress.percent}%`
                : isCharacter
                  ? '生成四视图中，约1分钟…'
                  : '生成中…'}
            </span>
          </>
        ) : (
          <>
            <ImagePlus size={14} />
            AI 生图
          </>
        )}
      </button>

      {/* 9. 历史记录折叠区（展开时拉取，横向滚动缩略图条） */}
      <div className="manga-history">
        <button
          type="button"
          className="manga-history-head"
          aria-expanded={historyOpen}
          onClick={toggleHistory}
        >
          <History size={13} />
          <span style={{ flex: 1, textAlign: 'left' }}>历史记录</span>
          <ChevronDown size={14} className={`manga-dock-chevron${historyOpen ? ' open' : ''}`} />
        </button>
        {historyOpen && (
          <div className="manga-history-body">
            {historyLoading ? (
              <div className="loading-block">
                <span className="spinner" />
                历史加载中…
              </div>
            ) : historyItems.length === 0 ? (
              <div className="manga-history-empty">暂无生成历史</div>
            ) : (
              <div className="manga-history-list">
                {historyItems.map((h, i) => (
                  <figure key={`${h.ts}-${i}`} className="manga-history-item">
                    <img src={h.url} alt={h.view || h.kind} loading="lazy" />
                    <figcaption>{formatHistoryTime(h.ts)}</figcaption>
                  </figure>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      {/* 10. 绑定音色（仅角色资产；character_id = 角色名，差异化功能保留） */}
      {isCharacter && (
        <div className="card manga-insp-card">
          <div className="manga-insp-title">
            绑定音色
            <span className="manga-voice-current">
              当前：{boundVoice ? boundVoice.name : '未绑定'}
            </span>
          </div>
          <select
            className="input"
            value={voiceSel}
            disabled={voiceBusy !== '' || voices.length === 0}
            onChange={(e) => setVoiceSel(e.target.value)}
          >
            <option value="">
              {voicesLoaded ? '选择音色…' : '音色加载中…'}
            </option>
            {voices.map((v) => (
              <option key={v.id} value={v.id}>
                {v.name}{v.is_preset ? '（预置）' : ''}
              </option>
            ))}
          </select>
          <div className="flex gap-2" style={{ marginTop: 'var(--space-2)' }}>
            <button
              type="button"
              className="btn btn-secondary btn-sm flex-1"
              disabled={!voiceSel || voiceBusy !== ''}
              title="试听选中音色"
              onClick={handleVoicePreview}
            >
              <Volume2 size={13} />
              {voiceBusy === 'preview' ? '播放中…' : '试听'}
            </button>
            <button
              type="button"
              className="btn btn-primary btn-sm flex-1"
              disabled={!voiceSel || voiceBusy !== ''}
              title={`绑定到「${asset.name}」（自动解除旧绑定）`}
              onClick={handleVoiceBind}
            >
              <Mic size={13} />
              {voiceBusy === 'bind' ? '绑定中…' : boundVoice ? '换绑' : '绑定'}
            </button>
          </div>
        </div>
      )}

      {/* 灯箱（四视图整图 / 主图） */}
      {lightbox !== null && (
        <AssetLightbox srcs={lightbox.srcs} initialIndex={lightbox.idx} onClose={() => setLightbox(null)} />
      )}
    </aside>
  );
}
// 本项目仅供学习使用，商业授权请+Q 3559331368
