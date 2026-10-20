/* ==========================================================================
 * ModelConfigModal.tsx —— 漫剧编辑器·模型配置弹层（竞品 yl.man-tui.com 对齐）
 * --------------------------------------------------------------------------
 * 竞品模型配置为居中弹层多下拉，本实现：
 *   - 三个模型下拉：推理模型 / 绘画模型 / 视频模型
 *     （打开时并行 listAvailableModels('dialog'|'paint'|'video') 拉取；
 *       每项显示模型名+可用性，不可用项 disabled）
 *   - 两个视频默认参数下拉：默认画幅（横屏 16:9 / 竖屏 9:16）、
 *     默认时长（4 秒 / 8 秒 / 11 秒 / 15 秒）
 *   - 底部 [恢复默认] [保存]
 *   - 持久化 localStorage 键「omnispace.manga.modelConfig」，
 *     结构 {dialogModel,paintModel,videoModel,aspect,duration}；打开时读取回填
 * 导出 loadModelConfig()：VideoConfirmModal 与本弹窗共用的读取 helper。
 * ========================================================================== */

import { useEffect, useState } from 'react';
import { Loader2, Settings2 } from 'lucide-react';
import { useAppStore } from '@/stores/useAppStore';
import { getVideoSrModel, getVideoSrResolution, listAvailableModels, setVideoSrModel, setVideoSrResolution } from '@/services/mangaApi';
import { getModuleModelConfig, saveModuleModelConfig } from '@/services/modelApi';
import { MODEL_AVAILABLE_STATUS_LABELS } from '@/constants/statusLabels';
import { DEFAULT_MODEL_CONFIG, writeModelConfig, type MangaModelConfig } from '@/constants/modelConfig';
import { loadModelConfig, snapToTiers, tierDurations, tierQualities } from '@/constants/modelConfig';
import type { AvailableModel } from '@/types';
import { Modal } from '../../common/Modal';

/** 模型下拉属性 */
interface ModelSelectProps {
  /** 字段标签（推理模型/绘画模型/视频模型） */
  label: string;
  /** 当前选中模型 ID（'' = 系统默认） */
  value: string;
  /** 候选模型列表 */
  models: AvailableModel[];
  /** 列表加载中 */
  loading: boolean;
  onChange: (id: string) => void;
}

/** 模型下拉（每项显示模型名+可用性，不可用项 disabled） */
function ModelSelect({ label, value, models, loading, onChange }: ModelSelectProps) {
  // 已保存的模型当前不在列表（被卸载/移除）→ 追加占位项，如实显示已保存值
  const savedMissing = value !== '' && !models.some((m) => m.id === value);
  return (
    <div>
      <label className="manga-form-label">{label}</label>
      {loading ? (
        <span className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)' }}>
          <Loader2 size={12} style={{ animation: 'spin 1s linear infinite', verticalAlign: -2 }} /> 加载模型列表…
        </span>
      ) : (
        <select
          className="manga-modal-select"
          value={value}
          aria-label={label}
          onChange={(e) => onChange(e.target.value)}
        >
          <option value="">系统默认（自动选择）</option>
          {models.map((m) => (
            <option key={m.id} value={m.id} disabled={m.status !== 'ready' && m.status !== 'downloaded' && m.status !== 'cloud'}>
              {`${m.name}（${MODEL_AVAILABLE_STATUS_LABELS[m.status]}）`}
            </option>
          ))}
          {savedMissing && (
            <option value={value} disabled>
              {`${value}（已保存，当前不可用）`}
            </option>
          )}
        </select>
      )}
    </div>
  );
}

export default function ModelConfigModal({ onClose }: { onClose: () => void }) {
  const showToast = useAppStore((s) => s.showToast);
  const [cfg, setCfg] = useState<MangaModelConfig>(loadModelConfig);
  /** 超分目标分辨率（后端 settings KV manga.sr_resolution；2026-10-12 用户令） */
  const [srRes, setSrRes] = useState<'1k' | '2k'>('1k');
  /** 超分模型（后端 settings KV manga.sr_model；2026-10-18 用户令四模式真切换） */
  const [srModel, setSrModel] = useState<'esrgan' | 'dlssnr' | 'rbvsr' | 'seedvr2'>('esrgan');
  const [loading, setLoading] = useState(true);
  const [dialogModels, setDialogModels] = useState<AvailableModel[]>([]);
  const [paintModels, setPaintModels] = useState<AvailableModel[]>([]);
  const [videoModels, setVideoModels] = useState<AvailableModel[]>([]);
  // 视频档位表（2026-10-03 分档具体化）：所选模型携带的「时长↔可选
  // 分辨率」精确配对表；系统默认/清单未携带=静态兜底（与后端默认模型
  // 档同源）。时长/分辨率切档自动校正，UI 层杜绝非法组合
  const videoTiers = videoModels.find((m) => m.id === cfg.videoModel)?.tiers;
  const snapped = snapToTiers(videoTiers, cfg.duration, cfg.quality);

  // 打开时并行拉取三类任务的可用模型（失败保留已保存配置，下拉仍可编辑）
  useEffect(() => {
    let alive = true;
    setLoading(true);
    Promise.all([
      listAvailableModels('dialog'),
      listAvailableModels('paint'),
      listAvailableModels('video'),
    ])
      .then(([dialogRes, paintRes, videoRes]) => {
        if (!alive) return;
        setDialogModels(dialogRes.items);
        setPaintModels(paintRes.items);
        setVideoModels(videoRes.items);
      })
      .catch(() => {
        if (alive) showToast('模型列表加载失败，可稍后重试', 'error');
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [showToast]);

  // 打开时读当前超分分辨率+超分模型（失败保持默认，保存时再写）
  useEffect(() => {
    getVideoSrResolution()
      .then((res) => setSrRes(res.resolution === '2k' ? '2k' : '1k'))
      .catch(() => { /* 读失败保持默认，保存时覆盖 */ });
    getVideoSrModel()
      .then((res) => {
        const m = res.model as 'esrgan' | 'dlssnr' | 'rbvsr' | 'seedvr2';
        if (m === 'esrgan' || m === 'dlssnr' || m === 'rbvsr' || m === 'seedvr2') setSrModel(m);
      })
      .catch(() => { /* 读失败保持默认，保存时覆盖 */ });
  }, []);

  /** 恢复默认：重置表单并立即持久化 */
  const handleReset = () => {
    setCfg({ ...DEFAULT_MODEL_CONFIG });
    writeModelConfig(DEFAULT_MODEL_CONFIG);
    showToast('已恢复默认模型配置', 'success');
  };

  /** 保存：写 localStorage（画幅/时长，供确认弹窗回填），并把三个模型
   *  选型同步到后端 module-config 的 manga-* 槽 default（2026-08-31
   *  接线：此前仅存 localStorage，推理/绘画选型无任何生成链路消费——
   *  用户「模型配置没生效」主诉根因）。后端同步失败仅提示，不阻断本地
   *  保存（画幅/时长仍生效）。 */
  const handleSave = () => {
    // 时长/分辨率按当前档位表校正后落存（档内精确配对）
    writeModelConfig({ ...cfg, ...snapped });
    void (async () => {
      try {
        const res = await getModuleModelConfig();
        const configs = { ...res.config };
        const pairs: Array<[string, string]> = [
          ['manga-dialog', cfg.dialogModel],
          ['manga-paint', cfg.paintModel],
          ['manga-video', cfg.videoModel],
        ];
        for (const [slot, modelId] of pairs) {
          const cur = configs[slot] || { allowed: [], default: '' };
          // 2026-10-04 用户二定路由：选中即权威——后端校验要求 default
          // 同时在 allowed 白名单与候选集内（云端端点已入候选）；选了
          // 不在白名单里的模型（如云端端点）自动补入，保存不再被拒
          const allowed = Array.isArray(cur.allowed) ? [...cur.allowed] : [];
          if (modelId && !allowed.includes(modelId)) allowed.push(modelId);
          configs[slot] = { allowed, default: modelId };
        }
        await saveModuleModelConfig(configs);
      } catch {
        showToast('模型选型同步到后端失败（画幅/时长仍已保存），可稍后重试', 'warning');
        return;
      }
      // 超分分辨率写后端 KV（失败仅提示，不阻断其余保存）
      try {
        await setVideoSrResolution(srRes);
      } catch {
        showToast('超分分辨率保存失败，可稍后重试', 'warning');
        return;
      }
      // 超分模型写后端 KV（2026-10-18：四模式真切换；失败仅提示，不阻断其余保存）
      try {
        await setVideoSrModel(srModel);
      } catch {
        showToast('超分模型保存失败，可稍后重试', 'warning');
        return;
      }
      showToast('模型配置已保存', 'success');
    })();
    onClose();
  };

  return (
    <Modal
      title={
        <span className="flex items-center gap-2">
          <Settings2 size={16} style={{ color: 'var(--color-primary)' }} />
          模型配置
        </span>
      }
      onClose={onClose}
      width={520}
      footer={
        <>
          <button type="button" className="btn btn-ghost" onClick={handleReset}>
            恢复默认
          </button>
          <button type="button" className="btn btn-primary" onClick={handleSave}>
            保存
          </button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        {/* ① 推理模型（AI 生词/实体推理等对话类任务） */}
        <ModelSelect
          label="推理模型"
          value={cfg.dialogModel}
          models={dialogModels}
          loading={loading}
          onChange={(id) => setCfg((c) => ({ ...c, dialogModel: id }))}
        />
        {/* ② 绘画模型（角色生图/分镜生图） */}
        <ModelSelect
          label="绘画模型"
          value={cfg.paintModel}
          models={paintModels}
          loading={loading}
          onChange={(id) => setCfg((c) => ({ ...c, paintModel: id }))}
        />
        {/* ③ 视频模型（生成视频；K-02 真消费 2026-10-21 用户拍板） */}
        <div>
          <ModelSelect
            label="视频模型"
            value={cfg.videoModel}
            models={videoModels}
            loading={loading}
            onChange={(id) => setCfg((c) => ({ ...c, videoModel: id }))}
          />
          <p className="manga-form-tip" style={{ margin: 0 }}>
            保存后下次生成视频生效：选云端模型=走绑定的云端连接（按量计费，
            端点取工位绑定）；本地模型/默认=H3 主力链不变。生成弹窗里单次
            显式选择恒优先于本默认。
          </p>
        </div>

        {/* ④ 超分模型（2026-10-18 用户令：四模式真切换；紧随视频模型） */}
        <div>
          <label className="manga-form-label">超分模型</label>
          <select
            className="manga-modal-select"
            value={srModel}
            aria-label="超分模型"
            onChange={(e) => {
              const v = e.target.value;
              if (v === 'esrgan' || v === 'dlssnr' || v === 'rbvsr' || v === 'seedvr2') setSrModel(v);
            }}
          >
            <option value="esrgan">ESRGAN 逐帧超分（默认 · 1K/2K 随上档）</option>
            <option value="dlssnr">DLSS-NR 2x（NVIDIA 加速 · 约 2 分钟 · 2K）</option>
            <option value="rbvsr">RealBasicVSR 时序修复（约 3 分钟 · 原尺寸）</option>
            <option value="seedvr2">SeedVR2 生成式修复（顶级细节 · 1K 约 8 分钟 / 2K 约 90 分钟）</option>
          </select>
          <p className="manga-form-tip" style={{ margin: 0 }}>
            「超分视频」列点击超分时走的真链路；保存后下次超分即时生效。
            DLSS-NR 固定 2K 输出；RealBasicVSR 固定原尺寸；SeedVR2/ESRGAN
            随下方超分分辨率档位。
          </p>
        </div>

        {/* ⑤ 视频默认参数：画幅 + 时长（档位驱动）+ 分辨率 */}
        <div className="manga-modelcfg-grid">
          <div>
            <label className="manga-form-label">默认画幅</label>
            <select
              className="manga-modal-select"
              value={cfg.aspect}
              aria-label="默认画幅"
              onChange={(e) => setCfg((c) => ({ ...c, aspect: e.target.value === '9:16' ? '9:16' : '16:9' }))}
            >
              <option value="16:9">横屏 16:9</option>
              <option value="9:16">竖屏 9:16</option>
            </select>
          </div>
          <div>
            <label className="manga-form-label">默认时长</label>
            <select
              className="manga-modal-select"
              value={String(snapped.duration)}
              aria-label="默认时长"
              onChange={(e) => setCfg((c) => ({
                ...c,
                ...snapToTiers(videoTiers, Number(e.target.value), c.quality),
              }))}
            >
              {tierDurations(videoTiers).map((d) => (
                <option key={d} value={String(d)}>
                  {d} 秒
                </option>
              ))}
            </select>
          </div>
        </div>
        <div>
          <label className="manga-form-label">默认视频分辨率</label>
          <select
            className="manga-modal-select"
            value={snapped.quality}
            aria-label="默认视频分辨率"
            onChange={(e) => setCfg((c) => ({ ...c, quality: e.target.value }))}
          >
            {tierQualities(videoTiers, snapped.duration).map((q) => (
              <option key={q} value={q}>
                {q === '720p' ? '720p 高清' : q === '480p' ? '480p 流畅' : q}
              </option>
            ))}
          </select>
          <p className="manga-form-tip" style={{ margin: 0 }}>
            时长与分辨率选项随所选视频模型的档位联动（每档精确配对，如
            720p 档仅 4/8 秒时长可选）。
          </p>
        </div>

        {/* ⑥ 超分分辨率（2026-10-12 用户令：超分视频列的目标档位） */}
        <div>
          <label className="manga-form-label">超分分辨率</label>
          <select
            className="manga-modal-select"
            value={srRes}
            aria-label="超分分辨率"
            onChange={(e) => setSrRes(e.target.value === '2k' ? '2k' : '1k')}
          >
            <option value="1k">1K（1344×768，超分更快）</option>
            <option value="2k">2K（2688×1536，细节更足）</option>
          </select>
          <p className="manga-form-tip" style={{ margin: 0 }}>
            「超分视频」列点击超分时的目标档位；保存后下次超分即时生效。
          </p>
        </div>

        <p className="manga-form-tip" style={{ margin: 0 }}>
          保存后作为「生成视频」确认弹窗的默认参数；模型留空 = 系统默认自动选择。
          云端模型由「设置 → 云端 API 服务」绑定后出现在上方下拉（标「云端·计费」）；
          绘画/视频环节的云端路由由该绑定决定。
        </p>
      </div>
    </Modal>
  );
}
// 本项目仅供学习使用，商业授权请+Q 3559331368
