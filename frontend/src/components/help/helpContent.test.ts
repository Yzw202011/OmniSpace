// 本项目仅供学习使用，商业授权请+Q 3559331368
/* ==========================================================================
 * helpContent.test.ts —— 帮助页静态文案锚词一致性（2026-10-19 ROUND-6 观察：
 * 帮助页静态文案域=文档漂移重灾区，L-01/L-02 连抓两处；本测试把 MODULES/
 * QUICK_STEPS 的关键锚词钉死，页面改名/功能换血时先红测试再改文案）
 * ========================================================================== */
import { describe, expect, it } from 'vitest';
import { MODULES, QUICK_STEPS } from './HelpPage';

/** 现行 11 路由的导航名（router.tsx 侧栏同源口径） */
const CURRENT_NAV_NAMES = [
  'AI对话', '写作台', 'AI漫画', '漫剧创作', '知识学习',
  '模型管理', '训练中心', 'MagicAI 中转', '设置', '系统日志', '帮助',
];

/** 已清退/已更名的死引用（再现即红） */
const DEAD_REFS = ['AI绘画', '视频风格', '绘画页', '3D 导演台'];

describe('帮助页锚词一致性（防文档漂移）', () => {
  it('功能模块简介覆盖全部创作/工场导航名', () => {
    const names = MODULES.map((m) => m.name);
    // 创作五页 + 工场两页必须全在简介里
    for (const nav of ['AI对话', '写作台', 'AI漫画', '漫剧创作', '知识学习', '模型管理', '训练中心']) {
      expect(names, `功能模块简介缺现行模块「${nav}」`).toContain(nav);
    }
    // 每条简介非空且长度合理
    for (const m of MODULES) {
      expect(m.desc.length, `${m.name} 简介为空`).toBeGreaterThan(8);
    }
  });

  it('模块名不与导航死区冲突（名字必须真实存在于侧栏）', () => {
    for (const m of MODULES) {
      expect(
        CURRENT_NAV_NAMES.some((n) => n === m.name || n.includes(m.name) || m.name.includes(n)),
        `模块简介名「${m.name}」在任何现行导航中不存在`,
      ).toBe(true);
    }
  });

  it('快速上手/模块简介零死引用（AI绘画/视频风格等清退名不再出现）', () => {
    const all = [...MODULES.map((m) => m.name + m.desc), ...QUICK_STEPS].join('\n');
    for (const dead of DEAD_REFS) {
      expect(all.includes(dead), `帮助页出现清退名「${dead}」`).toBe(false);
    }
  });

  it('快速上手五步锚定现行入口（模型管理/AI对话/知识学习/AI漫画/漫剧创作）', () => {
    expect(QUICK_STEPS).toHaveLength(5);
    const all = QUICK_STEPS.join('\n');
    for (const anchor of ['模型管理', 'AI对话', '知识学习', 'AI漫画', '漫剧创作']) {
      expect(all.includes(anchor), `快速上手缺现行入口锚词「${anchor}」`).toBe(true);
    }
    // 多项目管理已是现行能力，旧口径「不提供多项目管理」不得回潮
    expect(all.includes('不提供多项目管理'), '旧口径「不提供多项目管理」回潮').toBe(false);
  });
});
