// 本项目仅供学习使用，商业授权请+Q 3553191368
/* ==========================================================================
 * RecoveryGuideCard.tsx —— 恢复指南卡（批2 P33，2026-09-19）
 * --------------------------------------------------------------------------
 * 自愈失败（WS self_heal kind=failed）时弹出：人话四步恢复指南，
 * 能自动做的步骤一键执行（再试自动修复/重启绘画引擎），其余指路
 * （模型管理/一键体检/导诊断包）。绝不静默放弃——失败必给出路。
 * ========================================================================== */

import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { LifeBuoy, RefreshCw, X } from 'lucide-react';
import { post } from '@/services/api';
import { useAppStore } from '@/stores/useAppStore';
import { useSystemHealthStore, type SelfHealGuide } from '@/stores/useSystemHealthStore';
import { getErrorMessage } from '@/utils/errors';

/** 模块恢复建议文案（人话；按模块给最可能管用的一步） */
const RECOVER_HINTS: Record<string, string> = {
  paint: '绘画出图异常：多为 ComfyUI 进程崩溃或显存被占满。',
  dialog: '对话异常：多为 vLLM 引擎崩溃或显存不足。',
  video: '视频生成异常：H3 引擎无进程级重启，建议检查显存后重试。',
  training: '训练异常：多为素材/参数问题或显存不足。',
};

export default function RecoveryGuideCard() {
  const guide = useSystemHealthStore((s) => s.guide);
  const closeGuide = useSystemHealthStore((s) => s.closeGuide);
  const showToast = useAppStore((s) => s.showToast);
  const navigate = useNavigate();
  const [retryBusy, setRetryBusy] = useState(false);

  if (!guide) return null;
  const g = guide as SelfHealGuide;

  const retry = () => {
    setRetryBusy(true);
    post('/system/modules/health/recover', { module: g.module })
      .then(() => {
        showToast(`已重新发起「${g.label}」自动修复，进度见通知`, 'success');
        closeGuide();
      })
      .catch((err: unknown) =>
        showToast(getErrorMessage(err, '重试发起失败'), 'error'))
      .finally(() => setRetryBusy(false));
  };

  return (
    <div
      role="alert"
      aria-label="恢复指南"
      style={{
        position: 'fixed', right: 16, bottom: 92, zIndex: 1250,
        width: 'min(380px, calc(100vw - 32px))',
        background: 'var(--color-card)',
        border: '1px solid var(--color-error)',
        borderRadius: 'var(--radius-lg)',
        padding: 'var(--space-4)',
        boxShadow: '0 10px 32px rgba(0,0,0,0.4)',
      }}
    >
      <div className="flex items-center justify-between mb-2">
        <strong className="flex items-center gap-2" style={{ fontSize: 'var(--font-size-base)', color: 'var(--color-error)' }}>
          <LifeBuoy size={16} aria-hidden="true" />
          「{g.label}」自动修复未成功
        </strong>
        <button type="button" className="btn btn-ghost btn-sm" aria-label="关闭恢复指南" onClick={closeGuide}>
          <X size={14} aria-hidden="true" />
        </button>
      </div>
      <p className="text-secondary text-sm" style={{ marginBottom: 'var(--space-3)' }}>
        {RECOVER_HINTS[g.module] ?? '模块连续异常。'}按下面四步排查，一般前两步就能解决：
      </p>
      <ol className="text-sm text-secondary" style={{ paddingLeft: 'var(--space-5)', display: 'grid', gap: 8 }}>
        <li>
          <b>再试一次自动修复</b>——
          <button type="button" className="btn btn-ghost btn-sm" disabled={retryBusy} onClick={retry}>
            <RefreshCw size={13} className={retryBusy ? 'spin' : ''} aria-hidden="true" />
            {retryBusy ? '发起中…' : '一键重试'}
          </button>
        </li>
        <li><b>查显存占用</b>——到「模型管理」看显存条是否打满，满则卸载不用的模型
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => { closeGuide(); navigate('/models'); }}>前往</button>
        </li>
        <li><b>一键体检</b>——设置→维护→开始体检，可修项一键修复
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => { closeGuide(); navigate('/settings'); }}>前往</button>
        </li>
        <li><b>仍不行</b>——日志页「导出诊断包」发给维护者
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => { closeGuide(); navigate('/logs'); }}>前往</button>
        </li>
      </ol>
      <div className="text-tertiary mt-3" style={{ fontSize: 'var(--font-size-xs)' }}>
        最近错误：{g.message}
      </div>
    </div>
  );
}
