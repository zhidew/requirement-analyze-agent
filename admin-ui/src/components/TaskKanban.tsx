import React, { memo, useMemo, useState, useEffect, useRef, useCallback } from 'react';
import {
  Activity,
  AlertTriangle,
  CheckCircle,
  Circle,
  Loader as LucideLoader,
  MinusCircle,
  Sparkles,
  XCircle,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { apiClient } from '../api';

export type NodeStatus = 'todo' | 'running' | 'waiting_human' | 'success' | 'failed' | 'skipped' | 'idle';

export interface Task {
  id: string;
  agent_type: string;
  status: NodeStatus;
  priority?: number;
  phase?: string;
}

interface TaskKanbanProps {
  tasks: Task[];
  nodeStatuses: Record<string, NodeStatus>;
  nodeLlmMap?: Record<string, { label?: string | null }>;
  selectedNode: string | null;
  onSelectNode: (nodeId: string) => void;
  t: (key: string) => string;
  currentPhase?: string;
  selectedPipeline?: string[]; // Pipeline from planner reasoning
  isInitializing?: boolean; // True when workflow just started
  showPlannedStages?: boolean;
}

interface PhaseOrchestrationPayload {
  phases: Array<{ id: string; label: string; agents?: string[]; experts?: string[] }>;
  experts: Array<{ id: string; name: string; name_zh?: string | null; name_en?: string | null }>;
}

/** Minimum width per pipeline column (px) */
const COLUMN_MIN_WIDTH = 140;

const TaskKanbanComponent: React.FC<TaskKanbanProps> = ({
  tasks,
  nodeStatuses,
  nodeLlmMap,
  selectedNode,
  onSelectNode,
  t,
  currentPhase,
  selectedPipeline,
  isInitializing,
  showPlannedStages = false,
}) => {
  const { i18n } = useTranslation();
  // ---------- Dynamic phase stages from backend API ----------
  const [phaseLabels, setPhaseLabels] = useState<Record<string, string>>({});
  const [allStages, setAllStages] = useState<Array<{ id: string; agents: string[] }>>([]);
  const [expertNames, setExpertNames] = useState<Record<string, { name: string; name_zh?: string | null; name_en?: string | null }>>({});

  useEffect(() => {
    apiClient
      .get<PhaseOrchestrationPayload>('/expert-center/phase-orchestration')
      .then((res) => {
        const labelMap: Record<string, string> = {};
        const stages: Array<{ id: string; agents: string[] }> = [];
        const names: Record<string, { name: string; name_zh?: string | null; name_en?: string | null }> = {};
        for (const expert of res.data.experts || []) {
          names[expert.id] = {
            name: expert.name,
            name_zh: expert.name_zh,
            name_en: expert.name_en,
          };
        }
        for (const p of res.data.phases || []) {
          const rawAgents = Array.isArray(p.agents) ? p.agents : p.experts;
          const agents = Array.isArray(rawAgents) ? [...rawAgents] : [];
          if (p.id === 'PLANNING' && !agents.includes('planner')) {
            agents.unshift('planner');
          }
          if (agents.length === 0) {
            continue;
          }
          labelMap[p.id] = p.label;
          stages.push({ id: p.id, agents });
        }
        setPhaseLabels(labelMap);
        setAllStages(stages);
        setExpertNames(names);
      })
      .catch(() => {});
  }, []);

  /** Resolve phase display label: prefer API-fetched label, fallback to i18n key */
  const getPhaseLabel = useCallback(
    (phaseId: string) => phaseLabels[phaseId] || phaseId,
    [phaseLabels],
  );

  const getNodeLabel = useCallback(
    (nodeId: string) => {
      if (nodeId === 'planner') {
        return t('projectDetail.planner') || 'Planner';
      }
      const expert = expertNames[nodeId];
      const isZh = i18n.language.toLowerCase().startsWith('zh');
      if (!expert) {
        return nodeId;
      }
      if (isZh) {
        return expert.name_zh || expert.name_en || expert.name || nodeId;
      }
      return expert.name_en || expert.name || expert.name_zh || nodeId;
    },
    [expertNames, i18n.language, t],
  );

  // ---------- Horizontal drag-scroll refs & state ----------
  const scrollRef = useRef<HTMLDivElement>(null);
  const isDragging = useRef(false);
  const dragStartX = useRef(0);
  const scrollStartLeft = useRef(0);
  const [isDragActive, setIsDragActive] = useState(false);

  const handleDragStart = useCallback((e: React.MouseEvent) => {
    // Only start drag on the scroll container itself, not on interactive children
    if ((e.target as HTMLElement).closest('button')) return;
    isDragging.current = true;
    dragStartX.current = e.clientX;
    scrollStartLeft.current = scrollRef.current?.scrollLeft || 0;
    setIsDragActive(true);
  }, []);

  const handleDragMove = useCallback((e: React.MouseEvent) => {
    if (!isDragging.current) return;
    e.preventDefault();
    const dx = e.clientX - dragStartX.current;
    if (scrollRef.current) {
      scrollRef.current.scrollLeft = scrollStartLeft.current - dx;
    }
  }, []);

  const handleDragEnd = useCallback(() => {
    isDragging.current = false;
    setIsDragActive(false);
  }, []);

  // ---------- Auto-scroll to active phase ----------
  const activePhaseRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!currentPhase || !scrollRef.current) return;
    // Delay slightly to let React finish rendering
    const timer = setTimeout(() => {
      activePhaseRef.current?.scrollIntoView({
        behavior: 'smooth',
        inline: 'center',
        block: 'nearest',
      });
    }, 200);
    return () => clearTimeout(timer);
  }, [currentPhase]);

  // ---------- Pipeline logic (unchanged) ----------
  const hasTaskBackedPipeline = tasks.some((task) => task.agent_type !== 'planner');
  const hasConfirmedPipeline = (selectedPipeline?.length || 0) > 0 || hasTaskBackedPipeline;

  // Check if we're in initialization mode (tasks empty but workflow running)
  const showInitMode = !!(isInitializing && tasks.length === 0);

  // Check if we're in "Blueprint Mode" (Cold Start)
  const isBlueprintMode = !hasConfirmedPipeline && tasks.length === 0;

  // Check if we're in planning phase (only planner is active)
  const isInPlanningPhase = useMemo(() => {
    if (isBlueprintMode) return false;
    if (!hasConfirmedPipeline && !hasTaskBackedPipeline) {
      return true;
    }
    if (showInitMode) return true;

    const plannerTask = tasks.find((t) => t.agent_type === 'planner');
    const nonPlannerTasks = tasks.filter((t) => t.agent_type !== 'planner');

    // Case 1: Only planner in queue, and it's running or waiting for human
    if (tasks.length === 1 && plannerTask) {
      return plannerTask.status === 'running' || plannerTask.status === 'waiting_human';
    }

    // Case 2: Planner is active (running/waiting_human) and all other agents are still todo (not started)
    if (plannerTask && nonPlannerTasks.length > 0) {
      const plannerIsActive = plannerTask.status === 'running' || plannerTask.status === 'waiting_human';
      const allOthersAreTodo = nonPlannerTasks.every((t) => t.status === 'todo');
      return plannerIsActive && allOthersAreTodo;
    }

    return false;
  }, [hasConfirmedPipeline, hasTaskBackedPipeline, showInitMode, tasks, isBlueprintMode]);

  const stageIdByAgent = useMemo(() => {
    const map: Record<string, string> = { planner: 'PLANNING' };
    for (const stage of allStages) {
      for (const agentId of stage.agents) {
        map[agentId] = stage.id;
      }
    }
    return map;
  }, [allStages]);

  const taskAgentsByStage = useMemo(() => {
    const map: Record<string, string[]> = {};
    for (const task of tasks) {
      const phaseId = (task.phase || '').toUpperCase();
      const stageId = task.agent_type === 'planner'
        ? 'PLANNING'
        : phaseId || stageIdByAgent[task.agent_type];
      if (!stageId) {
        continue;
      }
      const agents = map[stageId] || [];
      if (!agents.includes(task.agent_type)) {
        agents.push(task.agent_type);
      }
      map[stageId] = agents;
    }
    return map;
  }, [tasks, stageIdByAgent]);

  const selectedPipelineStageIds = useMemo(() => {
    if (!selectedPipeline?.length) {
      return new Set<string>();
    }
    const pipelineAgentIds = new Set(selectedPipeline);
    return new Set(
      allStages
        .filter((stage) => stage.agents.some((agentId) => pipelineAgentIds.has(agentId)))
        .map((stage) => stage.id),
    );
  }, [allStages, selectedPipeline]);

  // Derive stages dynamically based on active tasks and their phases
  const activeStages = useMemo(() => {
    if (isBlueprintMode) {
      return allStages;
    }
    if (isInPlanningPhase) {
      return allStages.filter((stage) => stage.id === 'PLANNING');
    }

    return allStages.filter((stage) => (taskAgentsByStage[stage.id] || []).length > 0);
  }, [isInPlanningPhase, isBlueprintMode, allStages, taskAgentsByStage]);

  // Get pending stages (from selectedPipeline but not yet in tasks)
  // Only show pending stages when NOT in analysis phase
  const pendingStages = useMemo(() => {
    // Never show pending stages during analysis or blueprint phase
    if (isInPlanningPhase || isBlueprintMode) return [];
    if (!showPlannedStages || !selectedPipeline || tasks.length === 0) return [];
    
    const activeStageIds = new Set(activeStages.map((stage) => stage.id));
    
    return allStages.filter(
      (stage) => !activeStageIds.has(stage.id) && selectedPipelineStageIds.has(stage.id)
    );
  }, [isInPlanningPhase, showPlannedStages, selectedPipeline, tasks, activeStages, isBlueprintMode, allStages, selectedPipelineStageIds]);

  /** Total visible columns for width calculation */
  const totalColumns = isInPlanningPhase
    ? 2
    : Math.max(activeStages.length + (showInitMode ? 1 : pendingStages.length), 1);
  const needsScroll = !isInPlanningPhase && totalColumns > 4;

  const renderNode = (nodeId: string, label: string, _isActive: boolean, isLoading: boolean = false) => {
    const status = isLoading ? 'running' : (nodeStatuses[nodeId] || 'idle');
    const isSelected = selectedNode === nodeId;

    let icon = <Circle size={10} className="text-gray-300" />;
    let borderColor = 'border-gray-100';
    let bgColor = 'bg-white';
    let textColor = 'text-gray-400';
    let animation = '';

    if (status === 'running' || isLoading) {
      icon = <LucideLoader size={10} className="text-indigo-500 animate-spin" />;
      borderColor = 'border-indigo-400 shadow-[0_0_12px_rgba(99,102,241,0.2)]';
      bgColor = 'bg-indigo-50';
      textColor = 'text-indigo-900 font-bold';
      animation = 'animate-pulse';
    } else if (status === 'success') {
      icon = <CheckCircle size={10} className="text-emerald-500" />;
      borderColor = 'border-emerald-200';
      bgColor = 'bg-emerald-50/30';
      textColor = 'text-emerald-900 font-semibold';
    } else if (status === 'failed') {
      icon = <XCircle size={10} className="text-rose-500" />;
      borderColor = 'border-rose-200';
      bgColor = 'bg-rose-50/20';
      textColor = 'text-rose-900 font-semibold';
    } else if (status === 'waiting_human') {
      icon = <AlertTriangle size={10} className="text-amber-500" />;
      borderColor = 'border-amber-300';
      bgColor = 'bg-amber-50/40';
      textColor = 'text-amber-900 font-semibold';
    } else if (status === 'skipped') {
      icon = <MinusCircle size={10} className="text-gray-400" />;
      borderColor = 'border-gray-200';
      bgColor = 'bg-gray-50';
      textColor = 'text-gray-500 font-semibold';
    }

    if (isSelected) {
      borderColor = 'border-indigo-600 ring-4 ring-indigo-50';
    }

    return (
      <button
        key={nodeId}
        onClick={() => !isLoading && onSelectNode(nodeId)}
        disabled={isLoading}
        className={`relative flex items-start gap-2 w-full p-2.5 rounded-xl border ${borderColor} ${bgColor} ${textColor} ${animation} transition-all duration-300 hover:translate-y-[-1px] text-[9px] uppercase tracking-tighter font-black shadow-sm group ${isLoading ? 'cursor-wait' : ''}`}
      >
        <span className="flex-shrink-0">{icon}</span>
        <span className="flex-1 min-w-0 text-left">
          <span className="block truncate">{label}</span>
        </span>
      </button>
    );
  };

  // Render initialization placeholder with breathing animation
  const renderInitPlaceholder = () => (
    <div className="flex-shrink-0 flex flex-col items-center gap-3 transition-all duration-500" style={{ width: COLUMN_MIN_WIDTH * 2 }}>
      <div className="relative flex h-7 w-7 items-center justify-center">
        <div className="absolute inset-0 rounded-full bg-indigo-200 animate-ping opacity-75" />
        <div className="absolute inset-1 rounded-full bg-indigo-100 animate-pulse" />
        <Sparkles size={14} className="relative text-indigo-500 animate-pulse" />
      </div>
      <span className="text-[9px] font-black uppercase tracking-tight text-center leading-tight text-indigo-400 animate-pulse">
        {t('pipeline.initializing') || 'Initializing...'}
      </span>
    </div>
  );

  // Render pending stage with dashed style
  const renderPendingStage = (stage: { id: string; agents: string[] }, idx: number) => (
    <div key={`pending-${stage.id}`} className="flex-shrink-0 flex flex-col items-center gap-3 transition-all duration-500 opacity-50" style={{ width: COLUMN_MIN_WIDTH }}>
      <div className="flex h-7 w-7 items-center justify-center rounded-full border-2 border-dashed border-gray-300">
        <span className="text-[10px] font-black text-gray-400">{idx + 1}</span>
      </div>
      <span className="text-[9px] font-black uppercase tracking-tight text-center leading-tight text-gray-400">
        {getPhaseLabel(stage.id)}
      </span>
    </div>
  );

  // Render analysis phase waiting placeholder
  const renderAnalysisWaitingPlaceholder = () => (
    <div className="flex-shrink-0 flex flex-col items-center gap-3 transition-all duration-500" style={{ width: COLUMN_MIN_WIDTH * 2 }}>
      <div className="relative flex h-7 w-7 items-center justify-center">
        <div className="absolute inset-0 rounded-full bg-gray-200 animate-ping opacity-30" />
        <div className="absolute inset-1 rounded-full bg-gray-100 animate-pulse" />
        <LucideLoader size={14} className="relative text-gray-400 animate-spin" />
      </div>
      <span className="text-[9px] font-black uppercase tracking-tight text-center leading-tight text-gray-400 animate-pulse">
        {t('pipeline.waiting') || 'Waiting...'}
      </span>
    </div>
  );

  const selectedNodeStatus = selectedNode ? (nodeStatuses[selectedNode] || 'idle') : 'idle';
  let selectedNodeLabelColor = 'text-gray-400';
  if (selectedNodeStatus === 'running') {
    selectedNodeLabelColor = 'text-indigo-600';
  } else if (selectedNodeStatus === 'success') {
    selectedNodeLabelColor = 'text-emerald-600';
  } else if (selectedNodeStatus === 'failed') {
    selectedNodeLabelColor = 'text-rose-600';
  } else if (selectedNodeStatus === 'waiting_human') {
    selectedNodeLabelColor = 'text-amber-600';
  } else if (selectedNodeStatus === 'skipped') {
    selectedNodeLabelColor = 'text-gray-500';
  }

  return (
    <div className="relative w-full pb-8">
      {/* Horizontally scrollable pipeline container */}
      <div
        ref={scrollRef}
        className={`overflow-x-auto ${isDragActive ? 'cursor-grabbing select-none' : 'cursor-grab'}`}
        onMouseDown={handleDragStart}
        onMouseMove={handleDragMove}
        onMouseUp={handleDragEnd}
        onMouseLeave={handleDragEnd}
      >
        <div className="min-w-max px-4">
          {/* ===== Timeline row ===== */}
          <div className="relative py-2">
            {/* Connecting line spanning full content width */}
            {!isInPlanningPhase && (
              <div className="absolute top-1/2 left-0 right-0 h-[1px] bg-gray-100 -translate-y-[12px] z-0" />
            )}

            <div className="relative z-10 flex items-start gap-4">
              {activeStages.map((stage, idx) => {
                const isActive = currentPhase === stage.id || (showInitMode && stage.id === 'PLANNING');
                const isAutoScrollTarget = isActive && needsScroll;
                const stageAgentsInQueue = taskAgentsByStage[stage.id] || [];
                const statuses = stageAgentsInQueue.map((agentId) => nodeStatuses[agentId] || 'idle');
                const isAllSuccess = statuses.length > 0 && statuses.every((status) => status === 'success');
                
                // Special handling for PLANNING stage to prevent premature success checkmark
                // while the planner might still be finalizing its state.
                const isPlanningStageReallyDone = stage.id === 'PLANNING' 
                  ? (isAllSuccess && currentPhase !== 'PLANNING')
                  : isAllSuccess;

                const hasFailed = statuses.some((status) => status === 'failed');
                const hasWaitingHuman = statuses.some((status) => status === 'waiting_human');
                const hasSuccess = statuses.some((status) => status === 'success');
                const hasRunning = statuses.some((status) => status === 'running');

                let circleColor = 'bg-white border-gray-200 text-gray-300';
                let textColor = 'text-gray-400';
                let icon = <span className="text-[10px] font-black">{idx + 1}</span>;

                if (showInitMode && stage.id === 'PLANNING') {
                  circleColor = 'bg-indigo-500 border-indigo-500 text-white shadow-lg shadow-indigo-200';
                  textColor = 'text-indigo-600';
                  icon = <Activity size={14} className="animate-pulse" />;
                } else if (isBlueprintMode) {
                  circleColor = 'bg-gray-50 border-dashed border-gray-300 text-gray-300';
                  textColor = 'text-gray-400 opacity-60';
                  icon = <Circle size={10} className="opacity-40" />;
                } else if (isPlanningStageReallyDone) {
                  circleColor = 'bg-emerald-500 border-emerald-500 text-white';
                  textColor = 'text-emerald-600';
                  icon = <CheckCircle size={14} />;
                } else if (hasFailed && hasSuccess) {
                  circleColor = 'bg-amber-500 border-amber-500 text-white';
                  textColor = 'text-amber-600';
                  icon = <AlertTriangle size={14} />;
                } else if (hasWaitingHuman) {
                  circleColor = 'bg-amber-400 border-amber-400 text-white';
                  textColor = 'text-amber-600';
                  icon = <AlertTriangle size={14} />;
                } else if (hasFailed) {
                  circleColor = 'bg-rose-500 border-rose-500 text-white';
                  textColor = 'text-rose-600';
                  icon = <XCircle size={14} />;
                } else if (isActive || hasRunning) {
                  circleColor = 'bg-white border-indigo-600 text-indigo-600 shadow-md scale-110';
                  textColor = 'text-indigo-600';
                  icon = <Activity size={14} className="animate-pulse" />;
                }

                return (
                  <div
                    key={stage.id}
                    ref={isAutoScrollTarget ? activePhaseRef : undefined}
                    className="flex-shrink-0 flex flex-col items-center gap-3 transition-all duration-500"
                    style={{ width: COLUMN_MIN_WIDTH }}
                  >
                    <div className={`flex h-7 w-7 items-center justify-center rounded-full border-2 transition-all duration-500 ${circleColor}`}>
                      {icon}
                    </div>
                    <span className={`text-[9px] font-black uppercase tracking-tight text-center leading-tight transition-colors break-words ${textColor}`}>
                      {getPhaseLabel(stage.id)}
                    </span>
                  </div>
                );
              })}

              {isInPlanningPhase && renderAnalysisWaitingPlaceholder()}
              {!isInPlanningPhase && showInitMode && renderInitPlaceholder()}
              {!isInPlanningPhase && pendingStages.map((stage, idx) => renderPendingStage(stage, activeStages.length + idx))}
            </div>
          </div>

          {/* ===== Cards row ===== */}
          <div className="flex items-start gap-4 mt-6">
            {activeStages.map((stage) => {
              if (isBlueprintMode) {
                return (
                  <div
                    key={stage.id}
                    className="flex-shrink-0 flex flex-col gap-2 p-2.5 rounded-2xl border border-dashed border-gray-100 bg-gray-50/10 min-h-[110px] opacity-40 transition-all duration-700"
                    style={{ width: COLUMN_MIN_WIDTH }}
                  >
                    <div className="flex flex-col gap-1.5">
                      {stage.agents.map((agentId) => (
                        <div
                          key={agentId}
                          className="flex items-center gap-2 w-full p-2.5 rounded-xl border border-dashed border-gray-100 bg-white/40 text-gray-300 text-[9px] uppercase tracking-tighter font-black"
                        >
                          <Circle size={10} className="opacity-30" />
                          <span className="truncate">{getNodeLabel(agentId)}</span>
                        </div>
                      ))}
                    </div>
                  </div>
                );
              }

              const isActive = currentPhase === stage.id || (showInitMode && stage.id === 'PLANNING');
              const stageAgentsInQueue = taskAgentsByStage[stage.id] || [];

              return (
                <div
                  key={stage.id}
                  className={`flex-shrink-0 flex flex-col gap-2 p-2.5 rounded-2xl border transition-all duration-500 min-h-[110px] ${isActive
                    ? 'bg-white border-indigo-100 shadow-xl shadow-indigo-50/50 ring-1 ring-indigo-50'
                    : 'bg-white/60 border-gray-100 shadow-sm opacity-90'
                    }`}
                  style={{ width: COLUMN_MIN_WIDTH }}
                >
                    <div className="flex flex-col gap-1.5">
                      {stageAgentsInQueue.map((agentId) => {
                        const isLoading = agentId === 'planner' && (showInitMode || !hasConfirmedPipeline);
                        return renderNode(agentId, getNodeLabel(agentId), isActive, isLoading);
                      })}
                    </div>
                  </div>
              );
            })}

            {isInPlanningPhase && !isBlueprintMode && (
              <div className="flex-shrink-0 flex flex-col gap-2 p-2.5 rounded-2xl border border-dashed border-gray-200 bg-gray-50/30 min-h-[110px]" style={{ width: COLUMN_MIN_WIDTH * 2 }}>
                <div className="flex flex-1 items-center justify-center">
                  <div className="flex flex-col items-center gap-3">
                    <div className="relative">
                      <div className="absolute inset-0 rounded-full bg-gray-200 animate-ping opacity-30" />
                      <div className="absolute inset-0 rounded-full bg-gray-100 animate-pulse" />
                      <LucideLoader size={20} className="relative text-gray-400 animate-spin" />
                    </div>
                    <span className="text-[10px] font-bold uppercase tracking-tight text-gray-500 animate-pulse">
                      {t('pipeline.expertsWaiting') || '设计专家正在等待加载...'}
                    </span>
                  </div>
                </div>
              </div>
            )}

            {isBlueprintMode && (
              <div className="hidden" />
            )}

            {!isInPlanningPhase && !isBlueprintMode && showInitMode && (
              <div className="flex-shrink-0 flex flex-col gap-2 p-2.5 rounded-2xl border border-dashed border-indigo-200 bg-indigo-50/30 min-h-[110px]" style={{ width: COLUMN_MIN_WIDTH }}>
                <div className="flex flex-1 items-center justify-center">
                  <div className="flex flex-col items-center gap-2">
                    <div className="relative">
                      <div className="absolute inset-0 rounded-full bg-indigo-200 animate-ping opacity-50" />
                      <LucideLoader size={16} className="relative text-indigo-500 animate-spin" />
                    </div>
                    <span className="text-[9px] font-bold uppercase tracking-tight text-indigo-500 animate-pulse">
                      {t('pipeline.preparing') || 'Preparing pipeline...'}
                    </span>
                  </div>
                </div>
              </div>
            )}

            {!isInPlanningPhase && pendingStages.map((stage) => (
              <div
                key={`pending-${stage.id}`}
                className="flex-shrink-0 flex flex-col gap-2 p-2.5 rounded-2xl border border-dashed border-gray-200 bg-gray-50/30 min-h-[110px] opacity-50"
                style={{ width: COLUMN_MIN_WIDTH }}
              >
                <div className="flex min-h-[68px] items-center justify-center rounded-xl border border-dashed border-gray-200 bg-gray-50/60 px-2 text-center text-[9px] font-bold uppercase tracking-tight text-gray-300">
                  {getPhaseLabel(stage.id)}
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>

      {selectedNode && nodeLlmMap?.[selectedNode]?.label && (
        <div
          className={`pointer-events-none absolute bottom-0 right-4 inline-flex max-w-[320px] items-center gap-2 px-3 py-1.5 text-[10px] font-black tracking-widest ${selectedNodeLabelColor}`}
        >
          {nodeLlmMap[selectedNode]?.label}
        </div>
      )}
    </div>
  );
};

function buildTasksSignature(tasks: Task[]): string {
  return tasks.map((task) => `${task.agent_type}:${task.status}:${task.priority}:${task.phase || ''}`).join('|');
}

function buildNodeStatusesSignature(nodeStatuses: Record<string, NodeStatus>): string {
  return Object.entries(nodeStatuses)
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([key, value]) => `${key}:${value}`)
    .join('|');
}

function buildNodeLlmSignature(nodeLlmMap?: Record<string, { label?: string | null }>): string {
  return Object.entries(nodeLlmMap || {})
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([key, value]) => `${key}:${value?.label || ''}`)
    .join('|');
}

export const TaskKanban = memo(TaskKanbanComponent, (prev, next) => (
  prev.selectedNode === next.selectedNode &&
  prev.currentPhase === next.currentPhase &&
  prev.t === next.t &&
  prev.selectedPipeline === next.selectedPipeline &&
  prev.isInitializing === next.isInitializing &&
  prev.showPlannedStages === next.showPlannedStages &&
  buildTasksSignature(prev.tasks) === buildTasksSignature(next.tasks) &&
  buildNodeStatusesSignature(prev.nodeStatuses) === buildNodeStatusesSignature(next.nodeStatuses) &&
  buildNodeLlmSignature(prev.nodeLlmMap) === buildNodeLlmSignature(next.nodeLlmMap)
));
