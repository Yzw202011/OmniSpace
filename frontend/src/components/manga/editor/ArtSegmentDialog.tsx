/**
 * ArtSegmentDialog SAM 抠图对话框（/art/segment 前端接线，2026-09-17）
 * --------------------------------------------------------------------------
 * 点选前景提示点（可选，不点则后端用图像中心点）→ SAM 分割 →
 * 蒙版叠加预览（可切换原图/蒙版）+ 下载蒙版 PNG。
 * SAM 权重未装/加载失败由后端语义码如实透出（MODEL_FILE_NOT_FOUND 等）。
 */
import { useEffect, useRef, useState } from 'react';
import { Download, Loader2, Scissors, X } from 'lucide-react';
import { segmentImage } from '../../../services/mangaApi';
import { useAppStore } from '../../../stores/useAppStore';
import { isApiError } from '../../../services/api';

interface Props {
  imageUrl: string;
  assetName: string;
  onClose: () => void;
}

export default function ArtSegmentDialog({ imageUrl, assetName, onClose }: Props) {
  const showToast = useAppStore((s) => s.showToast);
  const [dataUrl, setDataUrl] = useState('');
  const [points, setPoints] = useState<number[][]>([]);
  const [busy, setBusy] = useState(false);
  const [maskB64, setMaskB64] = useState('');
  const [score, setScore] = useState<number | null>(null);
  const [showMask, setShowMask] = useState(true);
  const imgRef = useRef<HTMLImageElement>(null);

  // onClose 入 ref：父层传内联箭头时（AssetDetailPanel 现状），把 onClose
  // 放进依赖会让对话框开着时每次父组件重渲都重新取图重编码（09-17 审计竞态①）
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  // 取图转 dataURL（同源媒体直取）
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const resp = await fetch(imageUrl);
        const blob = await resp.blob();
        const reader = new FileReader();
        reader.onload = () => {
          if (alive) setDataUrl(String(reader.result));
        };
        reader.readAsDataURL(blob);
      } catch {
        showToast('资产图读取失败', 'error');
        onCloseRef.current();
      }
    })();
    return () => {
      alive = false;
    };
  }, [imageUrl, showToast]);

  /** 点选前景提示点（按显示尺寸换算回原图坐标） */
  const onImageClick = (e: React.MouseEvent<HTMLImageElement>) => {
    if (busy || maskB64) return;
    const img = imgRef.current;
    if (!img) return;
    const rect = img.getBoundingClientRect();
    const x = Math.round(((e.clientX - rect.left) / rect.width) * img.naturalWidth);
    const y = Math.round(((e.clientY - rect.top) / rect.height) * img.naturalHeight);
    setPoints((prev) => [...prev, [x, y]]);
  };

  const onSegment = async () => {
    if (!dataUrl) return;
    setBusy(true);
    try {
      const res = await segmentImage(dataUrl, points);
      setMaskB64(res.mask_png_b64);
      setScore(res.score);
    } catch (err: unknown) {
      showToast(isApiError(err) ? err.message : '分割失败', 'error');
    } finally {
      setBusy(false);
    }
  };

  const downloadMask = () => {
    if (!maskB64) return;
    const a = document.createElement('a');
    a.href = `data:image/png;base64,${maskB64}`;
    a.download = `${assetName}_mask.png`;
    a.click();
  };

  return (
    <div
      className="fixed inset-0 flex items-center justify-center"
      style={{ background: 'var(--color-overlay)', zIndex: 1000 }}
      onClick={onClose}
    >
      <div
        className="card"
        style={{ maxWidth: 'min(92vw, 760px)', maxHeight: '90vh', overflow: 'auto', padding: 'var(--space-4)' }}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-label="AI 抠图"
      >
        <div className="flex items-center justify-between mb-2">
          <h3 className="card-title" style={{ margin: 0 }}>
            <Scissors size={15} aria-hidden="true" /> AI 抠图 · {assetName}
          </h3>
          <button type="button" className="btn btn-ghost btn-sm" onClick={onClose} aria-label="关闭">
            <X size={15} />
          </button>
        </div>
        <p className="text-secondary" style={{ fontSize: 12, margin: '0 0 8px' }}>
          在图上<strong>点击要保留的主体</strong>添加提示点（不点则默认取中心）→ 开始抠图 →
          白色=保留、黑色=扣除；可下载蒙版 PNG 用于换背景/合成。
        </p>

        <div className="relative" style={{ display: 'inline-block', lineHeight: 0 }}>
          <img
            ref={imgRef}
            src={dataUrl}
            alt={assetName}
            onClick={onImageClick}
            style={{ maxWidth: '100%', maxHeight: '58vh', cursor: maskB64 || busy ? 'default' : 'crosshair' }}
          />
          {maskB64 && showMask && (
            <img
              src={`data:image/png;base64,${maskB64}`}
              alt="分割蒙版"
              style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', pointerEvents: 'none', mixBlendMode: 'screen', opacity: 0.85 }}
            />
          )}
          {points.map(([x, y], i) => {
            const img = imgRef.current;
            if (!img) return null;
            const rect = img.getBoundingClientRect();
            const host = img.parentElement?.getBoundingClientRect();
            if (!host) return null;
            const left = (x / img.naturalWidth) * rect.width + (rect.left - host.left);
            const top = (y / img.naturalHeight) * rect.height + (rect.top - host.top);
            return (
              <span key={i} style={{ position: 'absolute', left, top, width: 8, height: 8, borderRadius: 4, background: 'var(--color-warning)', border: '1px solid var(--color-text-on-brand)', transform: 'translate(-50%,-50%)', pointerEvents: 'none' }} />
            );
          })}
        </div>

        <div className="flex gap-2 items-center flex-wrap mt-2">
          <button type="button" className="btn btn-primary btn-sm" disabled={!dataUrl || busy} onClick={() => void onSegment()}>
            {busy ? <Loader2 size={13} className="animate-spin" /> : <Scissors size={13} />} {busy ? '分割中…' : '开始抠图'}
          </button>
          {points.length > 0 && !maskB64 && (
            <button type="button" className="btn btn-outline btn-sm" onClick={() => setPoints([])}>清除提示点（{points.length}）</button>
          )}
          {maskB64 && (
            <>
              <button type="button" className="btn btn-outline btn-sm" onClick={() => setShowMask((v) => !v)}>
                {showMask ? '隐藏蒙版' : '显示蒙版'}
              </button>
              <button type="button" className="btn btn-outline btn-sm" onClick={downloadMask}>
                <Download size={13} /> 下载蒙版 PNG
              </button>
              {score != null && <span className="text-secondary" style={{ fontSize: 12 }}>置信分 {score.toFixed(2)}</span>}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
