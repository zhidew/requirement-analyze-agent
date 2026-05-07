import { Highlight, themes } from 'prism-react-renderer';
import { Copy } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Mermaid } from './Mermaid';

export const CodeBlock = (props: any) => {
  const { t } = useTranslation();
  const { children, className, node, sourceStart, sourceEnd, ...rest } = props;
  const match = /language-(\w+)/.exec(className || '');
  const language = match ? match[1] : '';
  const codeString = String(children).replace(/\n$/, '');
  const dataSourceStart = rest['data-source-start'];
  const dataSourceEnd = rest['data-source-end'];
  const resolvedSourceStart = typeof sourceStart === 'number' ? sourceStart : dataSourceStart;
  const resolvedSourceEnd = typeof sourceEnd === 'number' ? sourceEnd : dataSourceEnd;
  const sourceAttrs = typeof resolvedSourceStart === 'number' && typeof resolvedSourceEnd === 'number'
    ? { 'data-source-start': resolvedSourceStart, 'data-source-end': resolvedSourceEnd }
    : {};

  if (language === 'mermaid') {
    return <Mermaid chart={codeString} />;
  }

  if (language) {
    return (
      <div className="my-6 rounded-xl overflow-hidden border border-gray-200 shadow-sm" {...sourceAttrs}>
        <div className="bg-gray-50 px-4 py-2 border-b border-gray-200 flex justify-between items-center">
          <span className="text-[10px] font-bold text-gray-400 uppercase tracking-widest">{language}</span>
          <button
            onClick={() => {
              void navigator.clipboard.writeText(codeString);
            }}
            className="text-gray-400 hover:text-blue-600 transition-colors"
            title={t('common.copy')}
          >
            <Copy size={14} />
          </button>
        </div>
        <Highlight theme={themes.vsLight} code={codeString} language={language}>
          {({ className, style, tokens, getLineProps, getTokenProps }) => (
            <pre className={`${className} p-4 overflow-x-auto text-sm leading-relaxed`} style={{ ...style, backgroundColor: '#fdfdfd' }}>
              {tokens.map((line, i) => {
                const lineProps = getLineProps({ line, key: i });
                return (
                  <div key={i} {...lineProps}>
                    {line.map((token, key) => {
                      const tokenProps = getTokenProps({ token, key });
                      return <span key={key} {...tokenProps} />;
                    })}
                  </div>
                );
              })}
            </pre>
          )}
        </Highlight>
      </div>
    );
  }

  return (
    <code className="px-1.5 py-0.5 bg-gray-100 text-indigo-700 border border-gray-200 rounded text-[0.85em] font-mono font-semibold mx-0.5" {...sourceAttrs} {...rest}>
      {children}
    </code>
  );
};
