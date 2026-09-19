// 本项目仅供学习使用，商业授权请+Q 3553191368
/* ==========================================================================
 * CloudSlotBadge.tsx —— 页内云端工位徽标（批3 P5，2026-09-19）
 * --------------------------------------------------------------------------
 * 治「云端入口找不到」：创作页内直接显示对应工位（槽位）的云端绑定
 * 状态——已绑定亮云并显示模型名（零显存占用）；未绑定点击直达
 * 设置页 AI 服务签（深链 ?tab=ai），配完返回即亮（60s 内自动刷新，
 * 或手动刷新页面）。读取失败如实降级为「状态未知」。
 * ========================================================================== */

import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Cloud } from 'lucide-react';
import { listCloudBindings } from '@/services/cloudApi';
import { reportBgError } from '@/utils/errors';

export interface CloudSlotDesc {
  /** 工位键（如 novel.text / paint.image） */
  key: string;
  /** 人话名（如「写作文本」） */
  label: string;
}

interface Props {
  slots: CloudSlotDesc[];
}

export default function CloudSlotBadge({ slots }: Props) {
  const navigate = useNavigate();
  const [bound, setBound] = useState<Record<string, { model: string; provider?: string }> | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = () => {
      listCloudBindings()
        .then((d) => {
          if (cancelled) return;
          const map: Record<string, { model: string; provider?: string }> = {};
          for (const s of slots) {
            const b = d?.bindings?.[s.key];
            if (b?.model) map[s.key] = { model: b.model, provider: b.provider_name };
          }
          setBound(map);
        })
        .catch((err: unknown) => {
          reportBgError('CloudSlotBadge', err);
          if (!cancelled) setBound({});
        });
    };
    load();
    const timer = window.setInterval(load, 60_000);
    return () => { cancelled = true; window.clearInterval(timer); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slots.map((s) => s.key).join(',')]);

  const goSettings = () => navigate('/settings?tab=ai');

  if (bound === null) {
    return (
      <button type="button" className="btn btn-ghost btn-sm" onClick={goSettings} title="云端 API 工位状态读取中…">
        <Cloud size={13} aria-hidden="true" /> 云端…
      </button>
    );
  }
  const boundCount = slots.filter((s) => bound[s.key]).length;
  if (boundCount === 0) {
    return (
      <button
        type="button"
        className="btn btn-ghost btn-sm"
        onClick={goSettings}
        title={`${slots.map((s) => s.label).join('/')} 未绑云端——点击去设置页「AI 服务」配置（配完回来即生效）`}
      >
        <Cloud size={13} aria-hidden="true" /> 云端未配置（点击配置）
      </button>
    );
  }
  // 有绑定：亮云显示模型（多槽取第一个 + N）
  const first = bound[slots.find((s) => bound[s.key])!.key];
  const extra = boundCount > 1 ? ` +${boundCount - 1}` : '';
  return (
    <button
      type="button"
      className="btn btn-ghost btn-sm"
      style={{ color: 'var(--color-accent)' }}
      onClick={goSettings}
      title={`当前走云端（零显存占用）：${slots.filter((s) => bound[s.key]).map((s) => `${s.label}=${bound[s.key].model}`).join('；')}——点击去设置页调整`}
    >
      <Cloud size={13} aria-hidden="true" />
      云端·{first.model}{extra}
    </button>
  );
}
