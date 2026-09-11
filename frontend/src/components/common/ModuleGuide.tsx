// 本项目仅供学习使用，商业授权请+Q 3559331368
/* ==========================================================================
 * ModuleGuide.tsx —— 模块新手引导（UAT 2026-09-10 缺口：三模块引导系统）
 * --------------------------------------------------------------------------
 * 首次进入模块时自动弹出引导卡（每浏览器一次，localStorage 记忆）；
 * 右下角悬浮「?」按钮随时可再次打开；可隐藏，应用重启后自动恢复显示
 * （隐藏态为会话内存态，不落盘——对齐清单「重启自动恢复显示」）。
 * ========================================================================== */

import { useEffect, useState } from 'react';
import { CircleHelp, X } from 'lucide-react';
export interface ModuleGuideContent {
  /** 模块标识（同时用作 localStorage 键后缀） */
  key: string;
  title: string;
  /** 引导步骤（2~5 条，大白话） */
  items: string[];
}

/** 各模块引导内容（新增模块在此登记即可接入） */
const GUIDE_CONTENT: Record<string, ModuleGuideContent> = {
  chat: {
    key: 'chat',
    title: 'AI 对话 · 新手引导',
    items: [
      '左侧选择会话或点「新建对话」；直接在输入框打字会自动创建对话',
      '右上「对话设置」可切换模型（首次切换约需 0.5-2 分钟加载）',
      '支持上传图片（图片理解）与文档（txt/md/docx/pdf）',
      '「深度思考」开启后 AI 先展示推理过程再给答案；生成中可点停止',
    ],
  },
  comic: {
    key: 'comic',
    title: 'AI 漫画 · 新手引导',
    items: [
      '先建角色（AI 生成四视图，约 1-3 分钟），角色是跨格一致的锚',
      '「AI 写分格」让 AI 按剧情生成分格描述，也可手动添加分格',
      '每格绑定角色后点「生成画面」，台词会渲染成气泡（可拖拽/拉伸）',
      '完成后用「预览」查看整页效果，「导出成册」支持 PNG/PDF',
    ],
  },
  novel: {
    key: 'novel',
    title: '写作台 · 新手引导',
    items: [
      '新建作品填书名/题材/一句话灵感，点「AI 生成大纲」出卷与章细纲',
      '大纲树每个节点都可改/删，满意后「生成未完成章节」批量写正文',
      '「角色/伏笔/世界观」面板让 AI 设计设定，写作时自动保持一致',
      '完成后 TXT/MD 一键导出整本书',
    ],
  },
};

export default function ModuleGuide({ moduleKey }: { moduleKey: string }) {
  const content = GUIDE_CONTENT[moduleKey];
  const [open, setOpen] = useState(false);
  /** 悬浮按钮隐藏态：仅内存（重启自动恢复显示，对齐清单要求） */
  const [hidden, setHidden] = useState(false);

  useEffect(() => {
    const key = `guide_auto_shown_${moduleKey}`;
    try {
      if (!window.localStorage.getItem(key)) {
        window.localStorage.setItem(key, '1');
        setOpen(true);
      }
    } catch { /* localStorage 不可用时不弹引导 */ }
  }, [moduleKey]);

  if (hidden) return null;

  return (
    <>
      <button
        type="button"
        aria-label={`${content?.title ?? '引导'}（点击查看/隐藏）`}
        title={content?.title}
        onClick={() => setOpen((v) => !v)}
        style={{
          position: 'fixed', right: 16, bottom: 44, zIndex: 1199,
          width: 40, height: 40, borderRadius: '50%',
          border: '1px solid var(--color-border-light)',
          background: 'var(--color-card)',
          color: 'var(--color-text-secondary)',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          cursor: 'pointer', boxShadow: '0 2px 8px rgba(0,0,0,0.25)',
        }}
      >
        <CircleHelp size={20} aria-hidden="true" />
      </button>
      {open && content && (
        <div
          role="dialog"
          aria-label={content.title}
          style={{
            position: 'fixed', right: 16, bottom: 92, zIndex: 1200,
            width: 'min(360px, calc(100vw - 32px))',
            background: 'var(--color-card)',
            border: '1px solid var(--color-border-light)',
            borderRadius: 'var(--radius-lg)',
            padding: 'var(--space-4)',
            boxShadow: '0 8px 24px rgba(0,0,0,0.35)',
          }}
        >
          <div className="flex items-center justify-between mb-2">
            <strong style={{ fontSize: 'var(--font-size-base)' }}>{content.title}</strong>
            <button
              type="button"
              aria-label="关闭引导"
              className="btn btn-ghost btn-sm"
              onClick={() => setOpen(false)}
            >
              <X size={14} aria-hidden="true" />
            </button>
          </div>
          <ul className="text-sm text-secondary" style={{ paddingLeft: 'var(--space-5)', display: 'grid', gap: 6 }}>
            {content.items.map((it) => <li key={it.slice(0, 12)}>{it}</li>)}
          </ul>
          <div className="flex items-center justify-between mt-3">
            <button
              className="btn btn-ghost btn-sm"
              onClick={() => setHidden(true)}
              title="本会话内不再显示，应用重启后自动恢复"
            >
              本会话不再显示
            </button>
            <button className="btn btn-primary btn-sm" onClick={() => setOpen(false)}>知道了</button>
          </div>
        </div>
      )}
    </>
  );
}
