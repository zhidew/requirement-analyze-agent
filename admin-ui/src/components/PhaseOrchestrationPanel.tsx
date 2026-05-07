import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Loader as LucideLoader,
  RefreshCw,
  Save,
  X,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { apiClient } from '../api';

interface PhaseItem {
  id: string;
  label: string;
  label_zh: string;
  label_en: string;
  executable: boolean;
  order: number;
  experts: string[];
}

interface PhaseExpert {
  id: string;
  name: string;
  name_zh?: string | null;
  name_en?: string | null;
  description?: string | null;
  phase: string;
}

interface PhaseOrchestrationPayload {
  phases: PhaseItem[];
  experts: PhaseExpert[];
  validation_errors: string[];
}

interface DependencyFinding {
  severity: 'error' | 'warning' | 'info';
  code: string;
  message: string;
  expert_id?: string | null;
  related_expert_id?: string | null;
  details: Record<string, unknown>;
}

interface DependencyValidationReport {
  ok: boolean;
  expert_count: number;
  dependency_edges: number;
  summary: {
    errors: number;
    warnings: number;
    infos: number;
  };
  findings: DependencyFinding[];
}

interface PhaseOrchestrationPanelProps {
  expertVersionKey: string;
}

const FIXED_PHASES = new Set(['INIT', 'PLANNING', 'DONE']);
const FIXED_PHASE_CARD_WIDTH = 143;
const CONFIGURABLE_PHASE_CARD_WIDTH = 165;

function clonePhases(phases: PhaseItem[]): PhaseItem[] {
  return phases.map((phase) => ({ ...phase, experts: [...phase.experts] }));
}

function isConfigurablePhase(phase: Pick<PhaseItem, 'id' | 'executable'>): boolean {
  return phase.executable && !FIXED_PHASES.has(phase.id);
}

function extractApiErrorDetail(error: unknown): string {
  if (typeof error !== 'object' || error === null || !('response' in error)) {
    return '';
  }
  const response = (error as { response?: { data?: { detail?: unknown } } }).response;
  const detail = response?.data?.detail;
  return typeof detail === 'string' ? detail : '';
}

function getDependencyRecommendationKey(code: string): string {
  switch (code) {
    case 'MISSING_PHASE_BINDING':
      return 'management.dependencyRecommendations.missingPhaseBinding';
    case 'BACKWARD_PHASE_DEPENDENCY':
      return 'management.dependencyRecommendations.backwardPhaseDependency';
    case 'DEPENDENCY_CYCLE':
      return 'management.dependencyRecommendations.dependencyCycle';
    case 'MISSING_DEPENDENCY':
    case 'UNKNOWN_UPSTREAM_EXPERT':
      return 'management.dependencyRecommendations.missingDependency';
    case 'MISSING_UPSTREAM_ARTIFACT_MAPPING':
    case 'DEPENDENCY_WITHOUT_ARTIFACT_MAPPING':
      return 'management.dependencyRecommendations.missingUpstreamArtifactMapping';
    case 'UPSTREAM_NOT_IN_DEPENDENCIES':
      return 'management.dependencyRecommendations.upstreamNotInDependencies';
    case 'UNKNOWN_UPSTREAM_ARTIFACT':
      return 'management.dependencyRecommendations.unknownUpstreamArtifact';
    case 'UPSTREAM_HAS_NO_EXPECTED_OUTPUTS':
      return 'management.dependencyRecommendations.upstreamHasNoExpectedOutputs';
    case 'EMPTY_UPSTREAM_ARTIFACT_MAPPING':
      return 'management.dependencyRecommendations.emptyUpstreamArtifactMapping';
    case 'BOUNDARY_INPUT_MISMATCH':
      return 'management.dependencyRecommendations.boundaryInputMismatch';
    case 'DUPLICATE_EXPECTED_OUTPUT':
      return 'management.dependencyRecommendations.duplicateExpectedOutput';
    case 'SELF_DEPENDENCY':
    case 'SELF_UPSTREAM_ARTIFACT':
      return 'management.dependencyRecommendations.selfDependency';
    case 'DUPLICATE_PHASE_ASSIGNMENT':
      return 'management.dependencyRecommendations.duplicatePhaseAssignment';
    default:
      return 'management.dependencyRecommendations.default';
  }
}

export function PhaseOrchestrationPanel({ expertVersionKey }: PhaseOrchestrationPanelProps) {
  const { t, i18n } = useTranslation();
  const [payload, setPayload] = useState<PhaseOrchestrationPayload | null>(null);
  const [draftPhases, setDraftPhases] = useState<PhaseItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [validatingDependencies, setValidatingDependencies] = useState(false);
  const [validationReport, setValidationReport] = useState<DependencyValidationReport | null>(null);
  const [selectedPhaseId, setSelectedPhaseId] = useState('');
  const [focusPhaseId, setFocusPhaseId] = useState('');
  const [message, setMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(null);
  const phaseSequenceRef = useRef<HTMLDivElement | null>(null);
  const phaseButtonRefs = useRef<Record<string, HTMLButtonElement | null>>({});

  const loadPhaseOrchestration = useCallback(async () => {
    setLoading(true);
    try {
      const response = await apiClient.get('/expert-center/phase-orchestration');
      const nextPayload = response.data as PhaseOrchestrationPayload;
      setPayload(nextPayload);
      setDraftPhases(clonePhases(nextPayload.phases || []));
      setMessage(null);
    } catch (err: unknown) {
      setMessage({
        type: 'error',
        text: extractApiErrorDetail(err) || t('management.phaseOrchestrationLoadError'),
      });
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    void loadPhaseOrchestration();
  }, [expertVersionKey, loadPhaseOrchestration]);

  const orderedPhases = useMemo(
    () => [...draftPhases].sort((left, right) => left.order - right.order),
    [draftPhases],
  );

  const selectedPhase = useMemo(
    () => orderedPhases.find((phase) => phase.id === selectedPhaseId) ?? null,
    [orderedPhases, selectedPhaseId],
  );

  useEffect(() => {
    const activePhase = orderedPhases.find((phase) => phase.id === selectedPhaseId);
    if (activePhase && isConfigurablePhase(activePhase)) {
      return;
    }
    const firstConfigurable = orderedPhases.find((phase) => isConfigurablePhase(phase));
    setSelectedPhaseId(firstConfigurable?.id || '');
  }, [orderedPhases, selectedPhaseId]);

  useEffect(() => {
    const targetPhaseId = focusPhaseId || selectedPhaseId;
    if (!targetPhaseId) {
      return;
    }
    const button = phaseButtonRefs.current[targetPhaseId];
    if (button) {
      if (focusPhaseId) {
        button.focus();
      }
      button.scrollIntoView({
        behavior: 'smooth',
        inline: 'center',
        block: 'nearest',
      });
    }
    if (focusPhaseId) {
      setFocusPhaseId('');
    }
  }, [focusPhaseId, orderedPhases, selectedPhaseId]);

  const expertDisplayName = (expert: PhaseExpert) => {
    const isZh = i18n.language.toLowerCase().startsWith('zh');
    return isZh
      ? (expert.name_zh || expert.name || expert.name_en || expert.id)
      : (expert.name_en || expert.name || expert.name_zh || expert.id);
  };

  const expertMap = useMemo(() => {
    const map: Record<string, PhaseExpert> = {};
    for (const expert of payload?.experts || []) {
      map[expert.id] = expert;
    }
    return map;
  }, [payload?.experts]);

  const assignedPhaseByExpert = useMemo(() => {
    const map: Record<string, string> = {};
    for (const phase of draftPhases) {
      for (const expertId of phase.experts) {
        map[expertId] = phase.id;
      }
    }
    return map;
  }, [draftPhases]);

  const unassignedExperts = useMemo(
    () => (payload?.experts || []).filter((expert) => !assignedPhaseByExpert[expert.id]),
    [payload?.experts, assignedPhaseByExpert],
  );

  const hasChanges = useMemo(() => {
    if (!payload) {
      return false;
    }
    return JSON.stringify(draftPhases) !== JSON.stringify(payload.phases);
  }, [draftPhases, payload]);

  const configurablePhaseIds = useMemo(
    () => orderedPhases.filter((phase) => isConfigurablePhase(phase)).map((phase) => phase.id),
    [orderedPhases],
  );

  const optimizationSuggestions = useMemo(() => {
    if (!validationReport?.findings.length) {
      return [];
    }
    const uniqueSuggestions = new Map<string, string>();
    validationReport.findings.forEach((finding) => {
      const key = finding.code;
      if (!uniqueSuggestions.has(key)) {
        uniqueSuggestions.set(key, t(getDependencyRecommendationKey(finding.code)));
      }
    });
    return Array.from(uniqueSuggestions.values());
  }, [validationReport, t]);

  const moveExpertToPhase = (expertId: string, phaseId: string) => {
    setDraftPhases((prev) =>
      prev.map((phase) => {
        const nextExperts = phase.experts.filter((item) => item !== expertId);
        if (phase.id === phaseId && !nextExperts.includes(expertId)) {
          nextExperts.push(expertId);
        }
        return { ...phase, experts: nextExperts };
      }),
    );
  };

  const removeExpertFromPhase = (expertId: string, phaseId: string) => {
    setDraftPhases((prev) =>
      prev.map((phase) => (
        phase.id === phaseId
          ? { ...phase, experts: phase.experts.filter((item) => item !== expertId) }
          : phase
      )),
    );
  };

  const moveSelectedPhase = (direction: 'left' | 'right') => {
    if (!selectedPhase) {
      return;
    }
    setFocusPhaseId(selectedPhase.id);
    const currentIndex = configurablePhaseIds.indexOf(selectedPhase.id);
    if (currentIndex === -1) {
      return;
    }
    const targetIndex = direction === 'left' ? currentIndex - 1 : currentIndex + 1;
    if (targetIndex < 0 || targetIndex >= configurablePhaseIds.length) {
      return;
    }
    const targetPhaseId = configurablePhaseIds[targetIndex];
    setDraftPhases((prev) => {
      const currentPhase = prev.find((phase) => phase.id === selectedPhase.id);
      const targetPhase = prev.find((phase) => phase.id === targetPhaseId);
      if (!currentPhase || !targetPhase) {
        return prev;
      }
      return prev.map((phase) => {
        if (phase.id === currentPhase.id) {
          return { ...phase, order: targetPhase.order };
        }
        if (phase.id === targetPhase.id) {
          return { ...phase, order: currentPhase.order };
        }
        return phase;
      });
    });
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      const response = await apiClient.put('/expert-center/phase-orchestration', {
        phases: draftPhases.map((phase) => ({ id: phase.id, order: phase.order, experts: phase.experts })),
      });
      const nextPayload = response.data as PhaseOrchestrationPayload;
      setPayload(nextPayload);
      setDraftPhases(clonePhases(nextPayload.phases || []));
      setMessage({ type: 'success', text: t('management.phaseOrchestrationSaveSuccess') });
      if (validationReport) {
        void loadDependencyValidation(true);
      }
    } catch (err: unknown) {
      setMessage({
        type: 'error',
        text: extractApiErrorDetail(err) || t('management.phaseOrchestrationSaveError'),
      });
    } finally {
      setSaving(false);
    }
  };

  const loadDependencyValidation = useCallback(async (silent = false) => {
    setValidatingDependencies(true);
    try {
      const response = await apiClient.get('/expert-center/experts/validate-dependencies');
      const nextReport = response.data as DependencyValidationReport;
      setValidationReport(nextReport);
      if (!silent) {
        setMessage({
          type: nextReport.ok ? 'success' : 'error',
          text: nextReport.ok ? t('management.validationSuccess') : t('management.validationIssuesFound'),
        });
      }
    } catch (err: unknown) {
      if (!silent) {
        setMessage({
          type: 'error',
          text: extractApiErrorDetail(err) || t('management.validationLoadError'),
        });
      }
    } finally {
      setValidatingDependencies(false);
    }
  }, [t]);

  return (
    <div className="space-y-6">
      <section className="bg-white rounded-2xl border border-gray-200 shadow-sm p-6">
        <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-start">
          <div className="min-w-0 space-y-3">
            <div className="text-[10px] font-black text-indigo-500 uppercase tracking-widest">{t('management.phaseOrchestrationEyebrow')}</div>
            <div className="text-2xl font-black text-gray-900">{t('management.phaseOrchestrationTitle')}</div>
            <div className="max-w-3xl text-sm text-gray-500 leading-relaxed">{t('management.phaseOrchestrationDescription')}</div>
          </div>
          <div className="flex w-full flex-wrap items-center gap-3 lg:w-auto lg:flex-nowrap lg:justify-end lg:self-start">
            <button
              type="button"
              onClick={() => void loadPhaseOrchestration()}
              disabled={loading || saving}
              className="inline-flex min-w-[104px] items-center justify-center gap-2 rounded-xl border border-gray-200 bg-white px-4 py-2.5 text-xs font-black uppercase text-gray-700 hover:border-indigo-200 hover:text-indigo-600 disabled:opacity-50 transition-all"
            >
              {loading ? <LucideLoader size={14} className="animate-spin" /> : <RefreshCw size={14} />}
              {t('common.refresh')}
            </button>
            <button
              type="button"
              onClick={() => void loadDependencyValidation()}
              disabled={loading || saving || validatingDependencies}
              className="inline-flex min-w-[148px] items-center justify-center gap-2 rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-2.5 text-xs font-black uppercase text-emerald-700 hover:bg-emerald-100 disabled:opacity-50 transition-all"
            >
              {validatingDependencies ? <LucideLoader size={14} className="animate-spin" /> : <CheckCircle2 size={14} />}
              {t('management.validateDependencies')}
            </button>
            <button
              type="button"
              onClick={handleSave}
              disabled={!hasChanges || loading || saving}
              className="inline-flex min-w-[136px] items-center justify-center gap-2 rounded-xl border border-indigo-200 bg-indigo-50 px-4 py-2.5 text-xs font-black uppercase text-indigo-700 hover:bg-indigo-100 disabled:opacity-50 transition-all"
            >
              {saving ? <LucideLoader size={14} className="animate-spin" /> : <Save size={14} />}
              {t('management.phaseOrchestrationSave')}
            </button>
          </div>
        </div>

        {message ? (
          <div className={`mt-5 rounded-xl border px-4 py-3 text-sm flex items-center justify-between ${message.type === 'success' ? 'border-emerald-200 bg-emerald-50 text-emerald-700' : 'border-rose-200 bg-rose-50 text-rose-700'}`}>
            <span>{message.text}</span>
            <button type="button" onClick={() => setMessage(null)} className="text-xs font-black uppercase opacity-70 hover:opacity-100">
              {t('common.dismiss')}
            </button>
          </div>
        ) : null}

        {payload?.validation_errors?.length ? (
          <div className="mt-5 rounded-2xl border border-amber-200 bg-amber-50 p-4">
            <div className="flex items-center gap-2 text-amber-700 text-xs font-black uppercase tracking-widest">
              <AlertTriangle size={14} />
              {t('management.phaseOrchestrationConfigIssues')}
            </div>
            <div className="mt-3 space-y-2">
              {payload.validation_errors.map((item) => (
                <div key={item} className="rounded-xl bg-white/70 px-3 py-2 text-sm text-amber-900">{item}</div>
              ))}
            </div>
          </div>
        ) : null}

        {validationReport ? (
          <div className="mt-5 rounded-2xl border border-gray-200 bg-gray-50/70 p-4 sm:p-5">
            <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
              <div>
                <div className="text-sm font-black text-gray-900">
                  {t('management.validationSummaryTitle')}
                </div>
                <div className="mt-1 max-w-4xl text-sm leading-relaxed text-gray-500">
                  {t('management.validationSummaryDescription')}
                </div>
              </div>
              <div className={`inline-flex items-center gap-2 rounded-full px-4 py-2 text-xs font-black uppercase ${
                validationReport.ok ? 'bg-emerald-100 text-emerald-700' : 'bg-amber-100 text-amber-700'
              }`}>
                {validationReport.ok ? <CheckCircle2 size={14} /> : <AlertTriangle size={14} />}
                {validationReport.ok ? t('management.validationHealthy') : t('management.validationAttention')}
              </div>
            </div>

            <div className="mt-5 grid grid-cols-2 gap-3 xl:grid-cols-4">
              <div className="rounded-2xl border border-gray-200 bg-white px-4 py-4">
                <div className="text-[10px] font-black uppercase tracking-widest text-gray-400">{t('management.validationExperts')}</div>
                <div className="mt-2 text-2xl font-black text-gray-900">{validationReport.expert_count}</div>
              </div>
              <div className="rounded-2xl border border-gray-200 bg-white px-4 py-4">
                <div className="text-[10px] font-black uppercase tracking-widest text-gray-400">{t('management.validationDependenciesCount')}</div>
                <div className="mt-2 text-2xl font-black text-gray-900">{validationReport.dependency_edges}</div>
              </div>
              <div className="rounded-2xl border border-rose-100 bg-rose-50 px-4 py-4">
                <div className="text-[10px] font-black uppercase tracking-widest text-rose-400">{t('management.validationErrors')}</div>
                <div className="mt-2 text-2xl font-black text-rose-700">{validationReport.summary.errors}</div>
              </div>
              <div className="rounded-2xl border border-amber-100 bg-amber-50 px-4 py-4">
                <div className="text-[10px] font-black uppercase tracking-widest text-amber-500">{t('management.validationWarnings')}</div>
                <div className="mt-2 text-2xl font-black text-amber-700">{validationReport.summary.warnings}</div>
              </div>
            </div>

            {optimizationSuggestions.length > 0 ? (
              <div className="mt-5 rounded-2xl border border-indigo-100 bg-indigo-50/70 p-4">
                <div className="text-[10px] font-black uppercase tracking-widest text-indigo-500">
                  {t('management.optimizationSuggestions')}
                </div>
                <div className="mt-3 grid grid-cols-1 gap-3 xl:grid-cols-2">
                  {optimizationSuggestions.map((suggestion) => (
                    <div key={suggestion} className="rounded-xl border border-indigo-100 bg-white/90 px-4 py-3 text-sm leading-relaxed text-gray-700">
                      {suggestion}
                    </div>
                  ))}
                </div>
              </div>
            ) : null}

            <div className="mt-5">
              {validationReport.findings.length === 0 ? (
                <div className="rounded-2xl border border-emerald-100 bg-emerald-50 px-5 py-8 text-sm text-emerald-700 flex items-center justify-center gap-2">
                  <CheckCircle2 size={16} />
                  {t('management.validationNoFindings')}
                </div>
              ) : (
                <div className="space-y-3">
                  {validationReport.findings.map((finding, index) => {
                    const severityClasses = finding.severity === 'error'
                      ? 'border-rose-200 bg-rose-50 text-rose-700'
                      : finding.severity === 'warning'
                        ? 'border-amber-200 bg-amber-50 text-amber-700'
                        : 'border-sky-200 bg-sky-50 text-sky-700';
                    const recommendation = t(getDependencyRecommendationKey(finding.code));
                    return (
                      <div key={`${finding.code}-${finding.expert_id ?? 'global'}-${finding.related_expert_id ?? 'none'}-${index}`} className={`rounded-2xl border p-4 ${severityClasses}`}>
                        <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
                          <div className="min-w-0 space-y-3">
                            <div className="flex flex-wrap items-center gap-2">
                              <span className="inline-flex items-center gap-1 rounded-full bg-white/80 px-2.5 py-1 text-[10px] font-black uppercase tracking-widest">
                                {finding.severity}
                              </span>
                              <span className="inline-flex items-center rounded-full bg-white/80 px-2.5 py-1 text-[10px] font-black uppercase tracking-widest">
                                {finding.code}
                              </span>
                              {finding.expert_id ? (
                                <span className="inline-flex items-center rounded-full bg-white/80 px-2.5 py-1 text-[10px] font-black tracking-widest">
                                  {t('management.validationSource')}: {finding.expert_id}
                                </span>
                              ) : null}
                              {finding.related_expert_id ? (
                                <span className="inline-flex items-center rounded-full bg-white/80 px-2.5 py-1 text-[10px] font-black tracking-widest">
                                  {t('management.validationTarget')}: {finding.related_expert_id}
                                </span>
                              ) : null}
                            </div>
                            <div className="text-sm font-semibold leading-relaxed">{finding.message}</div>
                            <div className="rounded-xl border border-white/60 bg-white/70 px-4 py-3 text-sm leading-relaxed text-gray-700">
                              <span className="font-black">{t('management.recommendationLabel')}</span>
                              {recommendation}
                            </div>
                          </div>
                          {Object.keys(finding.details ?? {}).length > 0 ? (
                            <pre className="max-w-xl overflow-x-auto rounded-xl bg-white/80 p-3 text-[11px] text-gray-700">
                              {JSON.stringify(finding.details, null, 2)}
                            </pre>
                          ) : null}
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          </div>
        ) : null}
      </section>

      <section className="bg-white rounded-2xl border border-gray-200 shadow-sm p-6">
        <div className="text-[10px] font-black text-gray-400 uppercase tracking-widest">{t('management.phaseOrchestrationSequence')}</div>
        <div ref={phaseSequenceRef} className="mt-4 overflow-x-auto">
          <div className="min-w-max flex items-stretch gap-4 pb-2">
            {orderedPhases.map((phase, index) => {
              const configurable = isConfigurablePhase(phase);
              const active = configurable && selectedPhaseId === phase.id;
              const cardWidthClass = configurable
                ? `w-[${CONFIGURABLE_PHASE_CARD_WIDTH}px]`
                : `w-[${FIXED_PHASE_CARD_WIDTH}px]`;
              const cardClasses = active
                ? 'border-indigo-500 bg-indigo-600 text-white shadow-lg shadow-indigo-100'
                : configurable
                  ? 'border-gray-200 bg-white text-gray-800 hover:border-indigo-300 hover:bg-indigo-50'
                  : 'border-gray-200 bg-gray-50 text-gray-500';

              return (
                <div key={phase.id} className="flex items-center gap-4">
                  <button
                    type="button"
                    disabled={!configurable}
                    onClick={() => configurable && setSelectedPhaseId(phase.id)}
                    ref={(node) => {
                      phaseButtonRefs.current[phase.id] = node;
                    }}
                    className={`${cardWidthClass} rounded-2xl border p-4 text-left transition-all disabled:cursor-default ${cardClasses}`}
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div>
                        <div className={`text-[10px] font-black uppercase tracking-widest ${active ? 'text-indigo-100' : 'text-gray-400'}`}>
                          {t('management.phaseOrchestrationOrder', { order: phase.order })}
                        </div>
                        <div className="mt-2 text-base font-black">{phase.label}</div>
                        <div className={`mt-1 text-[11px] ${active ? 'text-indigo-100' : 'text-gray-400'}`}>{phase.id}</div>
                      </div>
                      <span className={`rounded-full px-2.5 py-1 text-[10px] font-black uppercase tracking-widest ${
                        active
                          ? 'bg-white/15 text-white'
                          : configurable
                            ? 'bg-indigo-50 text-indigo-700'
                            : 'bg-gray-200 text-gray-600'
                      }`}>
                        {configurable ? t('management.phaseOrchestrationConfigurable') : t('management.phaseOrchestrationFixed')}
                      </span>
                    </div>
                    {configurable ? (
                      <div className={`mt-3 text-[11px] ${active ? 'text-indigo-100' : 'text-gray-400'}`}>
                        {t('management.phaseOrchestrationExpertCount', { count: phase.experts.length })}
                      </div>
                    ) : null}
                  </button>
                  {index < orderedPhases.length - 1 ? (
                    <div className="flex items-center justify-center text-gray-300 text-lg font-black">→</div>
                  ) : null}
                </div>
              );
            })}
          </div>
        </div>
      </section>

      <section className="grid grid-cols-1 xl:grid-cols-12 gap-6">
        <div className="xl:col-span-8 bg-white rounded-2xl border border-gray-200 shadow-sm p-6">
          {selectedPhase ? (
            <>
              <div className="flex flex-col lg:flex-row lg:items-start lg:justify-between gap-6">
                <div>
                  <div className="text-[10px] font-black text-indigo-500 uppercase tracking-widest">{t('management.phaseOrchestrationDetailEyebrow')}</div>
                  <div className="text-2xl font-black text-gray-900 mt-1">{selectedPhase.label}</div>
                  <div className="text-xs text-gray-400 mt-2">
                    {selectedPhase.id}
                    {' · '}
                    {t('management.phaseOrchestrationOrder', { order: selectedPhase.order })}
                    {' · '}
                    {t('management.phaseOrchestrationConfigurable')}
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={() => moveSelectedPhase('left')}
                    disabled={configurablePhaseIds.indexOf(selectedPhase.id) <= 0}
                    className="inline-flex items-center gap-2 rounded-xl border border-gray-200 bg-white px-3 py-2 text-xs font-black uppercase text-gray-700 hover:border-indigo-200 hover:text-indigo-600 disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    <ChevronLeft size={14} />
                    {t('management.phaseOrchestrationMoveLeft')}
                  </button>
                  <button
                    type="button"
                    onClick={() => moveSelectedPhase('right')}
                    disabled={configurablePhaseIds.indexOf(selectedPhase.id) === -1 || configurablePhaseIds.indexOf(selectedPhase.id) >= configurablePhaseIds.length - 1}
                    className="inline-flex items-center gap-2 rounded-xl border border-gray-200 bg-white px-3 py-2 text-xs font-black uppercase text-gray-700 hover:border-indigo-200 hover:text-indigo-600 disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    {t('management.phaseOrchestrationMoveRight')}
                    <ChevronRight size={14} />
                  </button>
                </div>
              </div>

              <div className="mt-6">
                <div className="text-[10px] font-black text-gray-400 uppercase tracking-widest mb-3">{t('management.phaseOrchestrationConfiguredExperts')}</div>
                <div className="flex flex-wrap gap-3">
                  {selectedPhase.experts.length === 0 ? (
                    <div className="rounded-xl border border-dashed border-gray-200 bg-gray-50 px-4 py-3 text-sm text-gray-400">
                      {t('management.phaseOrchestrationEmptyPhase')}
                    </div>
                  ) : (
                    selectedPhase.experts.map((expertId) => {
                      const expert = expertMap[expertId];
                      return (
                        <div key={expertId} className="group rounded-2xl border border-gray-200 bg-gray-50 px-4 py-3 pr-11 relative">
                          <div className="text-xs font-black text-gray-900">{expert ? expertDisplayName(expert) : expertId}</div>
                          <div className="text-[11px] text-gray-400 mt-1">{expertId}</div>
                          <button
                            type="button"
                            onClick={() => removeExpertFromPhase(expertId, selectedPhase.id)}
                            className="absolute top-2 right-2 rounded-lg bg-white p-1.5 text-gray-400 opacity-0 transition-all hover:bg-rose-50 hover:text-rose-500 group-hover:opacity-100"
                            title={t('common.delete')}
                          >
                            <X size={12} />
                          </button>
                        </div>
                      );
                    })
                  )}
                </div>
              </div>

              <div className="mt-6">
                <div className="text-[10px] font-black text-gray-400 uppercase tracking-widest mb-2">{t('management.phaseOrchestrationAddExpert')}</div>
                <select
                  className="w-full rounded-xl border border-gray-200 bg-white px-3 py-3 text-sm text-gray-700 outline-none focus:border-indigo-400"
                  value=""
                  onChange={(event) => {
                    const expertId = event.target.value;
                    if (!expertId) {
                      return;
                    }
                    moveExpertToPhase(expertId, selectedPhase.id);
                  }}
                >
                  <option value="">{t('management.phaseOrchestrationSelectExpert')}</option>
                  {(payload?.experts || []).some((expert) => assignedPhaseByExpert[expert.id] === selectedPhase.id) ? (
                    <optgroup label={t('management.phaseOrchestrationSelectedExperts')}>
                      {(payload?.experts || [])
                        .filter((expert) => assignedPhaseByExpert[expert.id] === selectedPhase.id)
                        .map((expert) => (
                          <option key={expert.id} value={expert.id}>
                            {expertDisplayName(expert)} ({expert.id}) · {t('management.phaseOrchestrationAlreadyInPhase')}
                          </option>
                        ))}
                    </optgroup>
                  ) : null}
                  <optgroup label={t('management.phaseOrchestrationAvailableExperts')}>
                    {(payload?.experts || [])
                      .filter((expert) => !assignedPhaseByExpert[expert.id])
                      .map((expert) => (
                      <option key={expert.id} value={expert.id}>
                        {expertDisplayName(expert)} ({expert.id})
                      </option>
                      ))}
                  </optgroup>
                </select>
              </div>
            </>
          ) : (
            <div className="rounded-2xl border border-dashed border-gray-200 bg-gray-50 px-6 py-10 text-center text-sm text-gray-400">
              {t('management.phaseOrchestrationNoEditablePhase')}
            </div>
          )}
        </div>

        <div className="xl:col-span-4 bg-white rounded-2xl border border-gray-200 shadow-sm p-6">
          <div className="text-[10px] font-black text-gray-400 uppercase tracking-widest">{t('management.phaseOrchestrationUnassigned')}</div>
          <div className="mt-4 flex flex-col gap-3">
            {loading ? (
              <div className="inline-flex items-center gap-2 rounded-xl border border-dashed border-gray-200 px-4 py-3 text-sm text-gray-400">
                <LucideLoader size={14} className="animate-spin" />
                {t('common.loading')}
              </div>
            ) : unassignedExperts.length === 0 ? (
              <div className="rounded-xl border border-dashed border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-700 inline-flex items-center gap-2">
                <CheckCircle2 size={14} />
                {t('management.phaseOrchestrationNoUnassigned')}
              </div>
            ) : (
              unassignedExperts.map((expert) => (
                <div key={expert.id} className="rounded-2xl border border-gray-200 bg-gray-50 px-4 py-3">
                  <div className="text-xs font-black text-gray-900">{expertDisplayName(expert)}</div>
                  <div className="text-[11px] text-gray-400 mt-1">{expert.id}</div>
                </div>
              ))
            )}
          </div>
        </div>
      </section>
    </div>
  );
}
