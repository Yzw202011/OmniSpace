/**
 * NovelModelConfigModal —— 写作台「模型设置」弹窗（2026-09-28 用户拍板：
 * 剔除页内云端徽章，改为独立弹窗可选本地/云端写作模型）。
 *
 * 结构对齐 ComicModelConfigModal（本地/云端二选一，与设置页同真源联动）：
 * - 本地档：dialog 类已下载模型（listAvailableModels('dialog')）
 * - 云端档：cloudApi providers（cloud:<provider>|<model>）
 * - 保存：clear/set novel.text 云端绑定 + module-config 槽持久化（novel 槽
 *   的 default 记本地选择，供 _ensure_engine 本地偏好链未来消费）
 */
import { useEffect, useState } from 'react';
import { Cloud, Loader2, Settings2 } from 'lucide-react';
import { useAppStore } from '@/stores/useAppStore';
import {
  clearCloudBinding,
  listCloudBindings,
  listCloudProviders,
  setCloudBinding,
} from '@/services/cloudApi';
import {
  getModuleModelConfig,
  saveModuleModelConfig,
} from '@/services/modelApi';
import { listAvailableModels } from '@/services/mangaApi';
import { MODEL_AVAILABLE_STATUS_LABELS } from '@/constants/statusLabels';
import type { AvailableModel } from '@/types';
import type { CloudProvider } from '@/services/cloudApi';
import { reportBgError } from '@/utils/errors';
import { Modal } from '../common/Modal';

const CLOUD_VAL = (p: CloudProvider, m: string) => `cloud:${p.id}|${m}`;
const parseCloudVal = (v: string): { provider_id: string; model: string } | null => {
  if (!v.startsWith('cloud:')) return null;
  const [provider_id, model] = v.slice(6).split('|');
  return provider_id && model ? { provider_id, model } : null;
};

const SLOT = 'novel.text';
/** module-config 槽名（本地选择持久化；后端 _MODULE_MODEL_SLOTS 的 dialog 槽） */
const MODULE_SLOT = 'dialog';

function ModelSelect({
  label, value, onChange, localModels, providers, hint,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  localModels: AvailableModel[];
  providers: CloudProvider[];
  hint?: string;
}) {
  const enabledProviders = providers.filter((p) => p.enabled);
  const knownLocal = localModels.some((m) => m.id === value);
  return (
    <div>
      <label className="manga-form-label">{label}</label>
      <select
        className="manga-modal-select"
        value={value}
        aria-label={label}
        onChange={(e) => onChange(e.target.value)}
      >
        <option value="__local__">本地 · 自动（8B 优先，缺失回退当前对话模型）</option>
        {localModels.map((m) => (
          <option key={m.id} value={m.id} disabled={m.status !== 'ready' && m.status !== 'downloaded'}>
            {`本地 · ${m.name}（${MODEL_AVAILABLE_STATUS_LABELS[m.status]}）`}
          </option>
        ))}
        {!knownLocal && value && !value.startsWith('cloud:') && value !== '__local__' && (
          <option value={value} disabled>{`本地 · ${value}（已保存，当前不可用）`}</option>
        )}
        {enabledProviders.map((p) =>
          (p.models?.length ? p.models : ['']).map((m) => (
            <option key={`${p.id}|${m}`} value={CLOUD_VAL(p, m)}>
              {`云端 · ${p.name}${m ? ` · ${m}` : ' · 默认模型'}（不占显存）`}
            </option>
          )),
        )}
      </select>
      {hint && <p className="manga-form-tip" style={{ margin: 0 }}>{hint}</p>}
    </div>
  );
}

export default function NovelModelConfigModal({ onClose }: { onClose: () => void }) {
  const showToast = useAppStore((s) => s.showToast);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  /** ''=尚未拉到（禁保存）；'__local__'=本地自动；本地模型id；cloud:x|m=云端 */
  const [textVal, setTextVal] = useState('');
  const [localModels, setLocalModels] = useState<AvailableModel[]>([]);
  const [providers, setProviders] = useState<CloudProvider[]>([]);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    Promise.all([
      listCloudBindings(),
      listCloudProviders(),
      listAvailableModels('dialog'),
      getModuleModelConfig(),
    ]).then(([bind, provs, models, cfg]) => {
      if (!alive) return;
      const b = bind.bindings?.[SLOT];
      setLocalModels(models.items ?? []);
      setProviders(provs.providers ?? []);
      if (b?.provider_id && b.model) {
        setTextVal(`cloud:${b.provider_id}|${b.model}`);
      } else {
        // 本地选择持久化在 module-config 的 novel 子键（约定 novel_local）
        const saved = (cfg.config?.[MODULE_SLOT]?.default) || '';
        setTextVal(saved && (models.items ?? []).some((m) => m.id === saved)
          ? saved : '__local__');
      }
    }).catch((err) => {
      if (!alive) return;
      reportBgError?.('NovelModelConfigModal', err as Error);
      showToast('模型配置读取失败，请重试', 'error');
    }).finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleSave = async () => {
    setSaving(true);
    try {
      const cloud = parseCloudVal(textVal);
      if (cloud) {
        await setCloudBinding(SLOT, cloud);
      } else {
        await clearCloudBinding(SLOT);
      }
      // 本地选择持久化（云端时清空，回落自动档）
      const cfgRes = await getModuleModelConfig();
      const configs = { ...cfgRes.config };
      const cur = configs[MODULE_SLOT] || { allowed: [], default: '' };
      const localPick = (textVal !== '' && textVal !== '__local__' && !cloud)
        ? textVal : '';
      configs[MODULE_SLOT] = { allowed: cur.allowed, default: localPick };
      await saveModuleModelConfig(configs);
      showToast('写作模型设置已保存（与设置页实时同步）', 'success');
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

  const handleReset = () => {
    setTextVal('__local__');
    showToast('已恢复默认（保存后生效）', 'info');
  };

  return (
    <Modal
      title="写作模型设置"
      width={520}
      onClose={onClose}
      footer={
        <div className="flex items-center gap-2 justify-end">
          <button className="btn btn-ghost btn-sm" onClick={handleReset} disabled={saving}>
            恢复默认
          </button>
          <button className="btn btn-primary btn-sm" onClick={handleSave} disabled={loading || saving}>
            {saving ? '保存中…' : '保存'}
          </button>
        </div>
      }
    >
      {loading ? (
        <div className="flex items-center gap-2 text-xs" style={{ color: 'var(--color-text-secondary)' }}>
          <Loader2 size={14} className="animate-spin" /> 加载中…
        </div>
      ) : (
        <div className="flex flex-col gap-4">
          <ModelSelect
            label="写作文本（大纲 / 章节 / 摘要 / 角色卡）"
            value={textVal}
            onChange={setTextVal}
            localModels={localModels}
            providers={providers}
            hint="选「云端」= 写作走服务商（零显存占用、按量计费）；选「本地」= 走本机对话引擎"
          />
          <p className="text-xs" style={{ color: 'var(--color-text-tertiary)', margin: 0 }}>
            本弹窗与「设置 → 云端 API 服务」读写同一份配置，两边改动实时互通
          </p>
        </div>
      )}
    </Modal>
  );
}
