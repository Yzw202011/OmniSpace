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
import { listAvailableModels } from '@/services/mangaApi';
import { getModuleModelConfig, saveModuleModelConfig } from '@/services/modelApi';
import CloudSlotBadge from '@/components/common/CloudSlotBadge';
import { MODEL_AVAILABLE_STATUS_LABELS } from '@/constants/statusLabels';
import { DEFAULT_MODEL_CONFIG, writeModelConfig, type MangaModelConfig } from '@/constants/modelConfig';
import { VIDEO_DURATION_OPTIONS, loadModelConfig } from '@/constants/modelConfig';
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
            <option key={m.id} value={m.id} disabled={m.status !== 'ready' && m.status !== 'downloaded'}>
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
  const [loading, setLoading] = useState(true);
  const [dialogModels, setDialogModels] = useState<AvailableModel[]>([]);
  const [paintModels, setPaintModels] = useState<AvailableModel[]>([]);
  const [videoModels, setVideoModels] = useState<AvailableModel[]>([]);

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
    writeModelConfig(cfg);
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
          configs[slot] = { allowed: cur.allowed, default: modelId };
        }
        await saveModuleModelConfig(configs);
      } catch {
        showToast('模型选型同步到后端失败（画幅/时长仍已保存），可稍后重试', 'warning');
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
        {/* ③ 视频模型（生成视频） */}
        <ModelSelect
          label="视频模型"
          value={cfg.videoModel}
          models={videoModels}
          loading={loading}
          onChange={(id) => setCfg((c) => ({ ...c, videoModel: id }))}
        />

        {/* ④ 视频默认参数：画幅 + 时长 */}
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
              value={String(cfg.duration)}
              aria-label="默认时长"
              onChange={(e) => setCfg((c) => ({ ...c, duration: Number(e.target.value) }))}
            >
              {VIDEO_DURATION_OPTIONS.map((d) => (
                <option key={d} value={String(d)}>
                  {d} 秒
                </option>
              ))}
            </select>
          </div>
        </div>

        <p className="manga-form-tip" style={{ margin: 0 }}>
          保存后作为「生成视频」确认弹窗的默认参数；模型留空 = 系统默认自动选择。
        </p>

        {/* ⑤ 批3 P5：云端 API 工位（manga.text / manga.video）——绑定即
            该环节走云端（零显存占用）；点击「去配置」直达设置页 AI 服务签 */}
        <div className="manga-modelcfg-grid">
          <CloudSlotBadge slots={[
            { key: 'manga.text', label: '漫剧文本' },
            { key: 'manga.video', label: '漫剧视频' },
          ]} />
        </div>
      </div>
    </Modal>
  );
}
// 本项目仅供学习使用，商业授权请+Q 3559331368
