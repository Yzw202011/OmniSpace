// 本项目仅供学习使用，商业授权请+Q 3553191368
/* ==========================================================================
 * FileGallery.tsx —— 生成物文件库页签（批6 P34，2026-09-19）
 * --------------------------------------------------------------------------
 * 挂在日志页新「作品文件」签：四产物目录按时间倒序分页浏览；缩略图
 * 灯箱预览+下载；只读（删除走各模块正规链——P11 硬边界）。
 * ========================================================================== */

import { useCallback, useEffect, useState } from 'react';
import { Download, FolderOpen, RefreshCw } from 'lucide-react';
import { get } from '@/services/api';
import { reportBgError } from '@/utils/errors';
import OmniLightbox from '@/components/common/OmniLightbox';

interface FileItem {
  module: string;
  module_label: string;
  name: string;
  path: string;
  url: string;
  is_video: boolean;
  size_bytes: number;
  mtime: number;
}

const MODULES = [
  { key: 'all', label: '全部' },
  { key: 'images', label: '生成图片' },
  { key: 'keyframes', label: '关键帧' },
  { key: 'videos', label: '视频' },
  { key: 'exports', label: '导出成品' },
];

const fmtSize = (b: number): string =>
  b >= 1024 ** 3 ? `${(b / 1024 ** 3).toFixed(1)}GB`
  : b >= 1024 ** 2 ? `${(b / 1024 ** 2).toFixed(1)}MB`
  : `${(b / 1024).toFixed(0)}KB`;

export default function FileGallery() {
  const [module, setModule] = useState('all');
  const [items, setItems] = useState<FileItem[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(false);
  const [lightbox, setLightbox] = useState<{ src: string; title: string } | null>(null);

  const load = useCallback((off: number) => {
    setLoading(true);
    get<{ items?: FileItem[]; total?: number }>('/system/files/gallery', {
      module, limit: 48, offset: off,
    })
      .then((d) => {
        setItems(d.items ?? []);
        setTotal(d.total ?? 0);
        setOffset(off);
      })
      .catch((err: unknown) => reportBgError('FileGallery', err))
      .finally(() => setLoading(false));
  }, [module]);

  useEffect(() => { load(0); }, [load]);

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-2 flex-wrap">
        <FolderOpen size={16} aria-hidden="true" />
        <b>作品文件</b>
        <span className="text-tertiary text-xs">共 {total} 个文件（只读——删除请到各模块的正规入口）</span>
        <div className="seg" role="tablist" aria-label="文件类型过滤" style={{ marginLeft: 'auto' }}>
          {MODULES.map((m) => (
            <button
              key={m.key}
              type="button"
              role="tab"
              aria-selected={module === m.key}
              className={`seg-item${module === m.key ? ' active' : ''}`}
              onClick={() => setModule(m.key)}
            >{m.label}</button>
          ))}
        </div>
        <button type="button" className="btn-icon" title="刷新" aria-label="刷新" onClick={() => load(offset)}>
          <RefreshCw size={13} />
        </button>
      </div>

      {loading ? (
        <div className="loading-block" style={{ height: 200 }}><span className="spinner" />加载中…</div>
      ) : items.length === 0 ? (
        <div className="text-secondary text-sm" style={{ padding: 'var(--space-6) 0' }}>
          暂无文件——生成的图片/视频/导出件会出现在这里
        </div>
      ) : (
        <>
          <div className="grid gap-2" style={{
            gridTemplateColumns: 'repeat(auto-fill, minmax(140px, 1fr))',
          }}>
            {items.map((f) => (
              <div key={f.path} className="card hoverable" style={{ padding: 'var(--space-2)', display: 'flex', flexDirection: 'column', gap: 6 }}>
                {f.is_video ? (
                  <video src={f.url} style={{ width: '100%', height: 80, objectFit: 'cover', borderRadius: 'var(--radius-sm)' }} muted />
                ) : (
                  <img
                    src={f.url}
                    alt={f.name}
                    loading="lazy"
                    style={{ width: '100%', height: 80, objectFit: 'cover', borderRadius: 'var(--radius-sm)', cursor: 'zoom-in' }}
                    onClick={() => setLightbox({ src: f.url, title: f.name })}
                  />
                )}
                <span className="text-xs ellipsis" title={f.path}>{f.name}</span>
                <span className="text-tertiary" style={{ fontSize: 10 }}>
                  {f.module_label} · {fmtSize(f.size_bytes)} · {new Date(f.mtime * 1000).toLocaleDateString()}
                </span>
                <a href={f.url} download className="manga-records-link" style={{ fontSize: 11 }}>
                  <Download size={11} aria-hidden="true" /> 下载
                </a>
              </div>
            ))}
          </div>
          {total > 48 && (
            <div className="flex items-center justify-center gap-2">
              <button type="button" className="btn btn-ghost btn-sm" disabled={offset === 0} onClick={() => load(Math.max(0, offset - 48))}>
                上一页
              </button>
              <span className="text-tertiary text-xs">
                {offset + 1}~{Math.min(offset + 48, total)} / {total}
              </span>
              <button type="button" className="btn btn-ghost btn-sm" disabled={offset + 48 >= total} onClick={() => load(offset + 48)}>
                下一页
              </button>
            </div>
          )}
        </>
      )}

      {lightbox && (
        <OmniLightbox src={lightbox.src} title={lightbox.title} onClose={() => setLightbox(null)} />
      )}
    </div>
  );
}
