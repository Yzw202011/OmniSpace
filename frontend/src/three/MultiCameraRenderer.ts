/* ==========================================================================
 * MultiCameraRenderer.ts —— 4 合 1 截图渲染器（规格 §5.7）
 * --------------------------------------------------------------------------
 * 锁定 Three.js r170
 * 职责：
 *   - render4in1(scene, cameras[4], width, height)：4 个机位分别渲染到 RenderTarget
 *   - 2×2 网格拼接为一张图片
 *   - 每格标注机位名称
 *   - 输出 DataURL 供 ScreenshotGrid 组件显示
 * ========================================================================== */

import * as THREE from 'three';

/** 机位定义 */
export interface CameraShot {
  /** 机位名称（标注用） */
  name: string;
  /** 透视相机 */
  camera: THREE.PerspectiveCamera;
}

/** 4合1截图结果 */
export interface Screenshot4in1Result {
  /** 拼接后的图片 DataURL */
  dataUrl: string;
  /** 输出宽度 */
  width: number;
  /** 输出高度 */
  height: number;
  /** 各机位名称 */
  cameraNames: string[];
}

/**
 * 4 合 1 多机位截图渲染器
 * 将 4 个相机视角分别渲染，拼接为 2×2 网格图片。
 */
export class MultiCameraRenderer {
  /** WebGL 渲染器 */
  private renderer: THREE.WebGLRenderer;
  /** 输出画布 */
  private outputCanvas: HTMLCanvasElement;
  /** 输出画布 2D 上下文 */
  private outputCtx: CanvasRenderingContext2D;

  constructor(renderer: THREE.WebGLRenderer) {
    this.renderer = renderer;
    this.outputCanvas = document.createElement('canvas');
    this.outputCtx = this.outputCanvas.getContext('2d')!;
  }

  /**
   * 渲染 4 合 1 截图
   * @param scene 场景对象
   * @param cameras 4 个机位定义（必须恰好 4 个）
   * @param cellWidth 每格宽度（像素），默认 512
   * @param cellHeight 每格高度（像素），默认 384
   * @returns 截图结果
   */
  public render4in1(
    scene: THREE.Scene,
    cameras: CameraShot[],
    cellWidth = 512,
    cellHeight = 384,
  ): Screenshot4in1Result {
    // 规格 §5.7 约束：必须恰好 4 个机位
    if (cameras.length !== 4) {
      throw new Error(`4合1截图需要恰好4个机位，当前 ${cameras.length} 个`);
    }

    const totalWidth = cellWidth * 2;
    const totalHeight = cellHeight * 2;

    // 设置输出画布尺寸
    this.outputCanvas.width = totalWidth;
    this.outputCanvas.height = totalHeight;

    // 创建临时渲染目标
    const renderTarget = new THREE.WebGLRenderTarget(cellWidth, cellHeight, {
      type: THREE.UnsignedByteType,
      minFilter: THREE.LinearFilter,
      magFilter: THREE.LinearFilter,
    });

    // 保存原始状态
    const originalRenderTarget = this.renderer.getRenderTarget();
    const originalSize = new THREE.Vector2();
    this.renderer.getSize(originalSize);
    const originalPixelRatio = this.renderer.getPixelRatio();

    // 设置渲染器尺寸为单格尺寸
    this.renderer.setPixelRatio(1);
    this.renderer.setSize(cellWidth, cellHeight);

    // 像素缓冲区
    const pixels = new Uint8Array(cellWidth * cellHeight * 4);

    // 清空输出画布
    this.outputCtx.fillStyle = '#1a1a1a';
    this.outputCtx.fillRect(0, 0, totalWidth, totalHeight);

    // 2×2 网格位置（左上、右上、左下、右下）
    const gridPositions: Array<{ x: number; y: number }> = [
      { x: 0, y: 0 },              // 左上
      { x: cellWidth, y: 0 },       // 右上
      { x: 0, y: cellHeight },      // 左下
      { x: cellWidth, y: cellHeight }, // 右下
    ];

    const cameraNames: string[] = [];

    // 逐个机位渲染
    for (let i = 0; i < 4; i++) {
      const { name, camera } = cameras[i];
      cameraNames.push(name);

      // 更新相机宽高比
      camera.aspect = cellWidth / cellHeight;
      camera.updateProjectionMatrix();

      // 渲染到 RenderTarget
      this.renderer.setRenderTarget(renderTarget);
      this.renderer.clear();
      this.renderer.render(scene, camera);

      // 读取像素
      this.renderer.readRenderTargetPixels(renderTarget, 0, 0, cellWidth, cellHeight, pixels);

      // 将像素写入临时 canvas（需 Y 轴翻转）
      const tempCanvas = document.createElement('canvas');
      tempCanvas.width = cellWidth;
      tempCanvas.height = cellHeight;
      const tempCtx = tempCanvas.getContext('2d')!;
      const imageData = tempCtx.createImageData(cellWidth, cellHeight);

      // Y 轴翻转
      for (let y = 0; y < cellHeight; y++) {
        const srcRow = (cellHeight - 1 - y) * cellWidth * 4;
        const dstRow = y * cellWidth * 4;
        imageData.data.set(
          pixels.subarray(srcRow, srcRow + cellWidth * 4),
          dstRow,
        );
      }
      tempCtx.putImageData(imageData, 0, 0);

      // 将临时 canvas 绘制到输出画布对应位置
      const pos = gridPositions[i];
      this.outputCtx.drawImage(tempCanvas, pos.x, pos.y, cellWidth, cellHeight);

      // 绘制机位名称标注
      this.drawLabel(name, pos.x, pos.y, cellWidth, cellHeight);
    }

    // 绘制网格分隔线
    this.drawGridLines(totalWidth, totalHeight, cellWidth, cellHeight);

    // 清理
    renderTarget.dispose();
    this.renderer.setRenderTarget(originalRenderTarget);
    this.renderer.setPixelRatio(originalPixelRatio);
    this.renderer.setSize(originalSize.x, originalSize.y);

    return {
      dataUrl: this.outputCanvas.toDataURL('image/png'),
      width: totalWidth,
      height: totalHeight,
      cameraNames,
    };
  }

  /** 绘制机位名称标注 */
  private drawLabel(name: string, x: number, y: number, _w: number, _h: number): void {
    const padding = 8;
    const fontSize = 16;

    this.outputCtx.save();
    this.outputCtx.font = `bold ${fontSize}px "Source Han Sans SC", "Microsoft YaHei", sans-serif`;
    this.outputCtx.textBaseline = 'top';

    // 测量文字宽度
    const metrics = this.outputCtx.measureText(name);
    const textW = metrics.width + padding * 2;
    const textH = fontSize + padding;

    // 半透明背景条
    this.outputCtx.fillStyle = 'rgba(0, 0, 0, 0.6)';
    this.outputCtx.fillRect(x + 4, y + 4, textW, textH);

    // 樱花粉文字
    this.outputCtx.fillStyle = '#F8A5C2';
    this.outputCtx.fillText(name, x + 4 + padding, y + 4 + padding / 2);

    this.outputCtx.restore();
  }

  /** 绘制网格分隔线 */
  private drawGridLines(totalW: number, totalH: number, cellW: number, cellH: number): void {
    this.outputCtx.save();
    this.outputCtx.strokeStyle = 'rgba(248, 165, 194, 0.4)';
    this.outputCtx.lineWidth = 2;

    // 垂直分隔线
    this.outputCtx.beginPath();
    this.outputCtx.moveTo(cellW, 0);
    this.outputCtx.lineTo(cellW, totalH);
    this.outputCtx.stroke();

    // 水平分隔线
    this.outputCtx.beginPath();
    this.outputCtx.moveTo(0, cellH);
    this.outputCtx.lineTo(totalW, cellH);
    this.outputCtx.stroke();

    this.outputCtx.restore();
  }

  /** 获取输出画布 */
  public getCanvas(): HTMLCanvasElement {
    return this.outputCanvas;
  }

  /**
   * 从输出画布获取 DataURL
   * @param mimeType 图片类型
   * @param quality 质量（0~1）
   */
  public toDataURL(mimeType = 'image/png', quality = 0.95): string {
    return this.outputCanvas.toDataURL(mimeType, quality);
  }

  /** 销毁 */
  public dispose(): void {
    // 无需额外清理，画布由 GC 回收
  }
}

export default MultiCameraRenderer;
