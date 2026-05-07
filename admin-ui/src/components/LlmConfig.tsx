import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { ArrowLeft, RefreshCw, Settings2 } from 'lucide-react';
import { api } from '../api';
import { LanguageSwitcher } from './LanguageSwitcher';

interface LlmConfigState {
  llm_provider: string;
  openai_api_key: string;
  openai_base_url: string;
  openai_model_name: string;
  has_openai_api_key?: boolean;
}

const EMPTY_CONFIG: LlmConfigState = {
  llm_provider: 'openai',
  openai_api_key: '',
  openai_base_url: '',
  openai_model_name: '',
};

export function LlmConfig() {
  const [config, setConfig] = useState<LlmConfigState>(EMPTY_CONFIG);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(null);

  const loadConfig = async () => {
    setLoading(true);
    setMessage(null);
    try {
      const data = await api.getSystemLlmDefaults();
      setConfig({
        llm_provider: data.llm_provider || 'openai',
        openai_api_key: '',
        openai_base_url: data.openai_base_url || '',
        openai_model_name: data.openai_model_name || '',
        has_openai_api_key: data.has_openai_api_key || false,
      });
    } catch {
      setMessage({ type: 'error', text: 'Failed to load LLM config.' });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadConfig();
  }, []);

  return (
    <div className="min-h-screen bg-[#F8FAFC]">
      <div className="max-w-[1100px] mx-auto p-6">
        <div className="flex flex-col gap-5 mb-8 lg:flex-row lg:items-center lg:justify-between">
          <div className="flex items-center gap-4">
            <Link to="/" className="p-2 bg-white rounded-xl shadow-sm border border-gray-200 text-gray-400 hover:text-indigo-600 transition-all">
              <ArrowLeft size={20} />
            </Link>
            <div>
              <div className="text-[10px] font-black text-indigo-500 uppercase tracking-widest mb-0.5">System Config</div>
              <h1 className="text-2xl font-black text-gray-900 uppercase flex items-center gap-3">
                <Settings2 size={24} className="text-indigo-600" />
                LLM Model Config
              </h1>
              <p className="text-sm text-gray-500 mt-1">View system-level LLM defaults. Configure via <code className="bg-gray-100 px-1 rounded">.env</code> file.</p>
            </div>
          </div>
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={() => void loadConfig()}
              disabled={loading}
              className="inline-flex items-center gap-2 px-4 py-2 bg-white border border-gray-200 rounded-xl font-bold text-xs uppercase text-gray-600 hover:text-indigo-600 hover:border-indigo-200 transition-all shadow-sm"
            >
              <RefreshCw size={16} className={loading ? 'animate-spin' : ''} />
              Refresh
            </button>
            <LanguageSwitcher />
          </div>
        </div>

        {message && (
          <div className={`mb-6 rounded-xl border p-4 text-sm shadow-sm ${
            message.type === 'success'
              ? 'border-green-200 bg-green-50 text-green-800'
              : 'border-red-200 bg-red-50 text-red-800'
          }`}>
            {message.text}
          </div>
        )}

        <div className="mb-4 p-4 rounded-xl border border-amber-200 bg-amber-50 text-amber-800 text-sm">
          <strong>Note:</strong> System-level LLM configuration is read-only. To modify defaults, update the <code className="bg-amber-100 px-1 rounded">.env</code> file in the server directory.
        </div>

        <section className="bg-white rounded-3xl border border-gray-100 shadow-sm p-8 space-y-6">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div className="md:col-span-2">
              <div className="text-[10px] font-black text-gray-400 uppercase tracking-widest mb-2">Provider</div>
              <input
                value="OpenAI Compatible"
                disabled
                className="w-full p-3 bg-gray-50 border border-gray-200 rounded-xl text-gray-600"
              />
            </div>

            <div>
              <div className="text-[10px] font-black text-gray-400 uppercase tracking-widest mb-2">OpenAI Base URL</div>
              <input
                value={config.openai_base_url || '(not set)'}
                disabled
                className="w-full p-3 bg-gray-50 border border-gray-200 rounded-xl text-gray-600"
              />
            </div>
            <div>
              <div className="text-[10px] font-black text-gray-400 uppercase tracking-widest mb-2">OpenAI Model</div>
              <input
                value={config.openai_model_name || '(not set)'}
                disabled
                className="w-full p-3 bg-gray-50 border border-gray-200 rounded-xl text-gray-600"
              />
            </div>
            <div className="md:col-span-2">
              <div className="text-[10px] font-black text-gray-400 uppercase tracking-widest mb-2">
                OpenAI API Key
              </div>
              <input
                value={config.has_openai_api_key ? '•••••••• (configured)' : '(not configured)'}
                disabled
                className="w-full p-3 bg-gray-50 border border-gray-200 rounded-xl text-gray-600"
              />
            </div>
          </div>
        </section>
      </div>
    </div>
  );
}
