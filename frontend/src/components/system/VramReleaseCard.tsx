// 本项目仅供学习使用，商业授权请+Q 3559331368
/* ==========================================================================
 * VramReleaseCard.tsx —— 设置页·维护签「一键释放显存」卡片
 * --------------------------------------------------------------------------
 * 2026-10-26 强制版（用户拍板）：显存紧张/任务卡死时的应急兜底——
 * 无条件中断所有在飞任务、终止 ComfyUI/vLLM 子进程、卸载全部模型、
 * 强拆功能锁。动作链语义见按钮组件与后端 src/services/vram_release.py；
 * 同款入口也在底部状态栏显存读数旁。
 * ========================================================================== */

import VramReleaseButton from '@/components/system/VramReleaseButton';

export default function VramReleaseCard() {
  return (
    <div className="settings-section card">
      <div className="settings-section-header">
        <h2 className="settings-section-title">一键释放显存</h2>
      </div>
      <p className="settings-section-desc">
        显存被占满、新任务排队失败或任务卡死时的应急兜底：立即中断所有在飞
        生成任务（绘画/视频/对话/训练，进度作废）、取消全部排队任务、终止
        ComfyUI 与 vLLM 子进程、卸载全部模型并清空推理缓存，显存全额释放。
        被卸载的组件下次使用时自动重新加载（对话引擎冷启动可能需要 1~3 分钟）。
      </p>
      <div style={{ marginTop: 'var(--space-3)' }}>
        <VramReleaseButton />
      </div>
    </div>
  );
}
