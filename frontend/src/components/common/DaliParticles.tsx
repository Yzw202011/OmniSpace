/**
 * DaliParticles 萤火粒子系统（Dali 风花雪月主题专属环境层）
 * --------------------------------------------------------------------------
 * Canvas 全屏粒子：慢速上浮萤火（暖金/珊瑚/象牙，alpha 呼吸 + sin 水平摆动）。
 * 与 Nebula 的语言区分：更少、更慢、更小、无流星——「下关风送萤火」的静。
 * 触发条件：仅 data-family=dali（洱海月/苍山雪）渲染；prefers-reduced-motion
 * 时不渲染（静态粒子无意义）。
 * 性能设计（继承 TechParticles，GTX3060 基线）：
 *   - 单 rAF 循环；标签页隐藏自动暂停（visibilitychange）
 *   - 粒子数按视口面积自适应（≤45，约为星云粒子的一半）
 *   - DPR 适配 + setTransform，物理像素绘制不模糊
 * 画布层级：fixed inset-0 z-index:-1（画于氛围洗底之上、全部内容之下）。
 */
import { useEffect, useRef } from 'react';
import { useAppStore } from '@/stores/useAppStore';

/** 萤火颜色池（靛海暗色：冰蓝/珊瑚/月白） */
const DARK_COLORS: ReadonlyArray<readonly [number, number, number]> = [
  [124, 192, 236], // 冰蓝
  [245, 167, 149], // 珊瑚
  [165, 214, 245], // 浅冰蓝
  [240, 248, 255], // 月白
];

/** 萤火颜色池（晨海亮色：深湖蓝/深珊瑚系，避免白底「污点」感） */
const LIGHT_COLORS: ReadonlyArray<readonly [number, number, number]> = [
  [46, 111, 174],  // 深湖蓝
  [217, 117, 95],  // 深珊瑚
  [90, 120, 160],  // 蓝灰
];

interface Particle {
  /** 当前位置（CSS 像素） */
  x: number;
  y: number;
  /** 半径 px */
  r: number;
  /** 上浮速度 px/s（萤火比星尘慢） */
  vy: number;
  /** 水平摆幅 px */
  drift: number;
  /** 呼吸相位 rad */
  phase: number;
  /** 呼吸角速度 rad/s */
  speed: number;
  /** 颜色 */
  color: readonly [number, number, number];
  /** 基础不透明度 */
  alpha: number;
}

/** 区间随机 */
function rand(min: number, max: number): number {
  return min + Math.random() * (max - min);
}

/** 生成一颗萤火（y 可指定——重置时从底部重生） */
function makeParticle(
  w: number,
  h: number,
  colors: ReadonlyArray<readonly [number, number, number]>,
  alphaScale: number,
  y?: number,
): Particle {
  return {
    x: rand(0, w),
    y: y ?? rand(0, h),
    r: rand(0.5, 1.5),
    vy: rand(3, 10),
    drift: rand(8, 30),
    phase: rand(0, Math.PI * 2),
    speed: rand(0.4, 1.2),
    color: colors[Math.floor(Math.random() * colors.length)],
    alpha: rand(0.18, 0.5) * alphaScale,
  };
}

export function DaliParticles() {
  const theme = useAppStore((s) => s.theme);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const isDali = theme === 'dali' || theme === 'dali-light';

  useEffect(() => {
    if (!isDali) return;
    // 无障碍：减少动效模式下不渲染粒子（挂载时快照即可）
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    // TS 闭包收窄兜底（同 TechParticles：显式非空别名供闭包安全引用）
    const cvs: HTMLCanvasElement = canvas;
    const c2d: CanvasRenderingContext2D = ctx;

    const light = theme === 'dali-light';
    const colors = light ? LIGHT_COLORS : DARK_COLORS;
    const alphaScale = light ? 0.6 : 1;

    let w = window.innerWidth;
    let h = window.innerHeight;
    let dpr = Math.min(window.devicePixelRatio || 1, 2);
    let particles: Particle[] = [];
    let raf = 0;
    let last = performance.now();
    let running = true;

    /** 尺寸变化：重建画布物理尺寸 + 依面积重播粒子 */
    function resize(): void {
      w = window.innerWidth;
      h = window.innerHeight;
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      cvs.width = Math.round(w * dpr);
      cvs.height = Math.round(h * dpr);
      c2d.setTransform(dpr, 0, 0, dpr, 0, 0);
      const count = Math.min(45, Math.floor((w * h) / 36000));
      particles = Array.from({ length: count }, () =>
        makeParticle(w, h, colors, alphaScale),
      );
    }

    /** 单帧：萤火慢浮 + 呼吸明灭 */
    function frame(now: number): void {
      if (!running) return;
      const dt = Math.min((now - last) / 1000, 0.05);
      last = now;
      const t = now / 1000;

      c2d.clearRect(0, 0, w, h);

      for (const p of particles) {
        p.y -= p.vy * dt;
        if (p.y < -12) {
          // 飘出顶部 → 底部重生
          p.y = h + 12;
          p.x = rand(0, w);
        }
        const sway = Math.sin(t * p.speed * 0.6 + p.phase) * p.drift;
        const twinkle = 0.45 + 0.55 * Math.sin(t * p.speed + p.phase);
        const a = p.alpha * twinkle;
        const [r0, g0, b0] = p.color;
        c2d.beginPath();
        c2d.arc(p.x + sway, p.y, p.r, 0, Math.PI * 2);
        c2d.fillStyle = `rgba(${r0},${g0},${b0},${a.toFixed(3)})`;
        c2d.fill();
      }

      raf = requestAnimationFrame(frame);
    }

    /** 标签页隐藏暂停 / 恢复（时间基线同步重置，避免 dt 跳变） */
    function onVisibility(): void {
      if (document.hidden) {
        running = false;
        cancelAnimationFrame(raf);
      } else {
        running = true;
        last = performance.now();
        raf = requestAnimationFrame(frame);
      }
    }

    resize();
    raf = requestAnimationFrame(frame);
    window.addEventListener('resize', resize);
    document.addEventListener('visibilitychange', onVisibility);

    return () => {
      running = false;
      cancelAnimationFrame(raf);
      window.removeEventListener('resize', resize);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [isDali, theme]);

  if (!isDali) return null;
  return <canvas ref={canvasRef} className="dali-particles" aria-hidden="true" />;
}

export default DaliParticles;
// 本项目仅供学习使用，商业授权请+Q 3559331368
