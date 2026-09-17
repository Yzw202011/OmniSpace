/**
 * 分组导航单测（P0 重组 2026-09-17，用户拍板 1A）。
 *
 * 覆盖：NAV_ITEMS 分组完备性（每项有组合法组键、组员非空）、
 * regroupNavItems 的组序优先/组内相对序保留/跨组穿插旧序兼容解读。
 * 侧栏 DOM 级走查由真浏览器验收承担（方案 §③-2）。
 */
import { describe, expect, it } from 'vitest';

import {
  NAV_GROUPS,
  NAV_ITEMS,
  arrangedNavItems,
  regroupNavItems,
  type NavItemMeta,
} from './nav';

describe('NAV_ITEMS 分组完备性', () => {
  it('每个导航项都携带合法组键', () => {
    const keys = new Set(NAV_GROUPS.map((g) => g.key));
    for (const it of NAV_ITEMS) {
      expect(keys.has(it.group)).toBe(true);
    }
  });

  it('三个分组都有成员（空组不呈现）', () => {
    const grouped = regroupNavItems(NAV_ITEMS);
    for (const g of grouped) {
      expect(g.items.length).toBeGreaterThan(0);
    }
    expect(grouped.map((g) => g.label)).toEqual(['创作', '工场', '系统']);
  });

  it('分组归属符合方案（创作5/工场2/系统3）', () => {
    const grouped = regroupNavItems(NAV_ITEMS);
    expect(grouped[0].items.map((i) => i.route)).toEqual(
      ['chat', 'paint', 'storyboard', 'novel', 'learning'],
    );
    expect(grouped[1].items.map((i) => i.route)).toEqual(['models', 'style']);
    expect(grouped[2].items.map((i) => i.route)).toEqual(
      ['settings', 'logs', 'help'],
    );
  });
});

describe('regroupNavItems 兼容解读', () => {
  it('组序优先：跨组穿插的自定义平铺序按组收拢，不炸不丢', () => {
    // 模拟旧版拖出的乱序：系统项插在创作项中间
    const flat: NavItemMeta[] = [
      NAV_ITEMS[7], // settings(system)
      NAV_ITEMS[0], // chat(create)
      NAV_ITEMS[5], // models(forge)
      NAV_ITEMS[1], // paint(create)
      NAV_ITEMS[8], // logs(system)
      NAV_ITEMS[2], // storyboard
      NAV_ITEMS[3], // novel
      NAV_ITEMS[6], // style(forge)
      NAV_ITEMS[4], // learning(create)
      NAV_ITEMS[9], // help(system)
    ];
    const grouped = regroupNavItems(flat);
    // 组序固定
    expect(grouped.map((g) => g.key)).toEqual(['create', 'forge', 'system']);
    // 组内顺序按乱序中的相对位置（settings 在 logs 前；chat 在 paint 前）
    expect(grouped[2].items.map((i) => i.route)).toEqual([
      'settings',
      'logs',
      'help',
    ]);
    expect(grouped[0].items.map((i) => i.route)).toEqual([
      'chat',
      'paint',
      'storyboard',
      'novel',
      'learning',
    ]);
    // 不丢项
    expect(grouped.flatMap((g) => g.items)).toHaveLength(NAV_ITEMS.length);
  });

  it('arrangedNavItems 默认序 = NAV_ITEMS（无 localStorage 记录环境）', () => {
    // jsdom 环境无历史记录时应返回完整默认序
    const items = arrangedNavItems();
    expect(items.map((i) => i.route)).toEqual(NAV_ITEMS.map((i) => i.route));
  });
});
