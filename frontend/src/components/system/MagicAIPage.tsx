// 本项目仅供学习使用，商业授权请+Q 3559331368
/* ==========================================================================
 * OmniSpace AI —— MagicAI 中转介绍页（2026-10-03 用户拍板：系统组专属模块）
 * --------------------------------------------------------------------------
 * 大白话：给 magicai.click（订阅转 API 平台「Happy」）做站内门面——
 * 上半动态：按 base_url 域名识别本机已配置的 MagicAI 连接，逐连接实时
 *   拉在线状态（/models 计时）、模型池、余额（billing 尽力而为）、
 *   已绑定工位；下半静态：卖点 / 支持模型 / 四步接入 / 诚实现状 / 隐私。
 * 动态数据全部复用现有云端连接端点（billing 为 2026-10-03 新增），
 * Key 全程留在后端不出本机。
 * ========================================================================== */

import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { RefreshCw } from 'lucide-react';
import {
  fetchCloudProviderBilling,
  fetchCloudProviderModels,
  listCloudBindings,
  listCloudProviders,
  type CloudBilling,
  type CloudProvider,
} from '@/services/cloudApi';

const SITE_HOME = 'https://magicai.click/';
const SITE_KEYS = 'https://magicai.click/keys';
const MAGICAI_HOST_RE = /magicai\.click/i;

/** 单连接的实时状态（models 拉取成功 = 在线，耗时即延迟） */
interface LiveStatus {
  checking: boolean;
  latencyMs: number | null;
  models: string[];
  modelsError: string;
  billing: CloudBilling | null;
}

const EMPTY_STATUS: LiveStatus = {
  checking: false, latencyMs: null, models: [], modelsError: '', billing: null,
};

interface SlotBinding {
  provider_id: string;
  model: string;
  provider_name?: string;
}

export default function MagicAIPage() {
  const [providers, setProviders] = useState<CloudProvider[] | null>(null);
  const [bindings, setBindings] = useState<Record<string, SlotBinding>>({});
  const [slotLabels, setSlotLabels] = useState<Record<string, string>>({});
  const [status, setStatus] = useState<Record<string, LiveStatus>>({});

  const magicai = (providers ?? []).filter((p) => MAGICAI_HOST_RE.test(p.base_url));

  const refreshOne = useCallback(async (id: string) => {
    setStatus((s) => ({
      ...s, [id]: { ...(s[id] ?? EMPTY_STATUS), checking: true },
    }));
    const t0 = performance.now();
    let latencyMs: number | null = null;
    let models: string[] = [];
    let modelsError = '';
    try {
      const r = await fetchCloudProviderModels(id);
      models = r.models ?? [];
      modelsError = r.error ?? '';
      latencyMs = Math.round(performance.now() - t0);
    } catch (err) {
      modelsError = (err as { message?: string })?.message || '连接失败';
    }
    let billing: CloudBilling | null = null;
    try {
      billing = await fetchCloudProviderBilling(id);
    } catch (err) {
      // 余额探针失败不算整体失败：如实显示原因（老后端无此端点/网络失败）
      billing = {
        supported: false,
        error: `余额查询不可用（${(err as { message?: string })?.message || '网络失败'}）`,
      };
    }
    setStatus((s) => ({
      ...s,
      [id]: { checking: false, latencyMs, models, modelsError, billing },
    }));
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [provResp, bindResp] = await Promise.all([
          listCloudProviders(), listCloudBindings(),
        ]);
        if (cancelled) return;
        setProviders(provResp.providers);
        setSlotLabels(bindResp.slots ?? {});
        setBindings(bindResp.bindings ?? {});
        const mine = provResp.providers.filter((p) => MAGICAI_HOST_RE.test(p.base_url));
        for (const p of mine) {
          if (!cancelled) void refreshOne(p.id);
        }
      } catch {
        if (!cancelled) setProviders([]);
      }
    })();
    return () => { cancelled = true; };
  }, [refreshOne]);

  /** provider_id → 已绑定的工位列表 */
  const bindingsOf = (pid: string): Array<{ slot: string; model: string }> => (
    Object.entries(bindings)
      .filter(([, b]) => b.provider_id === pid)
      .map(([slot, b]) => ({ slot, model: b.model }))
  );

  return (
    <div className="magicai-page">
      {/* ── 头部门面 ─────────────────────────────────────────── */}
      <header className="mai-hero">
        <div className="mai-hero-txt">
          <h2>MagicAI 中转</h2>
          <p className="mai-sub">
            订阅转 API 平台「Happy」 · magicai.click —— 订阅账号托管，
            对外发标准 API：一把钥匙调所有已接入模型
          </p>
        </div>
        <div className="mai-hero-actions">
          <a className="mai-btn" href={SITE_KEYS} target="_blank" rel="noopener noreferrer">
            打开控制台（领 Key）↗
          </a>
          <a className="mai-btn mai-btn-ghost" href={SITE_HOME} target="_blank" rel="noopener noreferrer">
            打开官网 ↗
          </a>
        </div>
      </header>

      {/* ── 卖点与支持模型 ───────────────────────────────────── */}
      <section className="mai-pills">
        <span className="mai-pill">🔑 一把钥匙通吃模型</span>
        <span className="mai-pill">🛡 多上游智能路由 · 自动容灾</span>
        <span className="mai-pill">💳 按量计费 · 用多少付多少</span>
        <span className="mai-pill mai-pill-dim">
          已接入：Claude / GPT / Gemini / Antigravity（以站点实时目录为准）
        </span>
      </section>

      {/* ── 动态：实时状态 ───────────────────────────────────── */}
      <section className="mai-section">
        <h3>实时状态（本机已配置的 MagicAI 连接）</h3>
        {providers === null && <p className="mai-dim">读取连接配置中…</p>}
        {providers !== null && magicai.length === 0 && (
          <div className="mai-card">
            <p>本机还没有配置 MagicAI 连接。</p>
            <p className="mai-dim">
              先去 <Link to="/settings">设置 → 云端 API 服务</Link> 添加连接
              （地址填 <code>https://magicai.click</code>，协议选「文本（OpenAI 兼容）」，
              Key 从上方「打开控制台」领取），配好回来本页自动出现实时状态。
            </p>
          </div>
        )}
        {magicai.map((p) => {
          const st = status[p.id] ?? EMPTY_STATUS;
          const bound = bindingsOf(p.id);
          const bill = st.billing;
          return (
            <div className="mai-card" key={p.id}>
              <div className="mai-card-head">
                <b>{p.name}</b>
                <span className={`mai-badge${p.enabled ? ' ok' : ''}`}>
                  {p.enabled ? '已启用' : '已停用'}
                </span>
                <span className="mai-dim mai-host">{p.base_url}</span>
                <button
                  className="mai-refresh"
                  onClick={() => void refreshOne(p.id)}
                  disabled={st.checking}
                  title="重新拉取在线状态 / 模型池 / 余额"
                ><RefreshCw size={14} />{st.checking ? ' 检测中…' : ' 刷新'}</button>
              </div>
              <div className="mai-kv">
                <span>在线状态</span>
                <b className={
                  st.checking ? '' : (st.latencyMs != null && !st.modelsError ? 'mai-ok' : 'mai-err')
                }>
                  {st.checking
                    ? '检测中…'
                    : (st.latencyMs != null && !st.modelsError
                      ? `✅ 在线 · ${st.latencyMs} ms`
                      : `❌ ${st.modelsError || '不可达'}`)}
                </b>
              </div>
              <div className="mai-kv">
                <span>余额</span>
                <b>
                  {bill?.supported
                    ? `总额 $${bill.total_usd} · 已用 $${bill.used_usd} · 剩余 $${bill.remain_usd}`
                    : (bill?.error || '未检测（点刷新后显示）')}
                </b>
              </div>
              <div className="mai-kv">
                <span>已绑定工位</span>
                <b>
                  {bound.length === 0
                    ? '未绑定（去设置·云端 API 绑定后生效）'
                    : bound.map((b) => (
                      `${slotLabels[b.slot] ?? b.slot}${b.model ? ` → ${b.model}` : ''}`
                    )).join('；')}
                </b>
              </div>
              <div className="mai-models">
                <div className="mai-models-head">
                  模型池（{st.models.length} 个，拉取自站点 /v1/models 实时目录——
                  模型池会漂移，以本页为准）
                </div>
                {st.models.length > 0 && (
                  <div className="mai-chips">
                    {st.models.map((m) => <code key={m} className="mai-chip">{m}</code>)}
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </section>

      {/* ── 静态：接入指引 ───────────────────────────────────── */}
      <section className="mai-section">
        <h3>四步接入</h3>
        <div className="mai-card">
          <ol className="mai-steps">
            <li>
              <b>注册登录</b>：点上方「打开控制台」注册并登录 magicai.click。
            </li>
            <li>
              <b>领取 Key</b>：在控制台 Keys 页创建并复制 API Key（形如 sk-…）。
            </li>
            <li>
              <b>添加连接</b>：<Link to="/settings">设置 → 云端 API 服务 → 添加连接</Link>：
              地址 <code>https://magicai.click</code>，协议「文本（OpenAI 兼容）」，粘贴 Key，
              模型列表可点「拉取」自动带出。
            </li>
            <li>
              <b>工位绑定</b>：在连接卡片上把「AI 对话 / 写作台 / 漫剧文字」等工位
              绑到 MagicAI 的模型，即绑即用。
            </li>
          </ol>
        </div>
      </section>

      <section className="mai-section">
        <h3>接入现状（诚实口径）</h3>
        <div className="mai-card">
          <ul className="mai-facts">
            <li className="mai-ok">✅ 文本链路已实测打通（GPT-6 系，2026-10-03 实弹验证）。</li>
            <li className="mai-warn">⚠️ 模型池会漂移：站点侧模型目录随时增减，绑定前以本页
              「实时状态 → 模型池」为准。</li>
            <li>🧭 图片 / 视频等其他能力不写死承诺：以「实时状态」各连接的
              模型池与已绑定工位为准（站点上新即自动呈现）。</li>
          </ul>
        </div>
      </section>

      <section className="mai-section">
        <h3>隐私口径</h3>
        <div className="mai-card">
          <p className="mai-dim">
            API Key 仅保存在本机，请求由你的电脑直达 magicai.click，
            软件不经手、不中转、不抽成；本页动态数据由本机后端持 Key 代查，
            Key 不出现在前端。
          </p>
        </div>
      </section>
    </div>
  );
}
