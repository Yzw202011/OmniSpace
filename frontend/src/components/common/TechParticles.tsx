/**
 * TechParticles 星云粒子系统（Nebula 主题专属环境层）
 * --------------------------------------------------------------------------
 * Canvas 全屏粒子：上浮微粒（青/紫/白，alpha 呼吸 + sin 水平摆动）+
 * 偶发流星拖尾（右上→左下斜掠，低频不喧哗）。
 * 触发条件：仅 data-family=tech（深空/晨辉）渲染；prefers-reduced-motion
 * 时挂载即不渲染（静态粒子无意义）。
 * 性能设计（GTX3060 基线）：
 *   - 单 rAF 循环驱动全部粒子；标签页隐藏自动暂停（visibilitychange）
 *   - 粒子数按视口面积自适应（≤90），流星同屏至多 1 颗
 *   - DPR 适配 + setTransform，物理像素绘制不模糊
 * 画布层级：fixed inset-0 z-index:-1（与极光同层，画于 body 底色之上、
 * 全部内容之下——main-content 透明，全窗可见）。
 */
import { useEffect, useRef } from 'react';
import { useAppStore } from '@/stores/useAppStore';

/** 微粒颜色池（深空：亮色系） */
const DARK_COLORS: ReadonlyArray<readonly [number, number, number]> = [
  [56, 229, 255],  // 电光青
  [103, 232, 249], // 浅青
  [180, 168, 255], // 星云紫
  [232, 242, 255], // 冰白
];

/** 微粒颜色池（晨辉：白底用深色系，避免"污点"感） */
const LIGHT_COLORS: ReadonlyArray<readonly [number, number, number]> = [
  [8, 145, 178],   // 深青
  [14, 165, 233],  // 天青
  [109, 91, 224],  // 深紫
];

interface Particle {
  /** 当前位置（CSS 像素） */
  x: number;
  y: number;
  /** 半径 px */
  r: number;
  /** 上浮速度 px/s */
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

interface Meteor {
  /** 头部位置 */
  x: number;
  y: number;
  /** 速度向量 px/s */
  vx: number;
  vy: number;
  /** 剩余生命 1→0 */
  life: number;
}

/** 区间随机 */
function rand(min: number, max: number): number {
  return min + Math.random() * (max - min);
}

/** 生成一颗微粒（y 可指定——重置时从底部重生） */
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
    r: rand(0.5, 1.8),
    vy: rand(6, 20),
    drift: rand(6, 26),
    phase: rand(0, Math.PI * 2),
    speed: rand(0.6, 1.8),
    color: colors[Math.floor(Math.random() * colors.length)],
    alpha: rand(0.25, 0.7) * alphaScale,
  };
}

export function TechParticles() {
  const theme = useAppStore((s) => s.theme);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const isTech = theme === 'tech' || theme === 'tech-light';

  useEffect(() => {
    if (!isTech) return;
    // 无障碍：减少动效模式下不渲染粒子（挂载时快照即可）
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    // TS 闭包收窄兜底：hoisted function 内不传播 const 判空收窄，
    // 以显式非空类型别名供 resize/frame 闭包安全引用。
    const cvs: HTMLCanvasElement = canvas;
    const c2d: CanvasRenderingContext2D = ctx;

    const light = theme === 'tech-light';
    const colors = light ? LIGHT_COLORS : DARK_COLORS;
    const alphaScale = light ? 0.55 : 1;

    let w = window.innerWidth;
    let h = window.innerHeight;
    let dpr = Math.min(window.devicePixelRatio || 1, 2);
    let particles: Particle[] = [];
    let meteor: Meteor | null = null;
    /** 下一颗流星的时间戳（ms） */
    let nextMeteorAt = performance.now() + rand(2500, 6000);
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
      const count = Math.min(90, Math.floor((w * h) / 18000));
      particles = Array.from({ length: count }, () =>
        makeParticle(w, h, colors, alphaScale),
      );
    }

    /** 单帧：微粒上浮 + 呼吸闪烁 + 流星生命周期推进 */
    function frame(now: number): void {
      if (!running) return;
      const dt = Math.min((now - last) / 1000, 0.05);
      last = now;
      const t = now / 1000;

      c2d.clearRect(0, 0, w, h);

      // 微粒：上浮 + sin 摆动 + alpha 呼吸
      for (const p of particles) {
        p.y -= p.vy * dt;
        if (p.y < -12) {
          // 飘出顶部 → 底部重生
          p.y = h + 12;
          p.x = rand(0, w);
        }
        const sway = Math.sin(t * p.speed * 0.7 + p.phase) * p.drift;
        const twinkle = 0.55 + 0.45 * Math.sin(t * p.speed + p.phase);
        const a = p.alpha * twinkle;
        const [r0, g0, b0] = p.color;
        c2d.beginPath();
        c2d.arc(p.x + sway, p.y, p.r, 0, Math.PI * 2);
        c2d.fillStyle = `rgba(${r0},${g0},${b0},${a.toFixed(3)})`;
        c2d.fill();
      }

      // 流星：低频偶发，右上→左下斜掠
      if (!meteor && now >= nextMeteorAt) {
        meteor = {
          x: rand(w * 0.55, w * 0.95),
          y: rand(-h * 0.05, h * 0.15),
          vx: -rand(180, 300),
          vy: rand(160, 260),
          life: 1,
        };
      }
      if (meteor) {
        const m = meteor;
        m.x += m.vx * dt;
        m.y += m.vy * dt;
        m.life -= dt / 1.4; // 1.4s 生命
        if (m.life <= 0) {
          meteor = null;
          nextMeteorAt = now + rand(4500, 9000);
        } else {
          // 拖尾：头部亮点 → 渐隐尾迹
          const tailX = m.x - m.vx * 0.22;
          const tailY = m.y - m.vy * 0.22;
          const grad = c2d.createLinearGradient(m.x, m.y, tailX, tailY);
          const head = light ? '8,145,178' : '207,243,255';
          const tail = light ? '14,165,233' : '34,211,238';
          grad.addColorStop(0, `rgba(${head},${(0.85 * m.life).toFixed(3)})`);
          grad.addColorStop(1, `rgba(${tail},0)`);
          c2d.strokeStyle = grad;
          c2d.lineWidth = 1.6;
          c2d.beginPath();
          c2d.moveTo(tailX, tailY);
          c2d.lineTo(m.x, m.y);
          c2d.stroke();
          // 头部辉光点
          c2d.beginPath();
          c2d.arc(m.x, m.y, 1.8, 0, Math.PI * 2);
          c2d.fillStyle = `rgba(${head},${(0.9 * m.life).toFixed(3)})`;
          c2d.fill();
        }
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
  }, [isTech, theme]);

  if (!isTech) return null;
  return <canvas ref={canvasRef} className="tech-particles" aria-hidden="true" />;
}

export default TechParticles;
