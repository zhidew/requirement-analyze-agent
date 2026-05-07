import React, { useEffect, useMemo, useState } from 'react';
import { api } from '../api';
import { Link, useLocation } from 'react-router-dom';
import { Folder, Plus, RefreshCw, Settings, LayoutDashboard, Search } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { LanguageSwitcher } from './LanguageSwitcher';

interface Project {
  id: string;
  name: string;
  description?: string;
  created_at?: string;
  updated_at?: string;
  total_versions?: number;
  enabled_experts_count?: number;
  running_versions?: number;
  success_versions?: number;
  failed_versions?: number;
  waiting_versions?: number;
  queued_versions?: number;
  unknown_versions?: number;
  status_counts?: Record<string, number>;
  has_versions?: boolean;
  is_active?: boolean;
  status?: string;
}

export function ProjectList() {
  const { t, i18n } = useTranslation();
  const location = useLocation();
  const [projects, setProjects] = useState<Project[]>([]);
  const [newProjectName, setNewProjectName] = useState('');
  const [searchTerm, setSearchTerm] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [isCreating, setIsCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    loadProjects();
  }, []);

  const loadProjects = async () => {
    setIsLoading(true);
    setError(null);
    try {
      const data = await api.getProjects();
      setProjects(data);
    } catch {
      setError(t('common.loadError') || 'Failed to load projects');
    } finally {
      setIsLoading(false);
    }
  };

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newProjectName.trim()) return;
    setIsCreating(true);
    setError(null);
    try {
      await api.createProject(newProjectName.trim());
      setNewProjectName('');
      await loadProjects();
    } catch {
      setError(t('common.error') || 'Failed to create project');
    } finally {
      setIsCreating(false);
    }
  };

  const filteredProjects = useMemo(() => {
    if (!searchTerm.trim()) {
      return projects;
    }
    const term = searchTerm.toLowerCase();
    return projects.filter((proj) => {
      return (
        proj.id.toLowerCase().includes(term) ||
        proj.name.toLowerCase().includes(term) ||
        (proj.description?.toLowerCase().includes(term) || false)
      );
    });
  }, [projects, searchTerm]);

  const expertsEnabledLabel = useMemo(() => {
    const isZh = i18n.language.toLowerCase().startsWith('zh');
    const value = t('projectList.hasExpertsEnabled', { count: 0 });
    return /\?{2,}/.test(value) || value === 'projectList.hasExpertsEnabled'
      ? (count: number) => (isZh ? `${count} ?????` : `${count} experts enabled`)
      : (count: number) => t('projectList.hasExpertsEnabled', { count });
  }, [i18n.language, t]);

  return (
    <div className="max-w-[1400px] mx-auto p-6 bg-gray-50/30 min-h-screen">
      <div className="flex justify-between items-center mb-8">
        <div className="flex items-center gap-4">
          <div className="p-3 bg-indigo-600 rounded-2xl shadow-lg shadow-indigo-200 text-white">
            <LayoutDashboard size={24} />
          </div>
          <div>
            <div className="text-[10px] font-black text-indigo-500 uppercase tracking-widest mb-0.5">{t('projectList.subtitle')}</div>
            <h1 className="text-2xl font-black text-gray-900 uppercase">{t('projectList.title')}</h1>
          </div>
        </div>
        <div className="flex items-center gap-3">
          <Link
            to="/expert-center"
            className="inline-flex items-center gap-2 px-4 py-2 bg-white border border-gray-200 rounded-xl font-bold text-xs uppercase text-gray-600 hover:text-indigo-600 hover:border-indigo-200 transition-all shadow-sm"
          >
            <Settings size={16} />
            {t('management.title')}
          </Link>
          <button
            type="button"
            onClick={loadProjects}
            disabled={isLoading}
            className="inline-flex items-center gap-2 px-4 py-2 bg-white border border-gray-200 rounded-xl font-bold text-xs uppercase text-gray-600 hover:text-indigo-600 hover:border-indigo-200 transition-all shadow-sm"
          >
            <RefreshCw size={16} className={isLoading ? 'animate-spin' : ''} />
            {t('common.refresh')}
          </button>
          <div className="h-8 w-px bg-gray-200 mx-2" />
          <LanguageSwitcher />
        </div>
      </div>

      {error && (
        <div className="mb-6 rounded-xl border border-rose-200 bg-rose-50 p-4 text-sm text-rose-700 flex items-center justify-between shadow-sm">
          <span>{error}</span>
          <button onClick={loadProjects} className="text-xs font-bold uppercase">{t('common.retry')}</button>
        </div>
      )}

      <div className="bg-white p-6 rounded-2xl shadow-sm border border-gray-200 mb-8 relative overflow-hidden">
        <div className="absolute top-0 right-0 p-4 opacity-5">
          <Plus size={64} className="text-indigo-600" />
        </div>
        <h2 className="text-[10px] font-black text-gray-400 uppercase tracking-widest mb-4">{t('projectList.createTitle')}</h2>
        <form onSubmit={handleCreate} className="flex flex-col sm:flex-row gap-4 relative z-10">
          <div className="flex-1 relative">
            <input
              id="project-name"
              type="text"
              value={newProjectName}
              onChange={(e) => setNewProjectName(e.target.value)}
              placeholder={t('projectList.projectNamePlaceholder')}
              className="w-full p-3 bg-gray-50 border border-gray-200 rounded-xl focus:ring-2 focus:ring-indigo-500 focus:bg-white outline-none transition-all"
            />
          </div>
          <div className="relative flex-shrink-0 sm:w-48">
            <Plus size={48} className="absolute -top-3 -right-3 opacity-10 text-indigo-600 pointer-events-none" />
            <button
              type="submit"
              disabled={isCreating || !newProjectName.trim()}
              className="w-full bg-indigo-600 text-white px-6 py-3 rounded-xl font-bold text-xs uppercase hover:bg-indigo-700 disabled:opacity-50 shadow-lg shadow-indigo-100 transition-all flex items-center justify-center relative z-10"
            >
              {t('projectList.createBtn')}
            </button>
          </div>
        </form>
      </div>

      {/* Search Box */}
      {projects.length > 0 && (
        <div className="mb-6">
          <div className="relative">
            <div className="absolute inset-y-0 left-0 pl-4 flex items-center pointer-events-none text-gray-400">
              <Search size={16} />
            </div>
            <input
              type="text"
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              placeholder={t('projectList.searchPlaceholder') || 'Search projects by name or ID...'}
              className="w-full bg-white border border-gray-200 rounded-xl pl-11 pr-4 py-3 text-sm outline-none focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100 transition-all"
            />
          </div>
        </div>
      )}

      {isLoading ? (
        <div className="rounded-2xl border border-gray-200 bg-white p-20 text-center flex flex-col items-center gap-4">
           <RefreshCw size={32} className="text-indigo-500 animate-spin" />
           <span className="text-sm font-bold text-gray-400 uppercase tracking-widest">{t('common.loadingProjects')}</span>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {filteredProjects.map((proj) => (
            <div
              key={proj.id}
              className="group bg-white p-6 rounded-2xl shadow-sm border border-gray-200 hover:border-indigo-500 hover:shadow-xl hover:shadow-indigo-50 transition-all relative overflow-hidden"
            >
              <div className="flex items-start justify-between gap-4 mb-4">
                <Link to={`/projects/${proj.id}`} className="flex items-center gap-4 min-w-0">
                  <div className="p-3 bg-gray-50 rounded-xl text-indigo-600 group-hover:bg-indigo-600 group-hover:text-white transition-all">
                    <Folder size={20} />
                  </div>
                  <div className="min-w-0">
                    <h3 className="text-sm font-black text-gray-900 uppercase truncate max-w-[200px]">{proj.name}</h3>
                    <span className="text-[10px] font-mono text-gray-400 uppercase">{t('common.id')}: {proj.id}</span>
                  </div>
                </Link>
                <Link
                  to={`/projects/${proj.id}/config`}
                  state={{ from: location.pathname }}
                  onClick={(e) => e.stopPropagation()}
                  className="inline-flex h-10 w-10 items-center justify-center rounded-xl border border-gray-200 bg-white text-gray-400 hover:text-indigo-600 hover:border-indigo-200 transition-all shadow-sm"
                  title={t('common.configuration')}
                >
                  <Settings size={16} />
                </Link>
              </div>
              <Link to={`/projects/${proj.id}`} className="block">
                <div className={`grid gap-1.5 mb-2 ${(proj.unknown_versions ?? 0) > 0 ? 'grid-cols-6' : 'grid-cols-5'}`}>
                  <div className="p-1.5 bg-gray-50 rounded-lg group-hover:bg-indigo-50 transition-all text-center">
                    <div className="text-[8px] font-bold text-gray-400 uppercase tracking-wider mb-0.5">{t('projectList.total') || 'Total'}</div>
                    <div className="text-base font-black text-gray-900">{proj.total_versions ?? 0}</div>
                  </div>
                  <div className="p-1.5 bg-indigo-50 rounded-lg text-center">
                    <div className="text-[8px] font-bold text-indigo-400 uppercase tracking-wider mb-0.5">{t('projectList.running') || 'Run'}</div>
                    <div className="text-base font-black text-indigo-600">{proj.running_versions ?? 0}</div>
                  </div>
                  <div className="p-1.5 bg-emerald-50 rounded-lg text-center">
                    <div className="text-[8px] font-bold text-emerald-400 uppercase tracking-wider mb-0.5">{t('projectList.success') || 'OK'}</div>
                    <div className="text-base font-black text-emerald-600">{proj.success_versions ?? 0}</div>
                  </div>
                  <div className="p-1.5 bg-rose-50 rounded-lg text-center">
                    <div className="text-[8px] font-bold text-rose-400 uppercase tracking-wider mb-0.5">{t('projectList.failed') || 'Fail'}</div>
                    <div className="text-base font-black text-rose-600">{proj.failed_versions ?? 0}</div>
                  </div>
                  <div className="p-1.5 bg-amber-50 rounded-lg text-center">
                    <div className="text-[8px] font-bold text-amber-400 uppercase tracking-wider mb-0.5">{t('projectList.waiting') || 'Wait'}</div>
                    <div className="text-base font-black text-amber-600">{(proj.waiting_versions ?? 0) + (proj.queued_versions ?? 0)}</div>
                  </div>
                  {(proj.unknown_versions ?? 0) > 0 && (
                    <div className="p-1.5 bg-gray-100 rounded-lg text-center">
                      <div className="text-[8px] font-bold text-gray-400 uppercase tracking-wider mb-0.5">{t('projectList.unknown') || 'Other'}</div>
                      <div className="text-base font-black text-gray-500">{proj.unknown_versions}</div>
                    </div>
                  )}
                </div>
              </Link>
              <div className="pt-4 border-t border-gray-50 flex items-center justify-between">
                    <span className="text-[10px] font-bold text-gray-400 uppercase">
                      {proj.status === 'active' 
                        ? t('projectList.workspaceActive')
                        : proj.has_versions 
                          ? expertsEnabledLabel(proj.enabled_experts_count ?? 0)
                          : t('projectList.emptyWorkspace') || 'Empty workspace'
                      }
                    </span>
                {proj.status !== 'empty' && (
                  <div className={`flex items-center gap-1.5 text-[10px] font-bold uppercase ${
                    proj.status === 'active' 
                      ? 'text-amber-500' 
                      : 'text-emerald-500'
                  }`}>
                    <div className={`h-1.5 w-1.5 rounded-full ${
                      proj.status === 'active' 
                        ? 'bg-amber-500' 
                        : 'bg-emerald-500'
                    }`} /> 
                    {proj.status === 'active' 
                      ? t('projectList.running') || 'Running'
                      : t('projectList.ready') 
                    }
                  </div>
                )}
              </div>
            </div>
          ))}
          {filteredProjects.length === 0 && searchTerm && (
            <div className="col-span-full text-center py-20 bg-white rounded-2xl border border-gray-200 border-dashed">
              <div className="max-w-xs mx-auto flex flex-col items-center gap-4">
                <Search size={48} className="text-gray-200" />
                <p className="text-sm font-bold text-gray-400 uppercase tracking-widest leading-relaxed">
                  {t('projectList.noSearchResults') || 'No projects match your search'}
                </p>
              </div>
            </div>
          )}
          {projects.length === 0 && (
            <div className="col-span-full text-center py-20 bg-white rounded-2xl border border-gray-200 border-dashed">
              <div className="max-w-xs mx-auto flex flex-col items-center gap-4">
                <Folder size={48} className="text-gray-200" />
                <p className="text-sm font-bold text-gray-400 uppercase tracking-widest leading-relaxed">{t('projectList.noProjects')}</p>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
