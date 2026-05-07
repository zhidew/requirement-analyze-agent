import axios from 'axios';

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000/api/v1';

export const apiClient = axios.create({
  baseURL: API_BASE_URL,
});

export interface ModelConfig {
  id: string;
  name: string;
  provider: string;
  model_name: string;
  api_key?: string;
  base_url?: string;
  headers?: Record<string, string> | null;
  is_default: boolean;
  has_api_key?: boolean;
  has_headers?: boolean;
  description?: string;
}

export interface LlmConfig {
  llm_provider: string;
  openai_api_key?: string;
  openai_base_url?: string;
  openai_model_name?: string;
  has_openai_api_key?: boolean;
}

export interface DebugConfig {
  project_id?: string;
  llm_interaction_logging_enabled: boolean;
  llm_full_payload_logging_enabled: boolean;
}

export interface InteractionEventRecord {
  event_id: string;
  interaction_id: string;
  event_type: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface InteractionRecord {
  interaction_id: string;
  project_id: string;
  version_id: string;
  run_id?: string | null;
  scope: string;
  owner_node: string;
  owner_expert_id?: string | null;
  status: string;
  turn_index: number;
  parent_interaction_id?: string | null;
  question_text: string;
  question_schema: Record<string, unknown>;
  context: Record<string, unknown>;
  answer: Record<string, unknown>;
  summary: string;
  knowledge_refs: string[];
  affected_artifacts: string[];
  created_at: string;
  updated_at: string;
  completed_at?: string | null;
  events: InteractionEventRecord[];
}

export interface ClarifiedRequirementsPayload {
  summary: string;
  clarified_requirements_markdown: string;
  requirements: Record<string, unknown>;
  clarification_log: Array<Record<string, unknown>>;
  decision_log?: Array<Record<string, unknown>>;
}

export interface ReflectionReport {
  report_id: string;
  artifact_id: string;
  expert_id: string;
  status: string;
  confidence: number;
  checks: Record<string, { status: string; message: string }>;
  issues: Array<Record<string, unknown>>;
  blocks_downstream: boolean;
  created_at: string;
}

export interface SystemConsistencyReport {
  report_id: string;
  artifact_id: string;
  project_id: string;
  version_id: string;
  status: string;
  checks: Array<{
    check_id?: string;
    status?: string;
    message?: string;
    evidence_refs?: Array<Record<string, unknown>>;
  }>;
  conflict_ids: string[];
  suggested_actions: string[];
  created_at: string;
  conflicts?: Array<ContextConflict>;
}

export interface ContextConflict {
  conflict_id: string;
  report_id?: string | null;
  project_id: string;
  version_id: string;
  artifact_id?: string | null;
  conflict_type: string;
  semantic: string;
  severity: string;
  status: string;
  summary: string;
  evidence_refs: Array<Record<string, unknown>>;
  suggested_actions: string[];
  decision_id?: string | null;
  created_at: string;
  updated_at: string;
}

export interface SectionReview {
  section_review_id: string;
  artifact_id: string;
  anchor_id?: string | null;
  status: string;
  reviewer_note: string;
  revision_session_id?: string | null;
  created_at: string;
  updated_at: string;
}

export interface DesignArtifact {
  artifact_id: string;
  project_id: string;
  version_id: string;
  run_id?: string | null;
  expert_id: string;
  artifact_type: string;
  artifact_version: number;
  parent_artifact_id?: string | null;
  status: string;
  title: string;
  file_name: string;
  file_path: string;
  content_hash: string;
  summary: string;
  reflection?: ReflectionReport | null;
  consistency?: SystemConsistencyReport | null;
  decision_logs?: DecisionLog[];
  impact_records?: ImpactRecord[];
  incoming_impacts?: ImpactRecord[];
  section_reviews?: SectionReview[];
}

export interface ArtifactAnchor {
  anchor_id: string;
  artifact_id: string;
  file_name: string;
  anchor_type: string;
  label: string;
  text_excerpt: string;
  start_offset: number;
  end_offset: number;
  content_hash: string;
  created_at: string;
}

export interface RevisionPatch {
  patch_id: string;
  revision_session_id: string;
  artifact_id: string;
  anchor_id: string;
  patch_status: string;
  preserve_policy: string;
  diff: {
    original_text?: string;
    replacement_text?: string;
    unified_diff?: string[];
  };
  rationale: string;
  predicted_impact: Record<string, unknown>;
  apply_result?: Record<string, unknown>;
  post_apply_validation: Record<string, unknown>;
  created_artifact_id?: string | null;
}

export interface RevisionReplacementSuggestion {
  project_id: string;
  version_id: string;
  revision_session_id: string;
  artifact_id: string;
  anchor_id: string;
  original_text: string;
  replacement_text: string;
  rationale: string;
  has_changes: boolean;
  session: RevisionSession;
}

export interface RevisionSession {
  revision_session_id: string;
  project_id: string;
  version_id: string;
  target_artifact_id: string;
  target_expert_id: string;
  status: string;
  user_feedback: string;
  normalized_revision_request: {
    revision_type?: string;
    revision_reason?: string;
    as_is_or_to_be?: string;
    semantic?: string;
    candidate_conflicts?: string[];
    decision_required?: boolean;
    [key: string]: unknown;
  };
  events: Array<Record<string, unknown>>;
  patches: RevisionPatch[];
}

export interface DecisionLog {
  decision_id: string;
  project_id: string;
  version_id: string;
  scope: string;
  conflict_ids: string[];
  decision: string;
  basis: string;
  authority: string;
  applies_to: string[];
  evidence_refs: Array<Record<string, unknown>>;
  created_by?: string | null;
  created_at: string;
}

export interface ImpactRecord {
  impact_id: string;
  project_id: string;
  version_id: string;
  source_artifact_id: string;
  impacted_artifact_id: string;
  impact_status: string;
  trigger_type: string;
  trigger_ref_id?: string | null;
  reason: string;
  evidence: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export const api = {
  getProjects: () => apiClient.get('/projects').then(res => res.data),
  createProject: (name: string, description?: string) => 
    apiClient.post('/projects', { name, description }).then(res => res.data),
  deleteProject: (projectId: string) =>
    apiClient.delete(`/projects/${projectId}`).then(res => res.data),
  getProjectAssetsSummary: (projectId: string) =>
    apiClient.get(`/projects/${projectId}/assets-summary`).then(res => res.data),
  getProjectVersions: (projectId: string, page: number = 1, pageSize: number = 10) => 
    apiClient.get(`/projects/${projectId}/versions`, { params: { page, page_size: pageSize } }).then(res => res.data),
  deleteProjectVersion: (projectId: string, version: string) =>
    apiClient.delete(`/projects/${projectId}/versions/${version}`).then(res => res.data),
  runOrchestrator: (projectId: string, version: string, requirementText: string, model?: string) =>
   apiClient.post(`/projects/${projectId}/versions/${version}/run`, { requirement_text: requirementText, model }).then(res => res.data),
  scheduleOrchestrator: (projectId: string, version: string, requirementText: string, scheduledFor: string, model?: string) =>
    apiClient.post(`/projects/${projectId}/versions/${version}/schedule-run`, {
      requirement_text: requirementText,
      scheduled_for: scheduledFor,
      model,
    }).then(res => res.data),

  uploadBaselineFiles: (projectId: string, version: string, files: File[]) => {
    const formData = new FormData();
    files.forEach(file => formData.append('files', file));
    return apiClient.post(`/projects/${projectId}/versions/${version}/upload`, formData, {
      headers: { 'Content-Type': 'multipart/form-data' }
    }).then(res => res.data);
  },
  getProjectArtifacts: (projectId: string, version: string) => 
    apiClient.get(`/projects/${projectId}/versions/${version}/artifacts`).then(res => res.data),
  listDesignArtifacts: (projectId: string, version: string, expertId?: string) =>
    apiClient.get(`/projects/${projectId}/versions/${version}/design-artifacts`, { params: { expert_id: expertId } }).then(res => res.data as { items: DesignArtifact[] }),
  acceptDesignArtifact: (projectId: string, version: string, artifactId: string, payload?: {
    reviewer_note?: string;
    accepted_by?: string;
  }) => apiClient.post(`/projects/${projectId}/versions/${version}/artifacts/${artifactId}/accept`, payload || {}).then(res => res.data as DesignArtifact),
  createManualArtifactRevision: (projectId: string, version: string, artifactId: string, payload: {
    content: string;
    reviewer_note?: string;
    edited_by?: string;
  }) => apiClient.post(`/projects/${projectId}/versions/${version}/artifacts/${artifactId}/manual-revision`, payload).then(res => res.data as RevisionPatch),
  markSectionReview: (projectId: string, version: string, artifactId: string, payload: {
    status: string;
    anchor_id?: string | null;
    reviewer_note?: string;
    revision_session_id?: string | null;
  }) => apiClient.post(`/projects/${projectId}/versions/${version}/artifacts/${artifactId}/section-reviews`, payload).then(res => res.data as SectionReview),
  listDecisionLogs: (projectId: string, version: string) =>
    apiClient.get(`/projects/${projectId}/versions/${version}/decision-logs`).then(res => res.data as { items: DecisionLog[] }),
  listImpactRecords: (projectId: string, version: string, params?: {
    source_artifact_id?: string;
    impacted_artifact_id?: string;
    impact_status?: string;
  }) => apiClient.get(`/projects/${projectId}/versions/${version}/impact-records`, { params }).then(res => res.data as { items: ImpactRecord[] }),
  createRevisionSession: (projectId: string, version: string, artifactId: string, userFeedback?: string) =>
    apiClient.post(`/projects/${projectId}/versions/${version}/artifacts/${artifactId}/revision-sessions`, { user_feedback: userFeedback || '' }).then(res => res.data as RevisionSession),
  addRevisionMessage: (projectId: string, version: string, sessionId: string, content: string, role = 'user') =>
    apiClient.post(`/projects/${projectId}/versions/${version}/revision-sessions/${sessionId}/messages`, { role, content }).then(res => res.data as RevisionSession),
  finalizeRevisionSession: (projectId: string, version: string, sessionId: string) =>
    apiClient.post(`/projects/${projectId}/versions/${version}/revision-sessions/${sessionId}/finalize`).then(res => res.data as RevisionSession),
  suggestRevisionReplacement: (projectId: string, version: string, sessionId: string, payload: {
    artifact_id: string;
    anchor_id: string;
    user_feedback?: string;
  }) => apiClient.post(`/projects/${projectId}/versions/${version}/revision-sessions/${sessionId}/replacement-suggestion`, payload).then(res => res.data as RevisionReplacementSuggestion),
  createArtifactAnchor: (projectId: string, version: string, artifactId: string, payload: {
    file_name: string;
    anchor_type?: string;
    label?: string;
    text_excerpt: string;
    start_offset?: number;
    end_offset?: number;
  }) => apiClient.post(`/projects/${projectId}/versions/${version}/artifacts/${artifactId}/anchors`, payload).then(res => res.data as ArtifactAnchor),
  createRevisionPatchPreview: (projectId: string, version: string, sessionId: string, payload: {
    artifact_id: string;
    anchor_id: string;
    replacement_text: string;
    rationale?: string;
    preserve_policy?: string;
  }) => apiClient.post(`/projects/${projectId}/versions/${version}/revision-sessions/${sessionId}/patch-preview`, payload).then(res => res.data as RevisionPatch),
  applyRevisionPatch: (projectId: string, version: string, patchId: string) =>
    apiClient.post(`/projects/${projectId}/versions/${version}/revision-patches/${patchId}/apply`).then(res => res.data as RevisionPatch),
  createConflictDecision: (projectId: string, version: string, conflictId: string, payload: {
    decision: string;
    basis: string;
    authority: string;
    created_by?: string;
  }) => apiClient.post(`/projects/${projectId}/versions/${version}/conflicts/${conflictId}/decisions`, payload).then(res => res.data as DecisionLog),
  updateImpactRecordStatus: (projectId: string, version: string, impactId: string, status: string) =>
    apiClient.post(`/projects/${projectId}/versions/${version}/impact-records/${impactId}/status`, { status }).then(res => res.data as ImpactRecord),
  getProjectState: (projectId: string, version: string) => 
    apiClient.get(`/projects/${projectId}/versions/${version}/state`).then(res => res.data),
  getCurrentInteraction: (projectId: string, version: string) =>
    apiClient.get(`/projects/${projectId}/versions/${version}/interactions/current`).then(res => res.data as InteractionRecord | null),
  listInteractions: (projectId: string, version: string) =>
    apiClient.get(`/projects/${projectId}/versions/${version}/interactions`).then(res => res.data as { items: InteractionRecord[] }),
  getClarifiedRequirements: (projectId: string, version: string) =>
    apiClient.get(`/projects/${projectId}/versions/${version}/clarified-requirements`).then(res => res.data as ClarifiedRequirementsPayload),
  submitInteractionResponse: (
    projectId: string,
    version: string,
    interactionId: string,
    payload: {
      action?: 'approve' | 'revise' | 'answer';
      response: Record<string, unknown>;
    },
  ) =>
    apiClient.post(`/projects/${projectId}/versions/${version}/interactions/${interactionId}/response`, payload).then(res => res.data),
  resumeWorkflow: (
    projectId: string,
    version: string,
    humanInput: {
      action: 'approve' | 'revise' | 'answer';
      node_id?: string;
      interrupt_id?: string;
      interaction_id?: string;
      selected_option?: string;
      selected_options?: string[];
      selected_experts?: string[];
      answer?: string;
      feedback?: string;
      response?: Record<string, unknown>;
    },
  ) => 
    apiClient.post(`/projects/${projectId}/versions/${version}/resume`, humanInput).then(res => res.data),
  retryWorkflowNode: (projectId: string, version: string, nodeType: string, model?: string) =>
    apiClient.post(`/projects/${projectId}/versions/${version}/retry-node`, { node_type: nodeType, model }).then(res => res.data),
  continueWorkflow: (projectId: string, version: string, model?: string) =>
    apiClient.post(`/projects/${projectId}/versions/${version}/continue`, { model }).then(res => res.data),
  cancelWorkflow: (projectId: string, version: string, reason?: string) =>
    apiClient.post(`/projects/${projectId}/versions/${version}/cancel`, { reason }).then(res => res.data),
  getVersionLogs: (projectId: string, version: string) => 
    apiClient.get(`/projects/${projectId}/versions/${version}/logs`).then(res => res.data),
  getJobStatusSseUrl: (jobId: string) => `${API_BASE_URL}/jobs/${jobId}/status`,
  getRepositoryConfigs: (projectId: string) =>
    apiClient.get(`/projects/${projectId}/config/repositories`).then(res => res.data),
  saveRepositoryConfig: (projectId: string, payload: {
    id: string;
    name: string;
    url: string;
    branch?: string;
    username?: string;
    token?: string;
    local_path?: string;
    description?: string;
    type?: string;
  }) => apiClient.post(`/projects/${projectId}/config/repositories`, payload).then(res => res.data),
  deleteRepositoryConfig: (projectId: string, repoId: string) =>
    apiClient.delete(`/projects/${projectId}/config/repositories/${repoId}`).then(res => res.data),
  getDatabaseConfigs: (projectId: string) =>
    apiClient.get(`/projects/${projectId}/config/databases`).then(res => res.data),
  saveDatabaseConfig: (projectId: string, payload: {
    id: string;
    name: string;
    type: string;
    host: string;
    port: number;
    database: string;
    username?: string;
    password?: string;
    schema_filter?: string[];
    description?: string;
  }) => apiClient.post(`/projects/${projectId}/config/databases`, payload).then(res => res.data),
  deleteDatabaseConfig: (projectId: string, dbId: string) =>
    apiClient.delete(`/projects/${projectId}/config/databases/${dbId}`).then(res => res.data),
  getKnowledgeBaseConfigs: (projectId: string) =>
    apiClient.get(`/projects/${projectId}/config/knowledge-bases`).then(res => res.data),
  saveKnowledgeBaseConfig: (projectId: string, payload: {
    id: string;
    name: string;
    type: string;
    path?: string;
    index_url?: string;
    includes?: string[];
    description?: string;
  }) => apiClient.post(`/projects/${projectId}/config/knowledge-bases`, payload).then(res => res.data),
  deleteKnowledgeBaseConfig: (projectId: string, kbId: string) =>
    apiClient.delete(`/projects/${projectId}/config/knowledge-bases/${kbId}`).then(res => res.data),
  getExpertConfigs: (projectId: string) =>
    apiClient.get(`/projects/${projectId}/config/experts`).then(res => res.data),
  getExpertPhaseOrchestration: () =>
    apiClient.get('/expert-center/phase-orchestration').then(res => res.data),
  saveExpertConfig: (projectId: string, payload: {
    id: string;
    name: string;
    name_zh?: string | null;
    name_en?: string | null;
    enabled: boolean;
    description?: string;
  }) => apiClient.post(`/projects/${projectId}/config/experts`, payload).then(res => res.data),
  getProjectLlmConfig: (projectId: string) =>
    apiClient.get(`/projects/${projectId}/config/llm`).then(res => res.data),
  getProjectDebugConfig: (projectId: string) =>
    apiClient.get(`/projects/${projectId}/config/debug`).then(res => res.data),
  saveProjectLlmConfig: (projectId: string, payload: {
    llm_provider: string;
    openai_api_key?: string;
    openai_base_url?: string;
    openai_model_name?: string;
  }) => apiClient.post(`/projects/${projectId}/config/llm`, payload).then(res => res.data),
  saveProjectDebugConfig: (projectId: string, payload: DebugConfig) =>
    apiClient.post(`/projects/${projectId}/config/debug`, payload).then(res => res.data),
  getProjectModels: (projectId: string) =>
    apiClient.get(`/projects/${projectId}/config/models`).then(res => res.data),
  saveProjectModel: (projectId: string, payload: {
    id: string;
    name: string;
    provider: string;
    model_name: string;
    api_key?: string;
    base_url?: string;
    headers?: Record<string, string>;
    is_default: boolean;
    description?: string;
  }) => apiClient.post(`/projects/${projectId}/config/models`, payload).then(res => res.data),
  deleteProjectModel: (projectId: string, modelId: string) =>
    apiClient.delete(`/projects/${projectId}/config/models/${modelId}`).then(res => res.data),
  testProjectModel: (projectId: string, payload: {
    id: string;
    name: string;
    provider: string;
    model_name: string;
    api_key?: string;
    base_url?: string;
    headers?: Record<string, string>;
    is_default: boolean;
    description?: string;
  }) => apiClient.post(`/projects/${projectId}/config/llm/test`, payload).then(res => res.data),
  testProjectRepository: (projectId: string, payload: {
    id: string;
    name: string;
    type?: string;
    url: string;
    branch?: string;
    username?: string;
    token?: string;
    local_path?: string;
    description?: string;
  }) => apiClient.post(`/projects/${projectId}/config/repositories/test`, payload).then(res => res.data),
  testProjectDatabase: (projectId: string, payload: {
    id: string;
    name: string;
    type: string;
    host: string;
    port: number;
    database: string;
    username?: string;
    password?: string;
    schema_filter?: string[];
    description?: string;
  }) => apiClient.post(`/projects/${projectId}/config/databases/test`, payload).then(res => res.data),
  testProjectKnowledgeBase: (projectId: string, payload: {
    id: string;
    name: string;
    type: string;
    path?: string;
    index_url?: string;
    includes?: string[];
    description?: string;
  }) => apiClient.post(`/projects/${projectId}/config/knowledge-bases/test`, payload).then(res => res.data),
  getSystemLlmDefaults: () =>
    apiClient.get('/system/llm-config').then(res => res.data),
};
