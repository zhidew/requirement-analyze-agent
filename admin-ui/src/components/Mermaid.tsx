import { useEffect, useState } from 'react';
import mermaid from 'mermaid';
import { useTranslation } from 'react-i18next';
import { Loader as LucideLoader, Maximize2, Copy, Check, X, FileText } from 'lucide-react';

mermaid.initialize({
  startOnLoad: true,
  theme: 'default',
  securityLevel: 'loose',
  fontFamily: 'ui-sans-serif, system-ui, sans-serif',
});

export const Mermaid = ({ chart }: { chart: string }) => {
  const { t } = useTranslation();
  const [svg, setSvg] = useState<string>('');
  const [error, setError] = useState<string | null>(null);
  const [isMaximized, setIsMaximized] = useState(false);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    let isMounted = true;
    
    const renderChart = async () => {
      if (!chart) return;
      
      try {
        const id = `mermaid-${Math.random().toString(36).substr(2, 9)}`;
        const { svg: renderedSvg } = await mermaid.render(id, chart);
        
        if (isMounted) {
          setSvg(renderedSvg);
          setError(null);
        }
      } catch (err: any) {
        console.error('Mermaid render error:', err);
        if (isMounted) {
          setError(err?.message || t('projectDetail.renderingFailed'));
        }
      }
    };

    void renderChart();
    return () => { isMounted = false; };
  }, [chart, t]);

  const handleCopy = () => {
    void navigator.clipboard.writeText(chart);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  if (error) {
    return (
      <div className="my-4">
        <div className="p-2 bg-red-50 text-red-600 rounded-t border border-red-200 text-xs font-bold flex justify-between items-center">
          <span>{t('projectDetail.mermaidError')}: {error}</span>
          <button onClick={handleCopy} className="p-1 hover:bg-red-100 rounded transition-colors" title={t('projectDetail.copySource')}>
            {copied ? <Check size={14} /> : <Copy size={14} />}
          </button>
        </div>
        <pre className="bg-gray-100 p-4 rounded-b overflow-x-auto text-xs border-x border-b border-gray-200 text-gray-900 font-mono leading-relaxed">
          <code>{chart}</code>
        </pre>
      </div>
    );
  }

  if (!svg) {
    return (
      <div className="flex flex-col items-center justify-center p-12 bg-gray-50 rounded-lg border border-gray-100 my-4 gap-3">
        <LucideLoader size={24} className="text-blue-500 animate-spin" />
        <span className="text-gray-400 text-xs font-medium">{t('projectDetail.renderingDiagram')}</span>
      </div>
    );
  }

  return (
    <>
      <div className="group relative my-6">
        <div className="absolute right-2 top-2 flex gap-2 opacity-0 group-hover:opacity-100 transition-opacity z-20">
          <button 
            onClick={handleCopy}
            className="p-1.5 bg-white shadow-sm border border-gray-200 rounded-md text-gray-500 hover:text-blue-600 hover:bg-gray-50 transition-all"
            title={t('projectDetail.copySource')}
          >
            {copied ? <Check size={16} /> : <Copy size={16} />}
          </button>
          <button 
            onClick={() => setIsMaximized(true)}
            className="p-1.5 bg-white shadow-sm border border-gray-200 rounded-md text-gray-500 hover:text-blue-600 hover:bg-gray-50 transition-all"
            title={t('projectDetail.fullscreen')}
          >
            <Maximize2 size={16} />
          </button>
        </div>
        <div 
          className="flex justify-center p-6 bg-white rounded-xl border border-gray-100 overflow-x-auto shadow-sm hover:shadow-md transition-all duration-300" 
          dangerouslySetInnerHTML={{ __html: svg }} 
        />
      </div>

      {isMaximized && (
        <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/60 backdrop-blur-sm p-4 sm:p-10">
          <div className="relative w-full h-full bg-white rounded-2xl shadow-2xl flex flex-col overflow-hidden animate-in fade-in zoom-in duration-200">
            <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100">
              <h3 className="text-sm font-bold text-gray-700 uppercase tracking-wider flex items-center gap-2">
                <FileText size={16} className="text-blue-500" />
                {t('projectDetail.diagramPreview')}
              </h3>
              <div className="flex items-center gap-3">
                <button 
                  onClick={handleCopy}
                  className="inline-flex items-center gap-2 px-3 py-1.5 text-xs font-medium text-gray-600 hover:text-blue-600 hover:bg-blue-50 rounded-lg transition-colors"
                >
                  {copied ? <Check size={14} /> : <Copy size={14} />}
                  {copied ? t('common.copied') : t('projectDetail.copyCode')}
                </button>
                <button 
                  onClick={() => setIsMaximized(false)}
                  className="p-2 text-gray-400 hover:text-gray-600 hover:bg-gray-100 rounded-full transition-colors"
                >
                  <X size={20} />
                </button>
              </div>
            </div>
            <div className="flex-1 overflow-auto p-8 flex items-start justify-center bg-gray-50/30">
              <div 
                className="min-w-full bg-white p-10 rounded-xl shadow-sm border border-gray-100"
                dangerouslySetInnerHTML={{ __html: svg }} 
              />
            </div>
          </div>
        </div>
      )}
    </>
  );
};
