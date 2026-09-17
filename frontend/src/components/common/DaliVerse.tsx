// 本项目仅供学习使用，商业授权请+Q 3559331368
/**
 * DaliVerse 风花雪月诗句水印（Dali 主题专属画眼层）
 * --------------------------------------------------------------------------
 * D4 拍板（2026-09-11，docs/治愈系主题方案-2026-09-11.md §1.5）+ B 强化
 * （2026-09-12 用户拍板）：平时是「若隐若现」的背景水印；页面处于空状态时
 * 诗句「浮现」（透明度升起、印章实色）——等你的时刻，句子的时刻。
 *   /chat        我在风花雪月里等你（书眼「等」：AI 伙伴等你回来）
 *   /paint       上关花（画布开花）
 *   /storyboard  洱海月（银幕如湖面）
 *   /learning    下关风（知识如风）
 *   /models      苍山雪（雪冠苍山，沉稳底座）
 * 艺术语言（样式见 dali-fx.css §3）：楷体竖排 + 墨韵渐隐 + 印章 + 呼吸。
 * 行为：仅 data-family=dali 渲染；pointer-events:none + z-index 0 永不挡活；
 * ui-lite 性能模式下呼吸动画被全局闸停（sakura.css），文字保留（静态零负担）。
 * 空状态探测（B）：通用 .empty-state 可见元素，或对话页空态标记文本
 * 「开始一段新对话吧」（DialogView.tsx 无类名空态，文本探测兜底）；
 * MutationObserver + rAF 节流，探测失败静默降级为普通水印。
 */
import { useEffect, useRef } from 'react';
import { useLocation } from 'react-router-dom';
import { useAppStore } from '@/stores/useAppStore';

/** 路由 → 诗句映射（prefix 精确匹配一级路由） */
interface VerseItem {
  prefix: string;
  text: string;
  seal: string;
}
const VERSES: ReadonlyArray<VerseItem> = [
  { prefix: '/chat', text: '我在风花雪月里等你', seal: '等' },
  { prefix: '/paint', text: '上关花', seal: '花' },
  { prefix: '/storyboard', text: '洱海月', seal: '月' },
  { prefix: '/learning', text: '下关风', seal: '风' },
  { prefix: '/models', text: '苍山雪', seal: '雪' },
];

/** 对话页空态标记文本（DialogView.tsx 空会话占位，无类名故按文本探测） */
const CHAT_EMPTY_MARK = '开始一段新对话吧';

export function DaliVerse() {
  const theme = useAppStore((s) => s.theme);
  const location = useLocation();
  const hostRef = useRef<HTMLDivElement | null>(null);
  const hit = VERSES.find((v) => v.prefix === location.pathname);

  const isDali = theme.startsWith('dali');

  /* 空状态探测（B 拍板）：可见 .empty-state 或对话页空态文本 → 浮现诗句 */
  useEffect(() => {
    if (!isDali || !hit) return;
    let raf = 0;
    const check = (): void => {
      if (raf) return;
      raf = requestAnimationFrame(() => {
        raf = 0;
        let revealed = false;
        const es = document.querySelector('.empty-state');
        if (es && (es as HTMLElement).offsetParent !== null) revealed = true;
        if (!revealed && location.pathname === '/chat') {
          try {
            revealed = (document.body.innerText || '').includes(CHAT_EMPTY_MARK);
          } catch {
            /* silent-intent: innerText 不可用（极老内核）静默跳过 */
          }
        }
        hostRef.current?.classList.toggle('dali-verse--revealed', revealed);
      });
    };
    const mo = new MutationObserver(check);
    mo.observe(document.body, { childList: true, subtree: true });
    window.addEventListener('resize', check);
    check();
    return () => {
      mo.disconnect();
      window.removeEventListener('resize', check);
      if (raf) cancelAnimationFrame(raf);
    };
  }, [isDali, hit, location.pathname]);

  if (!isDali) return null;
  if (!hit) return null;
  return (
    <div className="dali-verse" aria-hidden="true" ref={hostRef}>
      <span className="dali-verse-text">{hit.text}</span>
      <span className="dali-verse-seal">{hit.seal}</span>
    </div>
  );
}

export default DaliVerse;
