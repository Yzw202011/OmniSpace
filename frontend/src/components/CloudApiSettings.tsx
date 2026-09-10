/* ==========================================================================
 * OmniSpace AI —— 云端 API 服务设置卡片（批1：地基+文本，2026-09-06）
 * --------------------------------------------------------------------------
 * 用户自带 API Key 直连服务商（软件不经手不中转不抽成）：
 *   - 连接列表：名称/地址/打码 Key/启停/测试/编辑/删除
 *   - 添加/编辑连接：名称、地址、API Key（留空=保持原值）、模型列表
 *   - 工位绑定：「AI 对话与写作」→ 某连接的某模型（或本地引擎）
 * 对齐后端 backend/api/cloud.py；方案真源=docs/云端API接入方案-2026-09-06.md
 * ========================================================================== */

import { useCallback, useEffect, useState } from 'react';
import { Cloud } from 'lucide-react';
import { useAppStore } from '@/stores/useAppStore';
import {
  clearCloudBinding,
  createCloudProvider,
  deleteCloudProvider,
  fetchCloudProviderModels,
  listCloudBindings,
  listCloudProviders,
  setCloudBinding,
  testCloudProvider,
  updateCloudProvider,
  type CloudBinding,
  type CloudProvider,
  type CloudSlots,
} from '@/services/cloudApi';

interface ProviderForm {
  name: string;
  base_url: string;
  api_key: string;
  protocol: CloudProtocol;
  models_text: string;
}

/** 协议选项（批3：文本 + 图片两套 + 视频；后端 VALID_PROTOCOLS 同源） */
type CloudProtocol = 'openai_text' | 'openai_image' | 'task_image' | 'task_video';

const PROTOCOL_OPTIONS: Array<{ value: CloudProtocol; label: string; desc: string }> = [
  { value: 'openai_text', label: '文本（OpenAI 兼容）', desc: '对话/写作，如 DeepSeek、Kimi、硅基流动' },
  { value: 'openai_image', label: '图片·同步', desc: '/v1/images/generations 同步出图，如硅基流动、Recraft' },
  { value: 'task_image', label: '图片·异步任务', desc: '提交→轮询→下载三段式，如通义万相（DashScope）' },
  { value: 'task_video', label: '视频·异步任务', desc: '图生视频三段式，如通义万相视频（DashScope）' },
];

const PROTOCOL_LABELS: Record<string, string> = {
  openai_text: '文本',
  openai_image: '图片·同步',
  task_image: '图片·异步',
  task_video: '视频·异步',
};

/** 工位协议匹配（与后端 set_binding 校验同源） */
function protocolMatchesSlot(protocol: string, slot: string): boolean {
  if (slot === 'dialog.text' || slot === 'manga.text' || slot === 'novel.text') {
    return protocol === 'openai_text';
  }
  if (slot === 'manga.video') return protocol === 'task_video';
  return protocol === 'openai_image' || protocol === 'task_image';
}

/** 槽位附加说明（隐私/前置条件/组合提示） */
const SLOT_HINTS: Record<string, string> = {
  'dialog.text': '对话页专用；漫剧文字与写作台可分别绑定不同连接',
  'manga.text': 'AI 切分与描述词生成；与对话页可各绑各的（VLM 一致性评分恒本地）',
  'novel.text': '写作台/小说创作专用连接',
  'manga.video': '绑定后生成视频走云端（该分镜须已有关键帧；首帧图会上传给服务商，费用按条计）',
};

const EMPTY_FORM: ProviderForm = { name: '', base_url: '', api_key: '', protocol: 'openai_text', models_text: '' };

export default function CloudApiSettings() {
  const showToast = useAppStore((s) => s.showToast);
  const [providers, setProviders] = useState<CloudProvider[]>([]);
  const [slots, setSlots] = useState<CloudSlots>({});
  const [bindings, setBindings] = useState<Record<string, CloudBinding>>({});
  const [loading, setLoading] = useState(true);
  const [formOpen, setFormOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState<ProviderForm>(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  const [testingId, setTestingId] = useState('');
  const [testResults, setTestResults] = useState<Record<string, { ok: boolean; text: string } | null>>({});

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [pr, bd] = await Promise.all([listCloudProviders(), listCloudBindings()]);
      setProviders(pr.providers ?? []);
      setSlots(pr.slots ?? {});
      setBindings(bd.bindings ?? {});
    } catch (e) {
      showToast((e as Error).message, 'error');
    } finally {
      setLoading(false);
    }
  }, [showToast]);

  useEffect(() => {
    void load();
  }, [load]);

  function openCreate() {
    setEditingId(null);
    setForm(EMPTY_FORM);
    setFormOpen(true);
  }

  function openEdit(p: CloudProvider) {
    setEditingId(p.id);
    setForm({
      name: p.name,
      base_url: p.base_url,
      api_key: '', // 打码回显不还原：留空=保持原值
      protocol: (p.protocol as CloudProtocol) || 'openai_text',
      models_text: (p.models ?? []).join('\n'),
    });
    setFormOpen(true);
  }

  /** 保存连接（新增或更新） */
  async function handleSaveForm() {
    if (!form.name.trim() || !form.base_url.trim()) {
      showToast('请填写名称和服务地址', 'error');
      return;
    }
    setSaving(true);
    try {
      const body = {
        name: form.name,
        protocol: form.protocol,
        base_url: form.base_url,
        api_key: form.api_key, // 空串=保持原值（后端语义）
        models: form.models_text.split(/\n+/).map((s) => s.trim()).filter(Boolean),
      };
      if (editingId) {
        await updateCloudProvider(editingId, body);
        showToast('连接已更新', 'success');
      } else {
        await createCloudProvider(body);
        showToast('连接已添加', 'success');
      }
      setFormOpen(false);
      await load();
    } catch (e) {
      showToast((e as Error).message, 'error');
    } finally {
      setSaving(false);
    }
  }

  async function handleToggle(p: CloudProvider) {
    try {
      await updateCloudProvider(p.id, { name: p.name, protocol: p.protocol, base_url: p.base_url, enabled: !p.enabled });
      await load();
    } catch (e) {
      showToast((e as Error).message, 'error');
    }
  }

  async function handleTest(p: CloudProvider) {
    setTestingId(p.id);
    setTestResults((prev) => ({ ...prev, [p.id]: null }));
    try {
      const r = await testCloudProvider({ provider_id: p.id });
      setTestResults((prev) => ({
        ...prev,
        [p.id]: { ok: r.reachable, text: r.reachable ? '连接成功' : `不可达：${r.detail}` },
      }));
    } catch (e) {
      setTestResults((prev) => ({ ...prev, [p.id]: { ok: false, text: (e as Error).message } }));
    } finally {
      setTestingId('');
    }
  }

  async function handleDelete(p: CloudProvider) {
    if (!window.confirm(`确定删除连接「${p.name}」？指向它的工位绑定会一并解除。`)) return;
    try {
      await deleteCloudProvider(p.id);
      showToast('连接已删除', 'success');
      await load();
    } catch (e) {
      showToast((e as Error).message, 'error');
    }
  }

  async function handleFetchModels(p: CloudProvider) {
    showToast('正在拉取模型列表…', 'info');
    try {
      const r = await fetchCloudProviderModels(p.id);
      if (r.error) {
        showToast(`拉取失败：${r.error}`, 'error');
        return;
      }
      if (r.models.length === 0) {
        showToast('服务商未返回任何模型', 'error');
        return;
      }
      // 拉到的模型直接并入该连接（编辑态不打断）
      await updateCloudProvider(p.id, { name: p.name, protocol: p.protocol, base_url: p.base_url, models: r.models });
      showToast(`已更新 ${r.models.length} 个模型`, 'success');
      await load();
    } catch (e) {
      showToast((e as Error).message, 'error');
    }
  }

  /** 工位绑定下拉当前值（providerId|model 编码） */
  function bindingValue(slot: string): string {
    const b = bindings[slot];
    if (!b || !b.provider_id) return '';
    return `${b.provider_id}|${b.model ?? ''}`;
  }

  async function handleBindChange(slot: string, value: string) {
    try {
      if (!value) {
        await clearCloudBinding(slot);
        showToast('已恢复本地引擎', 'success');
      } else {
        const idx = value.indexOf('|');
        const providerId = value.slice(0, idx);
        const model = value.slice(idx + 1);
        await setCloudBinding(slot, { provider_id: providerId, model });
        showToast('已切换为云端', 'success');
      }
      await load();
    } catch (e) {
      showToast((e as Error).message, 'error');
    }
  }

  /** 绑定下拉选项（按槽位协议过滤）：启用中的连接 × 其模型列表 */
  function bindOptions(slot: string): Array<{ value: string; label: string }> {
    const out: Array<{ value: string; label: string }> = [];
    for (const p of providers) {
      if (!p.enabled || !protocolMatchesSlot(p.protocol, slot)) continue;
      const models = (p.models ?? []).length > 0 ? p.models : [''];
      for (const m of models) {
        out.push({ value: `${p.id}|${m}`, label: m ? `${p.name} · ${m}` : `${p.name} · 默认模型` });
      }
    }
    return out;
  }

  const slotEntries = Object.entries(slots);

  return (
    <div className="settings-section card">
      <div className="settings-section-header">
        <h2 className="settings-section-title">
          <Cloud size={16} style={{ verticalAlign: '-2px', marginRight: 6 }} aria-hidden="true" />
          云端 API 服务
        </h2>
        <button className="btn btn-primary btn-sm" onClick={openCreate}>添加服务商</button>
      </div>
      <p className="settings-section-desc">
        使用你自己的 API Key 直连云端大模型（OpenAI 兼容文本服务）：
        Key 仅保存在本机，请求由你的电脑直达服务商，软件不经手、不中转、不抽成；
        用量费用按服务商账单结算。绑定工位后该功能由云端承载，本地显卡零占用，
        可与绘画/训练同时进行。
      </p>

      {loading ? (
        <div className="settings-loading">加载中…</div>
      ) : providers.length === 0 && !formOpen ? (
        <div className="settings-empty">
          <p>还没有云端连接。添加一个服务商并粘贴 API Key 即可开始。</p>
        </div>
      ) : (
        <div className="settings-list">
          {providers.map((p) => {
            const tr = testResults[p.id];
            const usedBy = Object.entries(bindings)
              .filter(([, b]) => b.provider_id === p.id)
              .map(([slot]) => slots[slot] || slot);
            return (
              <div key={p.id} className="settings-row" style={{ alignItems: 'flex-start', flexDirection: 'column', gap: 6 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
                  <span className="settings-row-name">{p.name}</span>
                  <span className="settings-row-desc">{PROTOCOL_LABELS[p.protocol] || p.protocol}</span>
                  <span className="settings-row-desc">{p.base_url}</span>
                  {p.api_key_masked ? <span className="settings-row-desc">Key {p.api_key_masked}</span> : <span className="settings-row-desc">未填 Key</span>}
                  <span
                    className="settings-row-desc"
                    style={{ color: p.enabled ? 'var(--color-success, #4ade80)' : '#f87171' }}
                  >
                    {p.enabled ? '已启用' : '已停用'}
                  </span>
                  {usedBy.length > 0 && (
                    <span className="settings-row-desc" style={{ color: 'var(--color-primary, var(--color-primary-500))' }}>
                      使用中：{usedBy.join('、')}
                    </span>
                  )}
                </div>
                {(p.models ?? []).length > 0 && (
                  <div className="settings-row-desc">模型：{p.models.join('、')}</div>
                )}
                <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                  <button className="btn btn-ghost btn-sm" disabled={!p.enabled || testingId === p.id} onClick={() => void handleTest(p)}>
                    {testingId === p.id ? '测试中…' : '测试连接'}
                  </button>
                  <button className="btn btn-ghost btn-sm" disabled={!p.enabled} onClick={() => void handleFetchModels(p)}>
                    拉取模型列表
                  </button>
                  <button className="btn btn-secondary btn-sm" onClick={() => openEdit(p)}>编辑</button>
                  <button className="btn btn-ghost btn-sm" onClick={() => void handleToggle(p)}>
                    {p.enabled ? '停用' : '启用'}
                  </button>
                  <button className="btn btn-ghost btn-sm" style={{ color: '#f87171' }} onClick={() => void handleDelete(p)}>
                    删除
                  </button>
                </div>
                {tr && (
                  <div role="status" className="settings-row-desc" style={{ color: tr.ok ? 'var(--color-success, #4ade80)' : '#f87171' }}>
                    {tr.text}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* 添加/编辑连接表单 */}
      {formOpen && (
        <div style={{ borderTop: '1px solid var(--color-border, rgba(255,255,255,0.1))', marginTop: 12, paddingTop: 12 }}>
          <div className="settings-row">
            <div className="settings-row-label">
              <span className="settings-row-name">名称</span>
            </div>
            <div className="settings-row-control">
              <input className="input settings-input" style={{ minWidth: 280 }} placeholder="如 DeepSeek / 通义万相"
                value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
            </div>
          </div>
          <div className="settings-row">
            <div className="settings-row-label">
              <span className="settings-row-name">连接类型</span>
              <span className="settings-row-desc">决定该连接能绑定到哪类工位</span>
            </div>
            <div className="settings-row-control">
              <select
                className="input settings-input"
                style={{ minWidth: 280 }}
                value={form.protocol}
                onChange={(e) => setForm({ ...form, protocol: e.target.value as CloudProtocol })}
                disabled={Boolean(editingId)}
              >
                {PROTOCOL_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value} title={o.desc}>{o.label}</option>
                ))}
              </select>
              <div className="settings-row-desc" style={{ marginTop: 4 }}>
                {PROTOCOL_OPTIONS.find((o) => o.value === form.protocol)?.desc}
              </div>
            </div>
          </div>
          <div className="settings-row">
            <div className="settings-row-label">
              <span className="settings-row-name">服务地址</span>
              <span className="settings-row-desc">
                {form.protocol === 'openai_text'
                  ? 'OpenAI 兼容基础地址，如 https://api.deepseek.com'
                  : 'DashScope 形态地址，如 https://dashscope.aliyuncs.com'}
              </span>
            </div>
            <div className="settings-row-control">
              <input className="input settings-input" style={{ minWidth: 280 }}
                placeholder={form.protocol === 'openai_text' ? 'https://api.deepseek.com' : 'https://dashscope.aliyuncs.com'}
                value={form.base_url} onChange={(e) => setForm({ ...form, base_url: e.target.value })} />
            </div>
          </div>
          <div className="settings-row">
            <div className="settings-row-label">
              <span className="settings-row-name">API Key</span>
              <span className="settings-row-desc">{editingId ? '留空 = 保持原 Key 不变' : '服务商控制台获取，仅保存在本机'}</span>
            </div>
            <div className="settings-row-control">
              <input className="input settings-input" style={{ minWidth: 280 }} type="password" placeholder="sk-…"
                value={form.api_key} onChange={(e) => setForm({ ...form, api_key: e.target.value })} />
            </div>
          </div>
          <div className="settings-row">
            <div className="settings-row-label">
              <span className="settings-row-name">模型列表</span>
              <span className="settings-row-desc">
                {form.protocol === 'task_image'
                  ? '每行一个，如 wanx2.1-t2i-turbo（文生图）/ qwen-image-edit（图生图，漫剧参考图走它）'
                  : form.protocol === 'task_video'
                    ? '每行一个，如 wanx-i2v-turbo（图生视频，以关键帧为首帧）'
                    : '每行一个；也可保存后点「拉取模型列表」自动获取'}
              </span>
            </div>
            <div className="settings-row-control">
              <textarea className="input settings-input" style={{ minWidth: 280, minHeight: 64 }}
                placeholder={form.protocol === 'task_image'
                  ? 'wanx2.1-t2i-turbo\nqwen-image-edit'
                  : form.protocol === 'task_video'
                    ? 'wanx-i2v-turbo'
                    : 'deepseek-chat\ndeepseek-reasoner'}
                value={form.models_text} onChange={(e) => setForm({ ...form, models_text: e.target.value })} />
            </div>
          </div>
          <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
            <button className="btn btn-primary btn-sm" disabled={saving} onClick={() => void handleSaveForm()}>
              {saving ? '保存中…' : editingId ? '保存修改' : '添加'}
            </button>
            <button className="btn btn-secondary btn-sm" onClick={() => setFormOpen(false)}>取消</button>
          </div>
        </div>
      )}

      {/* 工位绑定 */}
      <div style={{ borderTop: '1px solid var(--color-border, rgba(255,255,255,0.1))', marginTop: 12, paddingTop: 12 }}>
        <div className="settings-row-name" style={{ marginBottom: 8 }}>工位绑定</div>
        {slotEntries.map(([slot, label]) => (
          <div key={slot} className="settings-row">
            <div className="settings-row-label">
              <span className="settings-row-name">{label}</span>
              <span className="settings-row-desc">
                {SLOT_HINTS[slot]
                  || '绑定后该功能出图走云端，本地显卡零占用；恢复本地=选回本地引擎'}
              </span>
            </div>
            <div className="settings-row-control">
              <select
                className="input settings-input"
                style={{ minWidth: 280 }}
                value={bindingValue(slot)}
                onChange={(e) => void handleBindChange(slot, e.target.value)}
              >
                <option value="">本地引擎（默认）</option>
                {bindOptions(slot).map((o) => (
                  <option key={o.value} value={o.value}>{o.label}</option>
                ))}
              </select>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
