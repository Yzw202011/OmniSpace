// 本项目仅供学习使用，商业授权请+Q 3559331368
/* ==========================================================================
 * HelpPage.tsx —— 帮助页（/help）
 * --------------------------------------------------------------------------
 * 静态中文使用指南：
 *   六大模块简介 / 快速上手 5 步 / 快捷键表 / 常见问题 6 条 / 关于卡片 v2.3.1
 * 纯静态内容，使用 Sakura 主题样式（card / badge / input 等）。
 * ========================================================================== */

import React from 'react';
import type { LucideIcon } from 'lucide-react';
import { MessageSquare, Palette, Clapperboard, BookOpen, Video, Package, CircleHelp, Compass, Rocket, Keyboard, Lightbulb, Flower2 } from 'lucide-react';

/** 六大模块简介 */
const MODULES: Array<{ icon: LucideIcon; name: string; desc: string }> = [
  { icon: MessageSquare, name: 'AI对话', desc: '与本地大模型多轮对话，支持知识库增强问答，回答引用已学习的知识。' },
  { icon: Palette, name: 'AI绘画', desc: '文生图 / 图生图，支持批量出图（1~4）与高清修复；ControlNet 与绘画 LoRA 叠加尚未开放，页面已如实标注。' },
  { icon: Clapperboard, name: '漫剧创作', desc: '分镜表驱动的漫剧流水线：剧本拆解、机位管理、截图资产与语音绑定。' },
  { icon: BookOpen, name: '知识学习', desc: 'AI 自主联网学习：主题管理、实时浏览查看、行为学习与知识库管理。' },
  { icon: Video, name: '视频风格', desc: '上传视频/图片素材训练专属风格 LoRA，支持版本管理、回滚与风格化预览帧。' },
  { icon: Package, name: '模型管理', desc: '本地模型的导入、加载、卸载与显存占用监控，协同调度一目了然（离线版本不提供在线下载）。' },
];

/** 快速上手 5 步 */
const QUICK_STEPS: string[] = [
  '在「模型管理」中加载随包附带的对话模型（如 Qwen3-VL-4B），等待状态变为就绪。',
  '打开「AI对话」开始提问；回答不满意时，可在「知识学习」添加相关主题让 AI 补充学习。',
  '在「知识学习」点击「+ 添加新主题」，或导入本地 PDF/DOCX/TXT 文档扩充知识库。',
  '在「AI绘画」输入提示词生成图片；训练专属风格可前往「视频风格」上传素材。',
  '在「漫剧创作」填写默认分镜表（当前版本为单一分镜表，不提供多项目管理），结合绘画产物完成漫剧流水线制作。',
];

/** 快捷键表（仅列已实现的；未实现的暂不展示，避免误导） */
const SHORTCUTS: Array<{ keys: string; desc: string }> = [
  { keys: 'Enter', desc: '对话页发送消息（Shift+Enter 换行）' },
  { keys: 'Esc', desc: '关闭弹窗 / 收起全局搜索' },
];

/** 常见问题 6 条 */
const FAQS: Array<{ q: string; a: string }> = [
  {
    q: '启动后提示「无法连接后端服务」怎么办？',
    a: '请确认后端服务已启动（默认 127.0.0.1:5800）。前端可离线打开，但 AI 功能需要后端在线。',
  },
  {
    q: '为什么对话 / 绘画按钮显示置灰？',
    a: 'OmniSpace 采用重量级功能互斥机制：训练、视频生成等任务运行时其他 AI 功能会暂时置灰，任务结束后自动恢复。',
  },
  {
    q: '知识学习会消耗多少资源？',
    a: '默认限制：空闲时 ≤5 标签页、CPU <20%、内存 <1.5GB、流量 <500KB/s，可在学习设置中调整流量与页数上限。',
  },
  {
    q: '如何让 AI 学习我自己的文档？',
    a: '在「知识学习」页的「导入文档学习」区域拖入 PDF / DOCX / TXT 文件，系统将自动提取知识点写入知识库。',
  },
  {
    q: '风格 LoRA 训练失败怎么办？',
    a: '常见原因：素材不足（建议 ≥20 张图或 10 秒视频）、显存不足（关闭其他 AI 功能后重试）、参数过激（将学习率调低至 1e-4）。',
  },
  {
    q: '数据会上传到云端吗？',
    a: '不会。除「联网学习」抓取的公开网页外，所有对话、知识库、模型与素材均保存在本机。',
  },
];

export const HelpPage: React.FC = () => {
  return (
    <div className="page help-page">
      <h1 className="page-title"><CircleHelp size={20} aria-hidden="true" /> 帮助</h1>
      <p className="page-subtitle">OmniSpace AI 使用指南与常见问题</p>

      <div className="flex flex-col gap-4 mt-5">
        {/* 六大模块简介 */}
        <section className="card hoverable" aria-label="功能模块">
          <h3 className="card-title"><Compass size={16} aria-hidden="true" /> 功能模块</h3>
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
            {MODULES.map((m) => (
              <div
                key={m.name}
                className="card hoverable"
                style={{ padding: 'var(--space-4)' }}
              >
                <div className="flex items-center gap-2.5" style={{ fontWeight: 'var(--font-weight-semibold)' }}>
                  <span
                    className="flex items-center justify-center rounded-lg"
                    style={{
                      width: 32,
                      height: 32,
                      background: 'var(--color-primary-50)',
                      color: 'var(--color-primary)',
                      flexShrink: 0,
                    }}
                  >
                    <m.icon size={16} aria-hidden="true" />
                  </span>
                  <span className="text-sm">{m.name}</span>
                </div>
                <div className="text-secondary mt-2.5" style={{ fontSize: 'var(--font-size-xs)', lineHeight: 'var(--line-height-relaxed)' }}>
                  {m.desc}
                </div>
              </div>
            ))}
          </div>
        </section>

        {/* 快速上手 */}
        <section className="card hoverable" aria-label="快速上手">
          <h3 className="card-title"><Rocket size={16} aria-hidden="true" /> 快速上手（5 步）</h3>
          <ol className="flex flex-col gap-3">
            {QUICK_STEPS.map((step, i) => (
              <li key={i} className="text-sm text-secondary">
                {step}
              </li>
            ))}
          </ol>
        </section>

        {/* 快捷键表 */}
        <section className="card hoverable" aria-label="快捷键">
          <h3 className="card-title"><Keyboard size={16} aria-hidden="true" /> 快捷键</h3>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <tbody>
              {SHORTCUTS.map((s) => (
                <tr key={s.keys} style={{ borderBottom: '1px solid var(--color-divider)' }}>
                  <td style={{ padding: 'var(--space-3) var(--space-2)', width: 180 }}>
                    <kbd
                      className="mono"
                      style={{
                        background: 'var(--color-primary-50)',
                        border: '1px solid var(--color-border-light)',
                        borderRadius: 'var(--radius-sm)',
                        padding: '2px 8px',
                        fontSize: 'var(--font-size-xs)',
                      }}
                    >
                      {s.keys}
                    </kbd>
                  </td>
                  <td className="text-secondary text-sm" style={{ padding: 'var(--space-3) var(--space-2)' }}>
                    {s.desc}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        {/* 常见问题 */}
        <section className="card hoverable" aria-label="常见问题">
          <h3 className="card-title"><Lightbulb size={16} aria-hidden="true" /> 常见问题</h3>
          <div className="flex flex-col gap-3">
            {FAQS.map((f, i) => (
              <details
                key={i}
                style={{
                  border: '1px solid var(--color-border-light)',
                  borderRadius: 'var(--radius-lg)',
                  padding: 'var(--space-3) var(--space-4)',
                }}
              >
                <summary className="text-sm" style={{ cursor: 'pointer', fontWeight: 'var(--font-weight-medium)' }}>
                  {f.q}
                </summary>
                <p className="text-secondary mt-2" style={{ fontSize: 'var(--font-size-sm)' }}>
                  {f.a}
                </p>
              </details>
            ))}
          </div>
        </section>

        {/* 关于卡片 */}
        <section className="card hoverable" aria-label="关于">
          <div className="flex items-center gap-4">
            <span
              aria-hidden="true"
              style={{
                width: 56,
                height: 56,
                borderRadius: 'var(--radius-lg)',
                background: 'var(--color-primary-50)',
                color: 'var(--color-primary)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                flexShrink: 0,
              }}
            >
              <Flower2 size={28} />
            </span>
            <div className="flex-1">
              <div style={{ fontWeight: 'var(--font-weight-semibold)' }}>OmniSpace AI</div>
              <div className="text-secondary text-sm">本地优先的一站式 AI 创作工作台</div>
            </div>
            <span className="badge info">v2.3.1</span>
          </div>
          <div className="text-tertiary mt-3" style={{ fontSize: 'var(--font-size-xs)' }}>
            Sakura 主题 · React 19 + Vite 6 · 全部数据本地存储
          </div>
        </section>
      </div>
    </div>
  );
};

export default HelpPage;
