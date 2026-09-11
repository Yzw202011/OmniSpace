// 本项目仅供学习使用，商业授权请+Q 3559331368
/**
 * DaliVerse 风花雪月诗句水印（Dali 主题专属画眼层）
 * --------------------------------------------------------------------------
 * D4 拍板（2026-09-11，docs/治愈系主题方案-2026-09-11.md §1.5）：宣传语拆句
 * 植入五功能页，一句一景 ——
 *   /chat        我在风花雪月里等你（书眼「等」：AI 伙伴等你回来）
 *   /paint       上关花（画布开花）
 *   /storyboard  洱海月（银幕如湖面）
 *   /learning    下关风（知识如风）
 *   /models      苍山雪（雪冠苍山，沉稳底座）
 * 艺术语言（样式见 dali-fx.css §3）：楷体竖排 + 墨韵渐隐 + 印章 + 若隐若现。
 * 行为：仅 data-family=dali 渲染；pointer-events:none + z-index 0 永不挡活；
 * ui-lite 性能模式下呼吸动画被全局闸停（sakura.css），文字保留（静态零负担）。
 */
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

export function DaliVerse() {
  const theme = useAppStore((s) => s.theme);
  const location = useLocation();
  if (!theme.startsWith('dali')) return null;
  const hit = VERSES.find((v) => v.prefix === location.pathname);
  if (!hit) return null;
  return (
    <div className="dali-verse" aria-hidden="true">
      <span className="dali-verse-text">{hit.text}</span>
      <span className="dali-verse-seal">{hit.seal}</span>
    </div>
  );
}

export default DaliVerse;
