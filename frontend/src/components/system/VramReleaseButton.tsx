// 本项目仅供学习使用，商业授权请+Q 3559331368
/* ==========================================================================
 * VramReleaseButton.tsx —— 一键释放显存按钮（2026-10-26 强制版，用户拍板）
 * --------------------------------------------------------------------------
 * 两处入口复用同一份逻辑（用户拍板「两处都放」）：
 *   - 设置页·维护签卡片（完整形态，带文案说明）
 *   - 底部状态栏显存区块旁（compact 形态，28px 通栏内联小按钮）
 * 语义：heavy 级二次确认 → POST /system/force-release-vram → toast 回报
 * 显存差值。强制版 = 无条件中断所有在飞任务 + 终止 ComfyUI/vLLM 子进程
 * + 卸载全部模型 + 强拆功能锁（后端 vram_release.py 强制版编排）。
 * ========================================================================== */

import { useState } from 'react';
import { confirmDialog } from '@/stores/useConfirmStore';
import { useAppStore } from '@/stores/useAppStore';
import { isApiError } from '@/services/api';
import { forceReleaseVram } from '@/services/systemApi';

/** 错误 → 人话（ApiError 语义码信封 / 普通 Error / 兜底） */
function errMsg(e: unknown): string {
  if (isApiError(e)) return e.message;
  if (e instanceof Error) return e.message;
  return '未知错误';
}

export default function VramReleaseButton({ compact = false }: { compact?: boolean }) {
  const [busy, setBusy] = useState(false);
  const showToast = useAppStore((s) => s.showToast);

  async function handleRelease() {
    const go = await confirmDialog({
      level: 'heavy',
      title: '一键释放显存？',
      badge: '在飞任务将中断',
      lines: [
        '将立即中断所有正在运行的生成任务（绘画/视频/对话/训练），未完成进度作废；排队任务全部取消。',
        '卸载全部模型并终止 ComfyUI / vLLM 子进程，显存全额释放。',
        '下次使用时自动重新加载（对话引擎冷启动可能需要 1~3 分钟）。',
      ],
      confirmText: '立即释放',
    });
    if (!go) return;
    setBusy(true);
    try {
      const r = await forceReleaseVram();
      if (r.completed) {
        showToast(
          `已释放 ${r.freed_gb.toFixed(1)} GB 显存`
          + `（${r.vram_before_gb.toFixed(1)}→${r.vram_after_gb.toFixed(1)} GB）`,
          'success');
      } else {
        const failed = r.actions.filter((a) => a.status === 'error').map((a) => a.step);
        showToast(
          `部分完成：释放 ${r.freed_gb.toFixed(1)} GB`
          + `（${r.vram_before_gb.toFixed(1)}→${r.vram_after_gb.toFixed(1)} GB），`
          + `异常步骤：${failed.join('、')}`,
          'warning');
      }
    } catch (e) {
      showToast(`一键释放显存失败：${errMsg(e)}`, 'error');
    } finally {
      setBusy(false);
    }
  }

  if (compact) {
    return (
      <button
        type="button"
        className="sb-item"
        disabled={busy}
        style={{
          color: busy ? 'var(--color-text-tertiary)' : 'var(--color-warning)',
          background: 'transparent', border: 'none', cursor: busy ? 'default' : 'pointer',
          padding: 0,
        }}
        title="一键释放显存：中断所有在飞任务并卸载全部模型，显存全额释放"
        onClick={handleRelease}
      >
        {busy ? '释放中…' : '一键释放显存'}
      </button>
    );
  }
  return (
    <button type="button" className="btn btn-danger" disabled={busy} onClick={handleRelease}>
      {busy ? '释放中…' : '一键释放显存'}
    </button>
  );
}
