import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import type { ClarifiedRequirementsPayload, InteractionRecord } from '../api';

interface InterruptOption {
  value: string;
  label: string;
  description?: string;
}

interface PlannerExpertCard {
  id: string;
  name: string;
  phaseLabel: string;
  phaseTitle?: string;
}

interface HumanInteractionPanelProps {
  currentInteraction: InteractionRecord | null;
  questionSchema: Record<string, unknown>;
  interactions: InteractionRecord[];
  clarifiedRequirements: ClarifiedRequirementsPayload | null;
  currentNode: string | null;
  waitingReason: string | null;
  interruptOptions: InterruptOption[];
  selectedInterruptOption: string;
  onSelectedInterruptOptionChange: (value: string) => void;
  reviewFeedback: string;
  onReviewFeedbackChange: (value: string) => void;
  responseDraft: Record<string, unknown>;
  onResponseDraftChange: (nextDraft: Record<string, unknown>) => void;
  isClarificationInterrupt: boolean;
  isPlannerExpertSelectionInterrupt: boolean;
  selectedPlannerExpertCards: PlannerExpertCard[];
  availablePlannerExpertCards: PlannerExpertCard[];
  selectedPlannerExperts: string[];
  onTogglePlannerExpertSelection: (expertId: string) => void;
  resumeActionLoading: 'approve' | 'revise' | 'answer' | null;
  onSubmitAnswer: () => void;
  onApprove: () => void;
  onRevise: () => void;
}

const formatInteractionScope = (scope: string, fallbackNode: string | null) => {
  switch (scope) {
    case 'requirement_clarification':
      return 'Requirement Clarification';
    case 'planner_review':
      return 'Planner Review';
    case 'expert_clarification':
      return 'Expert Clarification';
    case 'expert_review':
      return 'Expert Review';
    default:
      return fallbackNode || 'Interaction';
  }
};

export function HumanInteractionPanel({
  currentInteraction,
  questionSchema,
  interactions,
  clarifiedRequirements,
  currentNode,
  waitingReason,
  interruptOptions,
  selectedInterruptOption,
  onSelectedInterruptOptionChange,
  reviewFeedback,
  onReviewFeedbackChange,
  responseDraft,
  onResponseDraftChange,
  isClarificationInterrupt,
  isPlannerExpertSelectionInterrupt,
  selectedPlannerExpertCards,
  availablePlannerExpertCards,
  selectedPlannerExperts,
  onTogglePlannerExpertSelection,
  resumeActionLoading,
  onSubmitAnswer,
  onApprove,
  onRevise,
}: HumanInteractionPanelProps) {
  const { t } = useTranslation();

  const orderedInteractions = useMemo(
    () => [...interactions].sort((left, right) => {
      const leftTime = Date.parse(left.updated_at || left.created_at || '');
      const rightTime = Date.parse(right.updated_at || right.created_at || '');
      return leftTime - rightTime;
    }),
    [interactions],
  );

  const latestQuestion = currentInteraction?.question_text || waitingReason || '';
  const whyNeeded = typeof currentInteraction?.context?.why_needed === 'string'
    ? currentInteraction.context.why_needed
    : '';
  const schemaType = String(questionSchema.type ?? '').trim().toLowerCase();
  const isSchemaMultiSelect = schemaType === 'multi_select';
  const isSchemaBoolean = schemaType === 'boolean';
  const isSchemaNumber = schemaType === 'number';
  const isSchemaArtifactConfirm = schemaType === 'artifact_confirm';
  const isReviewInterrupt = !isClarificationInterrupt && !isPlannerExpertSelectionInterrupt;
  const isRequirementClarification = currentInteraction?.scope === 'requirement_clarification' || currentNode === 'requirement_clarifier';
  const hasRevisionFeedback = reviewFeedback.trim().length > 0;
  const hasClarificationOptions = isClarificationInterrupt
    && !isPlannerExpertSelectionInterrupt
    && interruptOptions.length > 0;
  const selectedSchemaValues = Array.isArray(responseDraft.selected_values)
    ? responseDraft.selected_values.map((item) => String(item))
    : [];
  const interactionScope = formatInteractionScope(currentInteraction?.scope || '', currentNode);
  const clarificationSummary = clarifiedRequirements?.summary?.trim() || '';
  const clarificationLog = clarifiedRequirements?.clarification_log || [];
  const currentQuestionTitle = isPlannerExpertSelectionInterrupt
    ? t('projectDetail.waitingHuman.expertSelectionCurrentQuestion')
    : t('projectDetail.waitingHuman.currentQuestion');
  const requirementSummaryTitle = isPlannerExpertSelectionInterrupt
    ? t('projectDetail.waitingHuman.requirementContext')
    : t('projectDetail.waitingHuman.confirmedRequirements');
  const requirementSummaryEmpty = isPlannerExpertSelectionInterrupt
    ? t('projectDetail.waitingHuman.noRequirementContext')
    : t('projectDetail.waitingHuman.noConfirmedRequirements');
  const historyTitle = isPlannerExpertSelectionInterrupt
    ? t('projectDetail.waitingHuman.interactionHistory')
    : t('projectDetail.waitingHuman.sessionHistory');
  const historyEmpty = isPlannerExpertSelectionInterrupt
    ? t('projectDetail.waitingHuman.noInteractionHistoryGeneric')
    : t('projectDetail.waitingHuman.noInteractionHistory');
  const freeTextPlaceholder = isPlannerExpertSelectionInterrupt
    ? t('projectDetail.waitingHuman.expertSelectionNotePlaceholder')
    : t('projectDetail.waitingHuman.clarificationPlaceholder');
  const panelTitle = isPlannerExpertSelectionInterrupt
    ? t('projectDetail.waitingHuman.expertSelectionTitle')
    : isReviewInterrupt
      ? t('projectDetail.waitingHuman.reviewTitle')
      : isRequirementClarification
        ? t('projectDetail.waitingHuman.requirementSessionTitle')
        : t('projectDetail.waitingHuman.designSessionTitle');
  const panelDescription = isPlannerExpertSelectionInterrupt
    ? t('projectDetail.waitingHuman.expertSelectionDescription')
    : isReviewInterrupt
      ? t('projectDetail.waitingHuman.reviewDescription')
      : t('projectDetail.waitingHuman.sessionDescription');
  const primaryOptionsLabel = isSchemaMultiSelect
    ? t('projectDetail.waitingHuman.primaryOptionsMulti')
    : t('projectDetail.waitingHuman.primaryOptions');
  const primaryOptionsHint = isSchemaMultiSelect
    ? t('projectDetail.waitingHuman.primaryOptionsMultiHint')
    : t('projectDetail.waitingHuman.primaryOptionsHint');
  const detailsLabel = hasClarificationOptions
    ? t('projectDetail.waitingHuman.optionalDetails')
    : (isClarificationInterrupt || isPlannerExpertSelectionInterrupt)
      ? t('projectDetail.waitingHuman.additionalDetails')
      : t('projectDetail.waitingHuman.revisionFeedbackOnly');

  const handleSchemaMultiSelectToggle = (value: string) => {
    const nextValues = selectedSchemaValues.includes(value)
      ? selectedSchemaValues.filter((item) => item !== value)
      : [...selectedSchemaValues, value];
    onResponseDraftChange({
      ...responseDraft,
      selected_values: nextValues,
    });
  };

  return (
    <section className="rounded-3xl border border-amber-200 bg-[linear-gradient(180deg,rgba(255,251,235,0.95)_0%,rgba(255,255,255,1)_100%)] shadow-sm p-8 space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div className="space-y-2">
          <div className="inline-flex items-center rounded-full bg-amber-100 px-3 py-1 text-[10px] font-black uppercase tracking-[0.2em] text-amber-700">
            {t('projectDetail.retry.waitingHuman')}
          </div>
          <h2 className="text-xl font-black tracking-tight text-amber-950">
            {panelTitle}
          </h2>
          <p className="text-sm font-medium text-amber-900/80">
            {latestQuestion || panelDescription}
          </p>
        </div>
        <div className="flex flex-col items-end gap-2">
          <span className="rounded-full border border-amber-200 bg-white px-3 py-1 text-[10px] font-black uppercase tracking-wider text-amber-700">
            {interactionScope}
          </span>
          {currentNode && (
            <span className="rounded-full border border-slate-200 bg-white px-3 py-1 text-[10px] font-black uppercase tracking-wider text-slate-500">
              {currentNode}
            </span>
          )}
        </div>
      </div>

      {whyNeeded && !isPlannerExpertSelectionInterrupt && (
        <div className="rounded-2xl border border-amber-200 bg-white/80 px-4 py-3 text-sm font-medium text-amber-900">
          <span className="font-black">{t('projectDetail.waitingHuman.whyMatters')}: </span>
          {whyNeeded}
        </div>
      )}

      <div className={isPlannerExpertSelectionInterrupt ? 'grid gap-4' : 'grid gap-4 xl:grid-cols-[1.1fr_0.9fr]'}>
        <section className="rounded-2xl border border-slate-200 bg-white p-5 space-y-4">
          <div className="flex items-center justify-between gap-3">
            <h3 className="text-[10px] font-black uppercase tracking-widest text-slate-500">
              {currentQuestionTitle}
            </h3>
            {currentInteraction?.status && (
              <span className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 text-[10px] font-black uppercase tracking-wider text-slate-500">
                {currentInteraction.status}
              </span>
            )}
          </div>

          {interruptOptions.length > 0 && !isPlannerExpertSelectionInterrupt && !isSchemaMultiSelect && (
            <div className="space-y-2">
              <div className="space-y-1">
                <label className="text-[10px] font-black uppercase tracking-widest text-amber-700">
                  {primaryOptionsLabel}
                </label>
                {isClarificationInterrupt && (
                  <p className="text-xs font-medium text-slate-500">
                    {primaryOptionsHint}
                  </p>
                )}
              </div>
              <div className="grid gap-3">
                {interruptOptions.map((option) => {
                  const isSelected = selectedInterruptOption === option.value;
                  return (
                    <label
                      key={option.value}
                      className={`flex cursor-pointer gap-3 rounded-2xl border px-4 py-3 transition-all ${isSelected
                        ? 'border-amber-400 bg-amber-50/70 shadow-sm'
                        : 'border-slate-200 bg-slate-50/70 hover:border-amber-300'
                      }`}
                    >
                      <input
                        type={isSchemaBoolean || isSchemaArtifactConfirm ? 'radio' : 'radio'}
                        name="interaction-option"
                        value={option.value}
                        checked={isSelected}
                        onChange={(e) => onSelectedInterruptOptionChange(e.target.value)}
                        className="mt-1 h-4 w-4 border-amber-300 text-amber-600 focus:ring-amber-400"
                      />
                      <div className="space-y-1">
                        <div className="text-sm font-black text-slate-900">{option.label}</div>
                        {option.description && (
                          <div className="text-xs font-medium text-slate-600">{option.description}</div>
                        )}
                      </div>
                    </label>
                  );
                })}
              </div>
            </div>
          )}

          {interruptOptions.length > 0 && isSchemaMultiSelect && !isPlannerExpertSelectionInterrupt && (
            <div className="space-y-2">
              <div className="space-y-1">
                <label className="text-[10px] font-black uppercase tracking-widest text-amber-700">
                  {primaryOptionsLabel}
                </label>
                {isClarificationInterrupt && (
                  <p className="text-xs font-medium text-slate-500">
                    {primaryOptionsHint}
                  </p>
                )}
              </div>
              <div className="grid gap-3">
                {interruptOptions.map((option) => {
                  const isSelected = selectedSchemaValues.includes(option.value);
                  return (
                    <label
                      key={option.value}
                      className={`flex cursor-pointer gap-3 rounded-2xl border px-4 py-3 transition-all ${isSelected
                        ? 'border-amber-400 bg-amber-50/70 shadow-sm'
                        : 'border-slate-200 bg-slate-50/70 hover:border-amber-300'
                      }`}
                    >
                      <input
                        type="checkbox"
                        checked={isSelected}
                        onChange={() => handleSchemaMultiSelectToggle(option.value)}
                        className="mt-1 h-4 w-4 border-amber-300 text-amber-600 focus:ring-amber-400"
                      />
                      <div className="space-y-1">
                        <div className="text-sm font-black text-slate-900">{option.label}</div>
                        {option.description && (
                          <div className="text-xs font-medium text-slate-600">{option.description}</div>
                        )}
                      </div>
                    </label>
                  );
                })}
              </div>
            </div>
          )}

          {isPlannerExpertSelectionInterrupt && (
            <div className="grid gap-4 xl:grid-cols-2">
              <section className="rounded-2xl border border-amber-200 bg-amber-50/50 p-4">
                <div className="mb-3 flex items-center justify-between gap-3">
                  <div className="text-[10px] font-black uppercase tracking-widest text-amber-900/80">
                    {t('projectDetail.waitingHuman.selectedExperts')}
                  </div>
                  <span className="rounded-full border border-amber-200 bg-white px-2.5 py-1 text-[10px] font-black text-amber-700">
                    {selectedPlannerExpertCards.length}
                  </span>
                </div>
                {selectedPlannerExpertCards.length > 0 ? (
                  <div className="grid gap-2">
                    {selectedPlannerExpertCards.map((expert) => (
                      <label
                        key={`selected-${expert.id}`}
                        className="group grid min-w-0 cursor-pointer grid-cols-[auto_minmax(0,1fr)] gap-3 rounded-xl border border-amber-200 bg-white/90 px-4 py-3 shadow-sm transition-all hover:-translate-y-0.5 hover:border-amber-300"
                      >
                        <input
                          type="checkbox"
                          checked
                          onChange={() => onTogglePlannerExpertSelection(expert.id)}
                          className="mt-1 h-4 w-4 rounded border-amber-300 text-amber-600 focus:ring-amber-400"
                        />
                        <div className="min-w-0 space-y-2">
                          <div className="flex flex-wrap items-start justify-between gap-x-3 gap-y-1">
                            <div className="min-w-0 flex-1 basis-56 break-words text-sm font-black leading-5 text-slate-900">
                              {expert.name}
                            </div>
                            <span className="inline-flex max-w-full shrink-0 rounded-full border border-amber-200 bg-amber-100/80 px-2 py-0.5 text-[9px] font-black tracking-[0.14em] text-amber-800" title={expert.phaseTitle}>
                              {expert.phaseLabel}
                            </span>
                          </div>
                          <div className="break-all text-[10px] font-semibold uppercase tracking-[0.08em] text-slate-500">
                            {expert.id}
                          </div>
                        </div>
                      </label>
                    ))}
                  </div>
                ) : (
                  <div className="rounded-2xl border border-amber-200/70 bg-white/80 px-4 py-3 text-sm font-medium text-slate-500">
                    {t('projectDetail.waitingHuman.selectedExpertsEmpty')}
                  </div>
                )}
              </section>

              <section className="rounded-2xl border border-slate-200 bg-slate-50/80 p-4">
                <div className="mb-3 flex items-center justify-between gap-3">
                  <div className="text-[10px] font-black uppercase tracking-widest text-slate-500">
                    {t('projectDetail.waitingHuman.availableExperts')}
                  </div>
                  <span className="rounded-full border border-slate-200 bg-white px-2.5 py-1 text-[10px] font-black text-slate-600">
                    {availablePlannerExpertCards.length}
                  </span>
                </div>
                {availablePlannerExpertCards.length > 0 ? (
                  <div className="grid gap-2">
                    {availablePlannerExpertCards.map((expert) => (
                      <label
                        key={`available-${expert.id}`}
                        className="group grid min-w-0 cursor-pointer grid-cols-[auto_minmax(0,1fr)] gap-3 rounded-xl border border-slate-200 bg-white px-4 py-3 shadow-sm transition-all hover:-translate-y-0.5 hover:border-amber-200"
                      >
                        <input
                          type="checkbox"
                          checked={false}
                          onChange={() => onTogglePlannerExpertSelection(expert.id)}
                          className="mt-1 h-4 w-4 rounded border-amber-300 text-amber-600 focus:ring-amber-400"
                        />
                        <div className="min-w-0 space-y-2">
                          <div className="flex flex-wrap items-start justify-between gap-x-3 gap-y-1">
                            <div className="min-w-0 flex-1 basis-56 break-words text-sm font-black leading-5 text-slate-900">
                              {expert.name}
                            </div>
                            <span className="inline-flex max-w-full shrink-0 rounded-full border border-slate-200 bg-slate-100 px-2 py-0.5 text-[9px] font-black tracking-[0.14em] text-slate-600" title={expert.phaseTitle}>
                              {expert.phaseLabel}
                            </span>
                          </div>
                          <div className="break-all text-[10px] font-semibold uppercase tracking-[0.08em] text-slate-500">
                            {expert.id}
                          </div>
                        </div>
                      </label>
                    ))}
                  </div>
                ) : (
                  <div className="rounded-2xl border border-slate-200 bg-white/80 px-4 py-3 text-sm font-medium text-slate-500">
                    {t('projectDetail.waitingHuman.availableExpertsEmpty')}
                  </div>
                )}
              </section>
            </div>
          )}

          <div className="space-y-2">
            <label className="text-[10px] font-black uppercase tracking-widest text-amber-700">
              {detailsLabel}
            </label>
            {isSchemaNumber ? (
              <input
                type="number"
                value={String(responseDraft.number_value ?? '')}
                onChange={(e) => onResponseDraftChange({ ...responseDraft, number_value: e.target.value })}
                placeholder="0"
                className="w-full rounded-2xl border border-amber-200 bg-white px-4 py-3 text-sm font-medium text-gray-800 focus:outline-none focus:ring-2 focus:ring-amber-400"
              />
            ) : schemaType === 'short_text' ? (
              <input
                type="text"
                value={reviewFeedback}
                onChange={(e) => onReviewFeedbackChange(e.target.value)}
                placeholder={
                  (isClarificationInterrupt || isPlannerExpertSelectionInterrupt)
                    ? freeTextPlaceholder
                    : t('projectDetail.waitingHuman.revisionPlaceholder')
                }
                className="w-full rounded-2xl border border-amber-200 bg-white px-4 py-3 text-sm font-medium text-gray-800 focus:outline-none focus:ring-2 focus:ring-amber-400"
              />
            ) : (
              <textarea
                value={reviewFeedback}
                onChange={(e) => onReviewFeedbackChange(e.target.value)}
                placeholder={
                  (isClarificationInterrupt || isPlannerExpertSelectionInterrupt)
                    ? freeTextPlaceholder
                    : t('projectDetail.waitingHuman.revisionPlaceholder')
                }
                className="w-full min-h-28 rounded-2xl border border-amber-200 bg-white px-4 py-3 text-sm font-medium text-gray-800 focus:outline-none focus:ring-2 focus:ring-amber-400 resize-none"
              />
            )}
          </div>

          {isPlannerExpertSelectionInterrupt && selectedPlannerExperts.length === 0 && (
            <div className="rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm font-medium text-rose-700">
              {t('projectDetail.waitingHuman.noExpertsSelectedWarning')}
            </div>
          )}

          {isReviewInterrupt && hasRevisionFeedback && (
            <div className="rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm font-medium text-amber-800">
              {t('projectDetail.waitingHuman.approveDisabledWithFeedback')}
            </div>
          )}

          <div className="flex flex-col sm:flex-row gap-3">
            {(isClarificationInterrupt || isPlannerExpertSelectionInterrupt) ? (
              <button
                onClick={onSubmitAnswer}
                disabled={
                  resumeActionLoading !== null
                  || (hasClarificationOptions && !isSchemaMultiSelect && !selectedInterruptOption)
                  || (!isPlannerExpertSelectionInterrupt
                    && !hasClarificationOptions
                    && !isSchemaMultiSelect
                    && !isSchemaNumber
                    && !selectedInterruptOption
                    && reviewFeedback.trim().length === 0)
                  || (isSchemaMultiSelect && selectedSchemaValues.length === 0)
                  || (isSchemaNumber && String(responseDraft.number_value ?? '').trim().length === 0 && reviewFeedback.trim().length === 0)
                }
                className="flex-1 rounded-2xl bg-emerald-600 px-5 py-4 text-sm font-black uppercase tracking-widest text-white transition-all hover:bg-emerald-700 disabled:cursor-not-allowed disabled:bg-emerald-300"
              >
                {resumeActionLoading === 'answer'
                  ? t('projectDetail.waitingHuman.submitting')
                  : (isPlannerExpertSelectionInterrupt ? t('projectDetail.waitingHuman.confirmExperts') : t('projectDetail.waitingHuman.submitAnswer'))}
              </button>
            ) : (
              <>
                <button
                  onClick={onApprove}
                  disabled={resumeActionLoading !== null || hasRevisionFeedback}
                  className="flex-1 rounded-2xl bg-emerald-600 px-5 py-4 text-sm font-black uppercase tracking-widest text-white transition-all hover:bg-emerald-700 disabled:cursor-not-allowed disabled:bg-emerald-300"
                >
                  {resumeActionLoading === 'approve' ? t('projectDetail.waitingHuman.approving') : t('projectDetail.waitingHuman.approveContinue')}
                </button>
                <button
                  onClick={onRevise}
                  disabled={resumeActionLoading !== null || reviewFeedback.trim().length === 0}
                  className="flex-1 rounded-2xl bg-amber-600 px-5 py-4 text-sm font-black uppercase tracking-widest text-white transition-all hover:bg-amber-700 disabled:cursor-not-allowed disabled:bg-amber-300"
                >
                  {resumeActionLoading === 'revise' ? t('projectDetail.waitingHuman.submitting') : t('projectDetail.waitingHuman.reviseRetry')}
                </button>
              </>
            )}
          </div>
        </section>

        {!isPlannerExpertSelectionInterrupt && (
          <div className="space-y-4">
            <section className="rounded-2xl border border-slate-200 bg-white p-5 space-y-3">
              <div className="flex items-center justify-between gap-3">
                <h3 className="text-[10px] font-black uppercase tracking-widest text-slate-500">
                  {requirementSummaryTitle}
                </h3>
                <span className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 text-[10px] font-black uppercase tracking-wider text-slate-500">
                  {clarificationLog.length}
                </span>
              </div>
              <p className="whitespace-pre-wrap text-sm font-medium leading-6 text-slate-700">
                {clarificationSummary || requirementSummaryEmpty}
              </p>
            </section>

            <section className="rounded-2xl border border-slate-200 bg-white p-5 space-y-3">
              <div className="flex items-center justify-between gap-3">
                <h3 className="text-[10px] font-black uppercase tracking-widest text-slate-500">
                  {historyTitle}
                </h3>
                <span className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 text-[10px] font-black uppercase tracking-wider text-slate-500">
                  {orderedInteractions.length}
                </span>
              </div>
              {orderedInteractions.length > 0 ? (
                <div className="space-y-3">
                  {orderedInteractions.map((interaction, index) => (
                    <div key={interaction.interaction_id} className="rounded-2xl border border-slate-200 bg-slate-50/80 px-4 py-3">
                      <div className="flex items-center justify-between gap-3">
                        <div className="text-[10px] font-black uppercase tracking-widest text-slate-500">
                          {t('projectDetail.waitingHuman.historyRound', { count: index + 1 })}
                        </div>
                        <span className="rounded-full border border-slate-200 bg-white px-2 py-0.5 text-[10px] font-black uppercase tracking-wider text-slate-500">
                          {formatInteractionScope(interaction.scope, interaction.owner_node)}
                        </span>
                      </div>
                      <div className="mt-2 text-sm font-semibold text-slate-900">
                        {interaction.question_text}
                      </div>
                      <div className="mt-2 text-sm font-medium text-slate-600 whitespace-pre-wrap">
                        {interaction.summary || t('projectDetail.waitingHuman.noResponseYet')}
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="rounded-2xl border border-slate-200 bg-slate-50 px-4 py-3 text-sm font-medium text-slate-500">
                  {historyEmpty}
                </div>
              )}
            </section>
          </div>
        )}
      </div>
    </section>
  );
}
