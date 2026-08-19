/* ==========================================================================
 * ImportDoc.tsx —— 导入文档学习（TASK-036）
 * --------------------------------------------------------------------------
 * 拖拽 / 点击上传 pdf / docx / txt → POST /v1/knowledge/import-document
 * 显示上传中进度（不定进度条）与导入结果。
 * ========================================================================== */

import React, { useRef, useState } from 'react';
import { FileText, Upload, Loader2 } from 'lucide-react';
import { useLearningStore } from '@/stores/useLearningStore';

/** 支持的文档扩展名 */
const ACCEPT = '.pdf,.docx,.txt';

export const ImportDoc: React.FC = () => {
  const importing = useLearningStore((s) => s.importing);
  const importDocument = useLearningStore((s) => s.importDocument);

  const [dragOver, setDragOver] = useState(false);
  const [lastResult, setLastResult] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const handleFile = async (file: File | undefined) => {
    if (!file) return;
    const ext = file.name.slice(file.name.lastIndexOf('.')).toLowerCase();
    if (!ACCEPT.includes(ext)) {
      setLastResult(`不支持的文件类型 ${ext}（仅支持 pdf / docx / txt）`);
      return;
    }
    const ok = await importDocument(file);
    setLastResult(ok ? `「${file.name}」导入完成` : `「${file.name}」导入失败`);
  };

  return (
    <section className="card hoverable" aria-label="导入文档">
      <h3 className="card-title"><FileText size={16} aria-hidden="true" /> 导入文档学习</h3>

      <div
        role="button"
        tabIndex={0}
        aria-label="拖拽或点击上传文档"
        className="flex-center flex-col gap-2"
        style={{
          padding: 'var(--space-8)',
          border: `2px dashed ${dragOver ? 'var(--color-primary)' : 'var(--color-border)'}`,
          borderRadius: 'var(--radius-lg)',
          background: dragOver ? 'var(--color-primary-50)' : 'var(--color-surface-secondary)',
          cursor: importing ? 'wait' : 'pointer',
          transition: 'all var(--duration-hover) var(--ease-out)',
        }}
        onClick={() => !importing && inputRef.current?.click()}
        onKeyDown={(e) => { if (e.key === 'Enter') inputRef.current?.click(); }}
        onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragOver(false);
          handleFile(e.dataTransfer.files?.[0]);
        }}
      >
        <span style={{ fontSize: 32, color: 'var(--color-primary)', lineHeight: 1, display: 'flex' }} aria-hidden="true">
          {importing ? <Loader2 size={32} className="animate-spin" /> : <Upload size={32} />}
        </span>
        <div className="text-sm">
          {importing ? '正在导入，请稍候…' : '拖拽文件到此处，或点击选择文件'}
        </div>
        <div className="text-tertiary" style={{ fontSize: 'var(--font-size-xs)' }}>
          支持 PDF / DOCX / TXT，导入后自动提取知识点
        </div>
      </div>

      {importing && (
        <div className="progress mt-3">
          <div className="progress-bar indeterminate" />
        </div>
      )}
      {lastResult && !importing && (
        <div className="text-secondary mt-3" style={{ fontSize: 'var(--font-size-sm)' }}>
          {lastResult}
        </div>
      )}

      <input
        ref={inputRef}
        type="file"
        accept={ACCEPT}
        hidden
        onChange={(e) => {
          handleFile(e.target.files?.[0]);
          e.target.value = '';
        }}
      />
    </section>
  );
};

export default ImportDoc;
