// 本项目仅供学习使用，商业授权请+Q 3559331368
/* ==========================================================================
 * ComicModelConfigModal.tsx —— AI 漫画·模型设置弹窗（2026-09-22）
 * --------------------------------------------------------------------------
 * 用户令：漫画页模型设置可选 本地模型 / 云端 API，且与设置页联动。
 * 联动机制=同一真源：本弹窗与「设置 → 云端 API 服务」读写同一份
 * cloud.bindings（keyframe.image / asset.image）与 module-config
 * （manga-paint / manga-dialog），无缓存——两边任一处改动，另一处
 * 下次打开即如实反映；顶栏 CloudSlotBadge 继续显示云端状态。
 *   - 分格出图 / 资产图：单下拉混排「本地·智能路由 + 本地清单 +
 *     云端连接模型」；选云端 = PUT 绑定，选本地 = 清除绑定回本地
 *     （设置页工位同语义）；云端出图零显存占用、按服务商计费
 *   - AI 写分格：本地对话模型下拉（暂仅本地，存 manga-dialog default）
 * ========================================================================== */

import { useEffect, useState } from 'react';
import { Cloud, Loader2, Settings2 } from 'lucide-react';
import { useAppStore } from '@/stores/useAppStore';
import {
  clearCloudBinding,
  listCloudBindings,
  listCloudProviders,
  setCloudBinding,
  type CloudBinding,
  type CloudProvider,
} from '@/services/cloudApi';
import { listAvailableModels } from '@/services/mangaApi';
import {
  getModuleModelConfig,
  saveModuleModelConfig,
} from '@/services/modelApi';
import { MODEL_AVAILABLE_STATUS_LABELS } from '@/constants/statusLabels';
import type { AvailableModel } from '@/types';
import { Modal } from '../common/Modal';

/** 云端选项值编码：cloud:<provider_id>|<model> */
const CLOUD_VAL = (p: CloudProvider, m: string) => `cloud:${p.id}|${m}`;
const parseCloudVal = (v: string): { provider_id: string; model: string } | null => {
  if (!v.startsWith('cloud:')) return null;
  const [provider_id, model] = v.slice(6).split('|');
  return provider_id && model ? { provider_id, model } : null;
};

/** 出图工位二选一下拉（本地混排云端） */
function ImageSlotSelect({
  label,
  hint,
  value,
  loading,
  localModels,
  providers,
  onChange,
}: {
  label: string;
  hint: string;
  value: string;
  loading: boolean;
  localModels: AvailableModel[];
  providers: CloudProvider[];
  onChange: (v: string) => void;
}) {
  const imageProviders = providers.filter(
    (p) => p.enabled && (p.protocol ?? '').toLowerCase().includes('image'),
  );
  const savedMissing =
    value && value !== '__auto__' && !parseCloudVal(value)
    && !localModels.some((m) => m.id === value);
  return (
    <div>
      <label className="manga-form-label">{label}</label>
      {loading ? (
        <span className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)' }}>
          <Loader2 size={12} style={{ animation: 'spin 1s linear infinite', verticalAlign: -2 }} /> 加载中…
        </span>
      ) : (
        <select
          className="manga-modal-select"
          value={value}
          aria-label={label}
          onChange={(e) => onChange(e.target.value)}
        >
          <option value="__auto__">本地 · 智能路由（自动选择）</option>
          {localModels.map((m) => (
            <option key={m.id} value={m.id} disabled={m.status !== 'ready' && m.status !== 'downloaded'}>
              {`本地 · ${m.name}（${MODEL_AVAILABLE_STATUS_LABELS[m.status]}）`}
            </option>
          ))}
          {savedMissing && <option value={value} disabled>{`本地 · ${value}（已保存，当前不可用）`}</option>}
          {imageProviders.map((p) =>
            (p.models?.length ? p.models : ['']).map((m) => (
              <option key={`${p.id}|${m}`} value={CLOUD_VAL(p, m)}>
                {`云端 · ${p.name}${m ? ` · ${m}` : ' · 默认模型'}（零显存占用）`}
              </option>
            )),
          )}
        </select>
      )}
      <p className="manga-form-tip" style={{ margin: 0 }}>{hint}</p>
    </div>
  );
}

export default function ComicModelConfigModal({ onClose }: { onClose: () => void }) {
  const showToast = useAppStore((s) => s.showToast);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  // 出图两工位：''=尚未拉到（禁保存）；'__auto__'=本地智能路由；模型id=本地指定；cloud:x|m=云端
  const [keyframeVal, setKeyframeVal] = useState('');
  const [assetVal, setAssetVal] = useState('');
  // 写分格（本地对话模型，''=系统默认）
  const [dialogVal, setDialogVal] = useState('');
  const [localPaint, setLocalPaint] = useState<AvailableModel[]>([]);
  const [localDialog, setLocalDialog] = useState<AvailableModel[]>([]);
  const [providers, setProviders] = useState<CloudProvider[]>([]);

  // 打开时拉取真值（绑定 + 本地清单 + 云端连接 + module-config）
  useEffect(() => {
    let alive = true;
    setLoading(true);
    Promise.all([
      listCloudBindings(),
      listCloudProviders(),
      listAvailableModels('paint'),
      listAvailableModels('dialog'),
      getModuleModelConfig(),
    ])
      .then(([bind, provs, paint, dialog, cfg]) => {
        if (!alive) return;
        const slotVal = (slot: string, localDefault: string): string => {
          const b: CloudBinding | undefined = bind.bindings?.[slot];
          if (b?.provider_id && b.model) return `cloud:${b.provider_id}|${b.model}`;
          return localDefault || '__auto__';
        };
        setKeyframeVal(slotVal('keyframe.image', cfg.config?.['manga-paint']?.default ?? ''));
        setAssetVal(slotVal('asset.image', cfg.config?.['manga-paint']?.default ?? ''));
        setDialogVal(cfg.config?.['manga-dialog']?.default ?? '');
        setLocalPaint(paint.items);
        setLocalDialog(dialog.items);
        setProviders(provs.providers ?? []);
      })
      .catch(() => {
        if (alive) showToast('模型配置加载失败，请稍后重试', 'error');
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [showToast]);

  /** 恢复默认：出图两工位回本地智能路由 + 写分格回系统默认 */
  const handleReset = () => {
    setKeyframeVal('__auto__');
    setAssetVal('__auto__');
    setDialogVal('');
    showToast('已恢复默认（保存后生效）', 'info');
  };

  /** 保存：出图工位写/清 cloud.bindings；本地选型与写分格写 module-config */
  const handleSave = async () => {
    setSaving(true);
    try {
      const slotPairs: Array<[string, string]> = [
        ['keyframe.image', keyframeVal],
        ['asset.image', assetVal],
      ];
      for (const [slot, val] of slotPairs) {
        const cloud = parseCloudVal(val);
        if (cloud) {
          await setCloudBinding(slot, cloud);
        } else {
          await clearCloudBinding(slot);
        }
      }
      // 本地出图选型（manga-paint default：两工位取后改者）+ 写分格
      const res = await getModuleModelConfig();
      const configs = { ...res.config };
      const localPaintPick = keyframeVal !== '' && !parseCloudVal(keyframeVal)
        ? keyframeVal
        : assetVal !== '' && !parseCloudVal(assetVal) ? assetVal : '';
      const curPaint = configs['manga-paint'] || { allowed: [], default: '' };
      configs['manga-paint'] = { allowed: curPaint.allowed, default: localPaintPick === '__auto__' ? '' : localPaintPick };
      const curDlg = configs['manga-dialog'] || { allowed: [], default: '' };
      configs['manga-dialog'] = { allowed: curDlg.allowed, default: dialogVal };
      await saveModuleModelConfig(configs);
      showToast('模型设置已保存（与设置页实时同步）', 'success');
      onClose();
    } catch (err) {
      showToast(
        err instanceof Error ? err.message : '保存失败，请稍后重试',
        'error',
      );
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      title={
        <span className="flex items-center gap-2">
          <Settings2 size={16} style={{ color: 'var(--color-primary)' }} />
          模型设置
        </span>
      }
      onClose={onClose}
      width={560}
      footer={
        <>
          <button type="button" className="btn btn-ghost" onClick={handleReset}>
            恢复默认
          </button>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => void handleSave()}
            disabled={loading || saving || keyframeVal === '' || assetVal === ''}
          >
            {saving ? '保存中…' : '保存'}
          </button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <ImageSlotSelect
          label="分格出图（漫画画面生成）"
          hint="选「云端」= 出图走服务商（零显存占用、按量计费）；选「本地」= 走本机显卡"
          value={keyframeVal}
          loading={loading}
          localModels={localPaint}
          providers={providers}
          onChange={setKeyframeVal}
        />
        <ImageSlotSelect
          label="资产图（角色 / 场景 / 道具）"
          hint="与分格出图独立配置；本地固定项（四视图底座等）不受影响"
          value={assetVal}
          loading={loading}
          localModels={localPaint}
          providers={providers}
          onChange={setAssetVal}
        />
        <div>
          <label className="manga-form-label">AI 写分格（文字 · 暂仅本地）</label>
          {loading ? (
            <span className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)' }}>
              <Loader2 size={12} style={{ animation: 'spin 1s linear infinite', verticalAlign: -2 }} /> 加载中…
            </span>
          ) : (
            <select
              className="manga-modal-select"
              value={dialogVal}
              aria-label="AI 写分格模型"
              onChange={(e) => setDialogVal(e.target.value)}
            >
              <option value="">系统默认（自动选择）</option>
              {localDialog.map((m) => (
                <option key={m.id} value={m.id} disabled={m.status !== 'ready' && m.status !== 'downloaded'}>
                  {`${m.name}（${MODEL_AVAILABLE_STATUS_LABELS[m.status]}）`}
                </option>
              ))}
            </select>
          )}
          <p className="manga-form-tip" style={{ margin: 0 }}>
            写分格用本地对话模型生成；云端文字模型接入属后续版本
          </p>
        </div>

        <div className="flex items-center gap-2 text-xs text-tertiary">
          <Cloud size={12} aria-hidden="true" />
          本弹窗与「设置 → 云端 API 服务」读写同一份配置，两边改动实时互通
        </div>
      </div>
    </Modal>
  );
}
