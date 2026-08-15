/* ==========================================================================
 * PromptModal.tsx —— 漫剧编辑器·AI 生词提示词设置弹窗（竞品对齐）
 * --------------------------------------------------------------------------
 * 配置项（localStorage omnispace.mangaPromptCfg，batchOps.readPromptCfg 读取）：
 *   - 默认提示词：后端内置分镜描述词提示词（视觉风格/镜头语言/情绪氛围）
 *   - 自定义提示词：用户前缀（≤500 字），逐行/批量 AI 生词时经 prompt_prefix
 *     透传 POST /storyboard/ai-describe
 * 入口：工序条右侧「提示词」工具按钮 / 批量生词确认弹窗「提示词设置」。
 * ========================================================================== */

import { useState } from 'react';
import { ScrollText } from 'lucide-react';
import { useAppStore } from '@/stores/useAppStore';
import { Modal } from '../../common/Modal';
import { PROMPT_PREFIX_MAX, readPromptCfg, writePromptCfg } from './batchOps';

/** 默认提示词说明（后端内置，不在前端复制提示词全文） */
const DEFAULT_HINT = '使用后端内置提示词：按漫剧分镜规范生成画面描述（角色动作/场景氛围/镜头语言）。';

export default function PromptModal({ onClose }: { onClose: () => void }) {
  const showToast = useAppStore((s) => s.showToast);
  const [cfg, setCfg] = useState(readPromptCfg);

  const customLen = cfg.custom.trim().length;
  const customInvalid = cfg.mode === 'custom' && customLen === 0;

  /** 保存并关闭（custom 模式空前缀禁止保存，避免静默回退造成误解） */
  const handleSave = () => {
    if (customInvalid) {
      showToast('自定义提示词不能为空，或切换回默认提示词', 'warning');
      return;
    }
    writePromptCfg({ mode: cfg.mode, custom: cfg.custom });
    showToast('提示词设置已保存，对后续 AI 生词生效', 'success');
    onClose();
  };

  return (
    <Modal
      title={
        <span className="flex items-center gap-2">
          <ScrollText size={16} style={{ color: 'var(--color-primary)' }} />
          AI 生词提示词设置
        </span>
      }
      onClose={onClose}
      width={520}
      footer={
        <>
          <button type="button" className="btn btn-ghost" onClick={onClose}>
            取消
          </button>
          <button type="button" className="btn btn-primary" disabled={customInvalid} onClick={handleSave}>
            保存
          </button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        {/* 模式切换 */}
        <div className="seg" role="tablist" aria-label="提示词模式">
          <button
            type="button"
            role="tab"
            aria-selected={cfg.mode === 'default'}
            className={`seg-item${cfg.mode === 'default' ? ' active' : ''}`}
            style={{ flex: 1 }}
            onClick={() => setCfg((c) => ({ ...c, mode: 'default' }))}
          >
            默认提示词
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={cfg.mode === 'custom'}
            className={`seg-item${cfg.mode === 'custom' ? ' active' : ''}`}
            style={{ flex: 1 }}
            onClick={() => setCfg((c) => ({ ...c, mode: 'custom' }))}
          >
            自定义提示词
          </button>
        </div>

        {cfg.mode === 'default' ? (
          <p className="manga-form-tip" style={{ margin: 0 }}>
            {DEFAULT_HINT}
          </p>
        ) : (
          <div>
            <textarea
              className="input"
              rows={6}
              value={cfg.custom}
              maxLength={PROMPT_PREFIX_MAX}
              style={{ resize: 'vertical' }}
              placeholder="如：日系赛璐璐风格，樱花粉与薄荷绿主色调，柔和光晕，强调角色表情特写…"
              aria-label="自定义提示词前缀"
              onChange={(e) => setCfg((c) => ({ ...c, custom: e.target.value }))}
            />
            <div className="manga-form-tip flex items-center" style={{ marginTop: 'var(--space-2)' }}>
              <span style={{ flex: 1 }}>
                作为前缀拼接到每行 AI 生词请求，对单行 ⟳ 与批量生词同时生效
              </span>
              <span className={customLen > PROMPT_PREFIX_MAX - 50 ? 'text-warning' : ''}>
                {customLen}/{PROMPT_PREFIX_MAX}
              </span>
            </div>
          </div>
        )}
      </div>
    </Modal>
  );
}
