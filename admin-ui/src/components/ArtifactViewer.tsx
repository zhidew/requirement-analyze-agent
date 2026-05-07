import React, { useMemo, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { FileText, FileJson, Database, MessageSquareText, GitCompare, Check, X, Wand2, ShieldAlert, AlertTriangle, GitBranch, CheckCircle2, PencilLine } from 'lucide-react';
import { CodeBlock } from './CodeBlock'; // Assuming we'll extract CodeBlock too
import { api, type ArtifactAnchor, type DesignArtifact, type RevisionPatch, type RevisionSession } from '../api';
import { canAcceptDesignArtifact, isSystemControlledDesignArtifact } from './artifactGovernanceUi';

interface ArtifactViewerProps {
  projectId: string;
  version: string | null;
  artifacts: Record<string, string>;
  designArtifacts: DesignArtifact[];
  activeExpertId?: string | null;
  selectedFile: string | null;
  onSelectFile: (filename: string) => void;
  filteredArtifacts: string[];
  onArtifactsChanged?: () => void;
  t: (key: string) => string;
}

const getApiErrorMessage = (error: unknown, fallback: string) => {
  if (typeof error === 'object' && error !== null && 'response' in error) {
    const response = (error as { response?: { data?: { detail?: unknown } } }).response;
    if (typeof response?.data?.detail === 'string') {
      return response.data.detail;
    }
  }
  if (error instanceof Error && error.message) {
    return error.message;
  }
  return fallback;
};

interface SourceSelectionRange {
  startOffset: number;
  endOffset: number;
  sourceText: string;
  renderedText: string;
  mode: 'rendered' | 'source';
}

interface MarkdownPosition {
  start?: { offset?: number };
  end?: { offset?: number };
}

interface MarkdownNode {
  position?: MarkdownPosition;
}

type MarkdownComponentProps = React.HTMLAttributes<HTMLElement> & {
  node?: MarkdownNode;
  children?: React.ReactNode;
  className?: string;
};

const sourceAttrsForNode = (node?: MarkdownNode | null) => {
  const start = node?.position?.start?.offset;
  const end = node?.position?.end?.offset;
  return typeof start === 'number' && typeof end === 'number'
    ? { 'data-source-start': start, 'data-source-end': end }
    : {};
};

const readSourceBounds = (element: Element | null) => {
  if (!(element instanceof HTMLElement)) return null;
  const start = Number(element.dataset.sourceStart);
  const end = Number(element.dataset.sourceEnd);
  return Number.isFinite(start) && Number.isFinite(end) ? { start, end } : null;
};

const closestSourceElement = (node: Node | null) => {
  const element = node instanceof Element ? node : node?.parentElement;
  return element?.closest<HTMLElement>('[data-source-start][data-source-end]') || null;
};

const renderedOffsetWithin = (element: HTMLElement, node: Node, offset: number) => {
  const range = document.createRange();
  range.selectNodeContents(element);
  range.setEnd(node, offset);
  return range.toString().length;
};

const normalizeSourceRange = (
  content: string,
  startOffset: number,
  endOffset: number,
  renderedText: string,
  mode: 'rendered' | 'source',
): SourceSelectionRange | null => {
  const start = Math.max(0, Math.min(startOffset, endOffset, content.length));
  const end = Math.max(0, Math.min(Math.max(startOffset, endOffset), content.length));
  const sourceText = content.slice(start, end);
  if (!sourceText.trim()) return null;
  return { startOffset: start, endOffset: end, sourceText, renderedText, mode };
};

export const ArtifactViewer: React.FC<ArtifactViewerProps> = ({
  projectId,
  version,
  artifacts,
  designArtifacts,
  activeExpertId,
  selectedFile,
  onSelectFile,
  filteredArtifacts,
  onArtifactsChanged,
  t
}) => {
  const [selectedExcerpt, setSelectedExcerpt] = useState('');
  const [selectedSourceRange, setSelectedSourceRange] = useState<SourceSelectionRange | null>(null);
  const [discussionScope, setDiscussionScope] = useState<'artifact' | 'selection'>('selection');
  const [artifactViewMode, setArtifactViewMode] = useState<'rendered' | 'source'>('rendered');
  const [isDrawerOpen, setIsDrawerOpen] = useState(false);
  const [feedback, setFeedback] = useState('');
  const [replacementText, setReplacementText] = useState('');
  const [revisionSession, setRevisionSession] = useState<RevisionSession | null>(null);
  const [anchor, setAnchor] = useState<ArtifactAnchor | null>(null);
  const [patchPreview, setPatchPreview] = useState<RevisionPatch | null>(null);
  const [isWorking, setIsWorking] = useState(false);
  const [drawerError, setDrawerError] = useState<string | null>(null);
  const [revisionSuggestionRationale, setRevisionSuggestionRationale] = useState('');
  const [revisionSuggestionHasChanges, setRevisionSuggestionHasChanges] = useState<boolean | null>(null);
  const [isManualRevision, setIsManualRevision] = useState(false);
  const [manualRevisionContent, setManualRevisionContent] = useState('');

  const getFileIcon = (filename: string) => {
    if (filename.endsWith('.sql')) return <Database size={16} className="text-purple-500" />;
    if (filename.endsWith('.yaml') || filename.endsWith('.json')) return <FileJson size={16} className="text-yellow-500" />;
    return <FileText size={16} className="text-blue-500" />;
  };

  const activeDesignArtifact = useMemo(() => {
    if (!selectedFile) return null;
    const candidates = designArtifacts.filter((item) => item.file_name === selectedFile || item.file_path.endsWith(`/${selectedFile}`));
    const scopedCandidates = activeExpertId
      ? candidates.filter((item) => item.expert_id === activeExpertId)
      : candidates;
    const rankedCandidates = scopedCandidates.length > 0 ? scopedCandidates : candidates;
    return rankedCandidates.sort((left, right) => right.artifact_version - left.artifact_version)[0] || null;
  }, [activeExpertId, designArtifacts, selectedFile]);

  const reflection = activeDesignArtifact?.reflection;
  const consistency = activeDesignArtifact?.consistency;
  const consistencyConflicts = consistency?.conflicts || [];
  const decisionLogs = activeDesignArtifact?.decision_logs || [];
  const outgoingImpacts = activeDesignArtifact?.impact_records || [];
  const incomingImpacts = activeDesignArtifact?.incoming_impacts || [];
  const sectionReviews = activeDesignArtifact?.section_reviews || [];
  const isSystemControlledArtifact = isSystemControlledDesignArtifact(selectedFile, activeDesignArtifact);
  const canDiscuss = Boolean(projectId && version && selectedFile && activeDesignArtifact && !isSystemControlledArtifact);
  const normalizedIntent = revisionSession?.normalized_revision_request || {};
  const candidateConflictIds = Array.isArray(normalizedIntent.candidate_conflicts)
    ? (normalizedIntent.candidate_conflicts as string[])
    : [];
  const candidateConflictCount = candidateConflictIds.length;
  const decisionRequired = Boolean(normalizedIntent.decision_required);
  const blockingConflictCount = consistencyConflicts.filter((conflict) => conflict.severity === 'blocking' && conflict.status === 'open').length;
  const warningConflictCount = consistencyConflicts.filter((conflict) => conflict.severity === 'warning' && conflict.status === 'open').length;
  const openImpactCount = [...incomingImpacts, ...outgoingImpacts].filter((impact) => impact.impact_status !== 'no_impact').length;
  const disputedSectionCount = sectionReviews.filter((review) => ['disputed', 'revision_pending', 'blocked_by_conflict'].includes(review.status)).length;
  const overallReviewStatus = blockingConflictCount > 0 || activeDesignArtifact?.status === 'system_check_failed'
    ? 'blocked'
    : activeDesignArtifact?.status === 'accepted' || activeDesignArtifact?.status === 'auto_accepted'
      ? 'accepted'
      : warningConflictCount > 0 || openImpactCount > 0 || disputedSectionCount > 0 || reflection?.status === 'warning'
        ? 'needs_review'
        : 'ready_for_review';

  const governanceTone = overallReviewStatus === 'blocked'
    ? 'rose'
    : overallReviewStatus === 'accepted'
      ? 'emerald'
      : overallReviewStatus === 'needs_review'
        ? 'amber'
        : 'indigo';
  const canAcceptArtifact = canAcceptDesignArtifact(activeDesignArtifact, canDiscuss, overallReviewStatus);
  const governableDesignArtifact = activeDesignArtifact && !isSystemControlledArtifact ? activeDesignArtifact : null;
  const actionableConflicts = consistencyConflicts.filter((conflict) => conflict.status === 'open');
  const actionableImpacts = [...incomingImpacts, ...outgoingImpacts].filter((impact) => impact.impact_status !== 'no_impact');
  const actionableSectionReviews = sectionReviews.filter((review) => ['disputed', 'revision_pending', 'blocked_by_conflict'].includes(review.status));
  const reviewFocusItems = [
    ...actionableConflicts.map((conflict) => ({
      id: `conflict-${conflict.conflict_id}`,
      tone: conflict.severity === 'blocking' ? 'rose' : 'amber',
      label: conflict.severity === 'blocking' ? '阻塞冲突' : '一致性警告',
      title: conflict.summary || conflict.conflict_type || '发现一致性问题',
      detail: [conflict.conflict_type, conflict.semantic, conflict.severity].filter(Boolean).join(' · '),
      action: conflict.status === 'open'
        ? () => handleStartArtifactDiscussion(`处理冲突：${conflict.summary}`)
        : undefined,
    })),
    ...actionableImpacts.map((impact) => ({
      id: `impact-${impact.impact_id}`,
      tone: 'amber',
      label: '下游影响',
      title: impact.reason || '需要确认是否影响相关产物',
      detail: impact.impact_status,
      action: undefined,
    })),
    ...actionableSectionReviews.map((review) => ({
      id: `section-${review.section_review_id}`,
      tone: 'slate',
      label: '局部审阅',
      title: review.reviewer_note || review.anchor_id || '局部内容需要继续确认',
      detail: review.status,
      action: undefined,
    })),
    ...(reflection && reflection.status !== 'passed'
      ? [{
        id: `reflection-${reflection.report_id}`,
        tone: reflection.status === 'blocking' ? 'rose' : 'amber',
        label: '自检结果',
        title: reflection.issues?.[0]?.message?.toString?.() || reflection.issues?.[0]?.summary?.toString?.() || `Reflection ${reflection.status}`,
        detail: `${Math.round((reflection.confidence || 0) * 100)}% confidence`,
        action: undefined,
      }]
      : []),
  ];

  const openDrawer = (scope: 'artifact' | 'selection', excerpt = '', nextFeedback = '', sourceRange: SourceSelectionRange | null = null) => {
    setDiscussionScope(scope);
    setSelectedExcerpt(excerpt);
    setReplacementText(excerpt);
    setSelectedSourceRange(sourceRange);
    setFeedback(nextFeedback);
    setRevisionSession(null);
    setAnchor(null);
    setPatchPreview(null);
    setDrawerError(null);
    setRevisionSuggestionRationale('');
    setRevisionSuggestionHasChanges(null);
    setIsManualRevision(false);
    setManualRevisionContent('');
    setIsDrawerOpen(true);
  };

  const getCurrentSourceContent = () => (selectedFile ? artifacts[selectedFile] || '' : '');
  const currentFileSupportsRenderedOffsets = () => Boolean(selectedFile && /\.(md|markdown)$/i.test(selectedFile));

  const captureSourceSelection = (selection: Selection, content: string): SourceSelectionRange | null => {
    const range = selection.rangeCount > 0 ? selection.getRangeAt(0) : null;
    if (!range) return null;
    const sourceRoot = range.commonAncestorContainer instanceof Element
      ? range.commonAncestorContainer.closest('[data-artifact-source-view="true"]')
      : range.commonAncestorContainer.parentElement?.closest('[data-artifact-source-view="true"]');
    if (!sourceRoot) return null;
    const pre = sourceRoot.querySelector('pre');
    if (!pre) return null;
    const start = renderedOffsetWithin(pre, range.startContainer, range.startOffset);
    const end = renderedOffsetWithin(pre, range.endContainer, range.endOffset);
    return normalizeSourceRange(content, start, end, selection.toString().trim(), 'source');
  };

  const captureRenderedSelection = (selection: Selection, content: string): SourceSelectionRange | null => {
    if (!currentFileSupportsRenderedOffsets()) return null;
    const range = selection.rangeCount > 0 ? selection.getRangeAt(0) : null;
    if (!range) return null;
    const startElement = closestSourceElement(range.startContainer);
    const endElement = closestSourceElement(range.endContainer);
    if (!startElement || !endElement) return null;
    const startBounds = readSourceBounds(startElement);
    const endBounds = readSourceBounds(endElement);
    if (!startBounds || !endBounds) return null;
    return normalizeSourceRange(content, startBounds.start, endBounds.end, selection.toString().trim(), 'rendered');
  };

  const handleCaptureSelection = () => {
    const browserSelection = window.getSelection();
    const selection = browserSelection?.toString().trim() || '';
    if (!selection) {
      setDrawerError('请先选中一段内容。');
      return;
    }
    const sourceContent = getCurrentSourceContent();
    const sourceRange = browserSelection
      ? captureSourceSelection(browserSelection, sourceContent) || captureRenderedSelection(browserSelection, sourceContent)
      : null;
    if (!sourceRange) {
      setDrawerError('当前选区无法稳定映射到源文档。请切换到“源文档”视图后重新划词。');
      return;
    }
    openDrawer('selection', sourceRange.sourceText, '', sourceRange);
  };

  const handleStartArtifactDiscussion = (initialFeedback = '') => {
    openDrawer('artifact', '', initialFeedback);
  };

  const handleStartManualRevision = () => {
    if (!selectedFile) return;
    openDrawer('artifact', '', '', null);
    setIsManualRevision(true);
    setManualRevisionContent(getCurrentSourceContent());
    setFeedback('');
  };

  const ensureSelectionAnchor = async () => {
    if (!version || !selectedFile || !activeDesignArtifact || !selectedExcerpt.trim()) {
      throw new Error('请先选中一段内容。');
    }
    if (!selectedSourceRange) {
      throw new Error('当前选区缺少源文档位置。请切换到“源文档”视图后重新划词。');
    }
    if (anchor) return anchor;
    const createdAnchor = await api.createArtifactAnchor(projectId, version, activeDesignArtifact.artifact_id, {
      file_name: selectedFile,
      anchor_type: selectedExcerpt.includes('\n```') ? 'code_block' : 'text_range',
      text_excerpt: selectedExcerpt,
      start_offset: selectedSourceRange.startOffset,
      end_offset: selectedSourceRange.endOffset,
    });
    setAnchor(createdAnchor);
    return createdAnchor;
  };

  const handleStartRevision = async () => {
    if (!version || !selectedFile || !activeDesignArtifact) return;
    setIsWorking(true);
    setDrawerError(null);
    try {
      const createdAnchor = discussionScope === 'selection' ? await ensureSelectionAnchor() : null;
      const session = await api.createRevisionSession(projectId, version, activeDesignArtifact.artifact_id, feedback);
      const finalized = await api.finalizeRevisionSession(projectId, version, session.revision_session_id);
      setAnchor(createdAnchor);
      if (discussionScope === 'selection' && createdAnchor) {
        const suggestion = await api.suggestRevisionReplacement(projectId, version, finalized.revision_session_id, {
          artifact_id: activeDesignArtifact.artifact_id,
          anchor_id: createdAnchor.anchor_id,
          user_feedback: feedback,
        });
        setRevisionSession(suggestion.session || finalized);
        setReplacementText(suggestion.replacement_text || selectedExcerpt);
        setRevisionSuggestionRationale(suggestion.rationale || '');
        setRevisionSuggestionHasChanges(Boolean(suggestion.has_changes));
        if (!suggestion.has_changes) {
          setDrawerError('LLM 未生成与原选区不同的修订内容，请补充更具体的反馈或直接编辑 Replacement 后再生成预览。');
        }
      } else {
        setRevisionSession(finalized);
      }
    } catch (err: unknown) {
      setDrawerError(getApiErrorMessage(err, '创建修订会话失败。'));
    } finally {
      setIsWorking(false);
    }
  };

  const handleCreatePatchPreview = async () => {
    if (!version || !activeDesignArtifact || !revisionSession || !anchor) return;
    setIsWorking(true);
    setDrawerError(null);
    try {
      const patch = await api.createRevisionPatchPreview(projectId, version, revisionSession.revision_session_id, {
        artifact_id: activeDesignArtifact.artifact_id,
        anchor_id: anchor.anchor_id,
        replacement_text: replacementText,
        rationale: feedback,
        preserve_policy: 'preserve_unselected_content',
      });
      setPatchPreview(patch);
    } catch (err: unknown) {
      setDrawerError(getApiErrorMessage(err, '生成补丁预览失败。'));
    } finally {
      setIsWorking(false);
    }
  };

  const handleApplyPatch = async () => {
    if (!version || !patchPreview) return;
    setIsWorking(true);
    setDrawerError(null);
    try {
      const applied = await api.applyRevisionPatch(projectId, version, patchPreview.patch_id);
      setPatchPreview(applied);
      await onArtifactsChanged?.();
    } catch (err: unknown) {
      setDrawerError(getApiErrorMessage(err, '应用补丁失败。'));
    } finally {
      setIsWorking(false);
    }
  };

  const handleApplyManualRevision = async () => {
    if (!version || !activeDesignArtifact) return;
    setIsWorking(true);
    setDrawerError(null);
    try {
      const patch = await api.createManualArtifactRevision(projectId, version, activeDesignArtifact.artifact_id, {
        content: manualRevisionContent,
        reviewer_note: feedback || 'Manual artifact revision.',
        edited_by: 'user',
      });
      setPatchPreview(patch);
      await onArtifactsChanged?.();
    } catch (err: unknown) {
      setDrawerError(getApiErrorMessage(err, '人工修订失败。'));
    } finally {
      setIsWorking(false);
    }
  };

  const handleResolveConflict = async (conflictId: string, decision: string) => {
    if (!version) return;
    setIsWorking(true);
    setDrawerError(null);
    try {
      await api.createConflictDecision(projectId, version, conflictId, {
        decision,
        basis: 'artifact_viewer_review',
        authority: 'user',
      });
      await onArtifactsChanged?.();
      setRevisionSession((current) => current ? { ...current } : current);
    } catch (err: unknown) {
      setDrawerError(getApiErrorMessage(err, '裁决冲突失败。'));
    } finally {
      setIsWorking(false);
    }
  };

  const handleAcceptArtifact = async () => {
    if (!version || !activeDesignArtifact) return;
    setIsWorking(true);
    setDrawerError(null);
    try {
      await api.acceptDesignArtifact(projectId, version, activeDesignArtifact.artifact_id, {
        reviewer_note: 'Accepted from ArtifactViewer.',
        accepted_by: 'user',
      });
      await onArtifactsChanged?.();
    } catch (err: unknown) {
      setDrawerError(getApiErrorMessage(err, '接受当前版本失败。'));
    } finally {
      setIsWorking(false);
    }
  };

  const handleMarkSectionReview = async (status: string) => {
    if (!version || !activeDesignArtifact) return;
    setIsWorking(true);
    setDrawerError(null);
    try {
      const createdAnchor = discussionScope === 'selection' ? await ensureSelectionAnchor() : null;
      await api.markSectionReview(projectId, version, activeDesignArtifact.artifact_id, {
        status,
        anchor_id: createdAnchor?.anchor_id,
        reviewer_note: feedback || selectedExcerpt.slice(0, 180),
        revision_session_id: revisionSession?.revision_session_id,
      });
      await onArtifactsChanged?.();
      if (status === 'accepted') {
        setIsDrawerOpen(false);
      }
    } catch (err: unknown) {
      setDrawerError(getApiErrorMessage(err, '更新局部审阅状态失败。'));
    } finally {
      setIsWorking(false);
    }
  };

  const handleUpdateImpactStatus = async (impactId: string, status: string) => {
    if (!version) return;
    setIsWorking(true);
    setDrawerError(null);
    try {
      await api.updateImpactRecordStatus(projectId, version, impactId, status);
      await onArtifactsChanged?.();
    } catch (err: unknown) {
      setDrawerError(getApiErrorMessage(err, '更新下游影响状态失败。'));
    } finally {
      setIsWorking(false);
    }
  };

  const markdownComponents = useMemo(() => ({
    h1: ({ node, ...props }: MarkdownComponentProps) => <h1 {...sourceAttrsForNode(node)} {...props} />,
    h2: ({ node, ...props }: MarkdownComponentProps) => <h2 {...sourceAttrsForNode(node)} {...props} />,
    h3: ({ node, ...props }: MarkdownComponentProps) => <h3 {...sourceAttrsForNode(node)} {...props} />,
    h4: ({ node, ...props }: MarkdownComponentProps) => <h4 {...sourceAttrsForNode(node)} {...props} />,
    h5: ({ node, ...props }: MarkdownComponentProps) => <h5 {...sourceAttrsForNode(node)} {...props} />,
    h6: ({ node, ...props }: MarkdownComponentProps) => <h6 {...sourceAttrsForNode(node)} {...props} />,
    p: ({ node, ...props }: MarkdownComponentProps) => <p {...sourceAttrsForNode(node)} {...props} />,
    li: ({ node, ...props }: MarkdownComponentProps) => <li {...sourceAttrsForNode(node)} {...props} />,
    blockquote: ({ node, ...props }: MarkdownComponentProps) => <blockquote {...sourceAttrsForNode(node)} {...props} />,
    table: ({ node, ...props }: MarkdownComponentProps) => <table {...sourceAttrsForNode(node)} {...props} />,
    th: ({ node, ...props }: MarkdownComponentProps) => <th {...sourceAttrsForNode(node)} {...props} />,
    td: ({ node, ...props }: MarkdownComponentProps) => <td {...sourceAttrsForNode(node)} {...props} />,
    a: ({ node, ...props }: MarkdownComponentProps) => <a {...sourceAttrsForNode(node)} {...props} />,
    strong: ({ node, ...props }: MarkdownComponentProps) => <strong {...sourceAttrsForNode(node)} {...props} />,
    em: ({ node, ...props }: MarkdownComponentProps) => <em {...sourceAttrsForNode(node)} {...props} />,
    code: ({ node, ...props }: MarkdownComponentProps) => (
      <CodeBlock {...props} {...sourceAttrsForNode(node)} />
    ),
  }), []);

  const renderContent = () => {
    if (!selectedFile) return null;
    
    const sourceContent = artifacts[selectedFile] || '';
    let content = sourceContent;
    
    // For JSON files, try to pretty print
    if (selectedFile.endsWith('.json')) {
      try {
        const parsed = JSON.parse(content);
        content = '```json\n' + JSON.stringify(parsed, null, 2) + '\n```';
      } catch {
        // Fallback to raw with highlighting if parse fails
        content = '```json\n' + content + '\n```';
      }
    } else if (selectedFile.endsWith('.sql')) {
      content = '```sql\n' + content + '\n```';
    } else if (selectedFile.endsWith('.yaml') || selectedFile.endsWith('.yml')) {
      content = '```yaml\n' + content + '\n```';
    }

    return (
      <div className="flex-1 overflow-auto bg-white rounded-2xl border border-gray-100 shadow-sm p-6 sm:p-10 min-h-[500px] animate-in fade-in zoom-in-95 duration-300">
        {governableDesignArtifact && (
          <div className="mb-6 border-b border-gray-100 pb-5">
            <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
              <div className="min-w-0 flex-1">
                <div className="mb-3 flex flex-wrap items-center gap-2">
                  <span className={`inline-flex items-center gap-1 rounded-full px-3 py-1 text-[10px] font-black uppercase tracking-wider ${
                    governanceTone === 'rose' ? 'bg-rose-50 text-rose-700' : governanceTone === 'emerald' ? 'bg-emerald-50 text-emerald-700' : governanceTone === 'amber' ? 'bg-amber-50 text-amber-700' : 'bg-indigo-50 text-indigo-700'
                  }`}>
                    {overallReviewStatus === 'accepted' ? <CheckCircle2 size={12} /> : overallReviewStatus === 'blocked' ? <AlertTriangle size={12} /> : <ShieldAlert size={12} />}
                    {overallReviewStatus}
                  </span>
                  <span className="rounded-full bg-slate-100 px-3 py-1 text-[10px] font-black uppercase tracking-wider text-slate-600">
                    v{governableDesignArtifact.artifact_version}
                  </span>
                  <span className="rounded-full bg-slate-100 px-3 py-1 text-[10px] font-black uppercase tracking-wider text-slate-600">
                    {governableDesignArtifact.status}
                  </span>
                  {reflection && (
                    <span className={`inline-flex items-center gap-1 rounded-full px-3 py-1 text-[10px] font-black uppercase tracking-wider ${
                      reflection.status === 'passed' ? 'bg-emerald-50 text-emerald-700' : reflection.status === 'blocking' ? 'bg-rose-50 text-rose-700' : 'bg-amber-50 text-amber-700'
                    }`}>
                      <ShieldAlert size={12} />
                      Reflection {reflection.status} · {Math.round((reflection.confidence || 0) * 100)}%
                    </span>
                  )}
                  {consistency && (
                    <span className={`rounded-full px-3 py-1 text-[10px] font-black uppercase tracking-wider ${
                      consistency.status === 'passed' ? 'bg-emerald-50 text-emerald-700' : consistency.status === 'failed' ? 'bg-rose-50 text-rose-700' : 'bg-amber-50 text-amber-700'
                    }`}>
                      System {consistency.status}
                    </span>
                  )}
                </div>
                <div className="rounded-xl border border-slate-100 bg-slate-50 px-4 py-3 text-xs">
                  <div className="flex flex-wrap items-center gap-2">
                    <div className="font-black text-slate-900">审阅关注点</div>
                    {blockingConflictCount > 0 && (
                      <span className="rounded-full bg-rose-100 px-2 py-0.5 text-[10px] font-black text-rose-700">
                        {blockingConflictCount} 阻塞
                      </span>
                    )}
                    {warningConflictCount > 0 && (
                      <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-black text-amber-700">
                        {warningConflictCount} 警告
                      </span>
                    )}
                    {openImpactCount > 0 && (
                      <span className="rounded-full bg-indigo-100 px-2 py-0.5 text-[10px] font-black text-indigo-700">
                        {openImpactCount} 影响
                      </span>
                    )}
                    {disputedSectionCount > 0 && (
                      <span className="rounded-full bg-slate-200 px-2 py-0.5 text-[10px] font-black text-slate-700">
                        {disputedSectionCount} 局部
                      </span>
                    )}
                  </div>
                  {reviewFocusItems.length > 0 ? (
                    <div className="mt-3 space-y-2">
                      {reviewFocusItems.slice(0, 3).map((item) => (
                        <div key={item.id} className="flex flex-col gap-2 rounded-lg bg-white px-3 py-2 sm:flex-row sm:items-start sm:justify-between">
                          <div className="min-w-0">
                            <div className={`text-[10px] font-black uppercase tracking-widest ${
                              item.tone === 'rose' ? 'text-rose-600' : item.tone === 'amber' ? 'text-amber-600' : 'text-slate-500'
                            }`}>
                              {item.label}
                            </div>
                            <div className="mt-0.5 break-words font-bold text-slate-900">{item.title}</div>
                            {item.detail && <div className="mt-0.5 break-words text-slate-500">{item.detail}</div>}
                          </div>
                          {item.action && (
                            <button
                              type="button"
                              onClick={item.action}
                              className="shrink-0 rounded-lg border border-slate-200 bg-white px-2 py-1 text-[10px] font-black text-slate-700 hover:border-indigo-200 hover:text-indigo-700"
                            >
                              处理
                            </button>
                          )}
                        </div>
                      ))}
                      {reviewFocusItems.length > 3 && (
                        <div className="text-[10px] font-bold text-slate-400">还有 {reviewFocusItems.length - 3} 项可在下方明细中查看</div>
                      )}
                    </div>
                  ) : (
                    <div className="mt-2 font-semibold text-slate-500">
                      暂无阻塞冲突、警告、下游影响或局部争议，可以直接阅读并确认当前产物。
                    </div>
                  )}
                </div>
              </div>
              <div className={`grid gap-2 ${canAcceptArtifact ? 'sm:grid-cols-4 xl:w-[480px]' : 'sm:grid-cols-3 xl:w-[360px]'}`}>
                {canAcceptArtifact && (
                  <button
                    type="button"
                    onClick={handleAcceptArtifact}
                    disabled={isWorking}
                    className="inline-flex items-center justify-center gap-2 rounded-lg bg-emerald-600 px-3 py-2 text-xs font-black text-white transition-all hover:bg-emerald-700 disabled:cursor-not-allowed disabled:bg-emerald-300"
                    title="接受当前 Artifact 版本"
                  >
                    <Check size={14} />
                    接受
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => handleStartArtifactDiscussion()}
                  disabled={!canDiscuss}
                  className="inline-flex items-center justify-center gap-2 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-black text-slate-700 transition-all hover:border-indigo-200 hover:text-indigo-700 disabled:cursor-not-allowed disabled:opacity-50"
                  title="对整份产出发起讨论"
                >
                  <MessageSquareText size={14} />
                  讨论
                </button>
                <button
                  type="button"
                  onClick={handleStartManualRevision}
                  disabled={!canDiscuss}
                  className="inline-flex items-center justify-center gap-2 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-black text-slate-700 transition-all hover:border-indigo-200 hover:text-indigo-700 disabled:cursor-not-allowed disabled:opacity-50"
                  title="人工编辑整份产物源码并生成修订版本"
                >
                  <PencilLine size={14} />
                  人工修订
                </button>
                <button
                  type="button"
                  onClick={handleCaptureSelection}
                  disabled={!canDiscuss}
                  className="inline-flex items-center justify-center gap-2 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-black text-slate-700 transition-all hover:border-indigo-200 hover:text-indigo-700 disabled:cursor-not-allowed disabled:opacity-50"
                  title="选中文本后发起局部讨论"
                >
                  <GitCompare size={14} />
                  选区
                </button>
              </div>
            </div>
          </div>
        )}
        <div className="mb-4 flex justify-end">
          <div className="inline-flex rounded-lg border border-slate-200 bg-slate-50 p-1 text-xs font-bold text-slate-600">
            <button
              type="button"
              onClick={() => setArtifactViewMode('rendered')}
              className={`rounded-md px-3 py-1.5 ${artifactViewMode === 'rendered' ? 'bg-white text-indigo-700 shadow-sm' : 'hover:text-slate-900'}`}
            >
              阅读视图
            </button>
            <button
              type="button"
              onClick={() => setArtifactViewMode('source')}
              className={`rounded-md px-3 py-1.5 ${artifactViewMode === 'source' ? 'bg-white text-indigo-700 shadow-sm' : 'hover:text-slate-900'}`}
            >
              源文档
            </button>
          </div>
        </div>
        {governableDesignArtifact && consistencyConflicts.length > 0 && (
          <div className="mb-4 rounded-xl border border-rose-100 bg-rose-50 p-3">
            <div className="mb-2 flex items-center gap-2 text-[10px] font-black uppercase tracking-widest text-rose-700">
              <AlertTriangle size={14} />
              Conflicts
            </div>
            <div className="space-y-2">
              {consistencyConflicts.slice(0, 3).map((conflict) => (
                <div key={conflict.conflict_id} className="rounded-lg bg-white/80 px-3 py-2 text-xs text-slate-700">
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div>
                      <div className="font-bold text-slate-900">{conflict.summary}</div>
                      <div className="text-slate-500">{conflict.conflict_type} · {conflict.semantic} · {conflict.severity}</div>
                    </div>
                    {conflict.status === 'open' && (
                      <button
                        type="button"
                        onClick={() => handleStartArtifactDiscussion(`处理冲突：${conflict.summary}`)}
                        className="rounded-lg border border-rose-100 bg-white px-2 py-1 text-[10px] font-black text-rose-700 hover:bg-rose-50"
                      >
                        处理
                      </button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}
        {governableDesignArtifact && decisionLogs.length > 0 && (
          <div className="mb-4 rounded-xl border border-slate-200 bg-slate-50 p-3">
            <div className="mb-2 text-[10px] font-black uppercase tracking-widest text-slate-500">Decision Log</div>
            <div className="space-y-2">
              {decisionLogs.slice(0, 2).map((decision) => (
                <div key={decision.decision_id} className="rounded-lg bg-white px-3 py-2 text-xs text-slate-700">
                  <div className="font-bold text-slate-900">{decision.decision}</div>
                  <div className="text-slate-500">{decision.basis} · {decision.authority}</div>
                </div>
              ))}
            </div>
          </div>
        )}
        {governableDesignArtifact && (outgoingImpacts.length > 0 || incomingImpacts.length > 0) && (
          <div className="mb-4 rounded-xl border border-amber-100 bg-amber-50 p-3">
            <div className="mb-2 flex items-center gap-2 text-[10px] font-black uppercase tracking-widest text-amber-700">
              <GitBranch size={14} />
              Impact
            </div>
            {incomingImpacts.slice(0, 2).map((impact) => (
              <div key={impact.impact_id} className="mb-2 rounded-lg bg-white/80 px-3 py-2 text-xs text-slate-700">
                <div className="font-bold text-slate-900">被上游影响：{impact.impact_status}</div>
                <div className="text-slate-500">{impact.reason}</div>
                {impact.impact_status !== 'no_impact' && (
                  <button
                    type="button"
                    onClick={() => handleUpdateImpactStatus(impact.impact_id, 'no_impact')}
                    className="mt-2 rounded-lg border border-amber-100 bg-white px-2 py-1 text-[10px] font-black text-amber-700 hover:bg-amber-50"
                  >
                    标记已校验
                  </button>
                )}
              </div>
            ))}
            {outgoingImpacts.slice(0, 2).map((impact) => (
              <div key={impact.impact_id} className="mb-2 rounded-lg bg-white/80 px-3 py-2 text-xs text-slate-700">
                <div className="font-bold text-slate-900">影响下游：{impact.impact_status}</div>
                <div className="text-slate-500">{impact.reason}</div>
                <div className="mt-2 flex flex-wrap gap-2">
                  <button
                    type="button"
                    onClick={() => handleUpdateImpactStatus(impact.impact_id, 'needs_revalidation')}
                    className="rounded-lg border border-amber-100 bg-white px-2 py-1 text-[10px] font-black text-amber-700 hover:bg-amber-50"
                  >
                    需重校验
                  </button>
                  <button
                    type="button"
                    onClick={() => handleUpdateImpactStatus(impact.impact_id, 'needs_regeneration')}
                    className="rounded-lg border border-amber-100 bg-white px-2 py-1 text-[10px] font-black text-amber-700 hover:bg-amber-50"
                  >
                    需重跑
                  </button>
                  <button
                    type="button"
                    onClick={() => handleUpdateImpactStatus(impact.impact_id, 'no_impact')}
                    className="rounded-lg border border-amber-100 bg-white px-2 py-1 text-[10px] font-black text-amber-700 hover:bg-amber-50"
                  >
                    无影响
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
        {governableDesignArtifact && sectionReviews.length > 0 && (
          <div className="mb-4 rounded-xl border border-slate-200 bg-slate-50 p-3">
            <div className="mb-2 text-[10px] font-black uppercase tracking-widest text-slate-500">Section Reviews</div>
            <div className="space-y-2">
              {sectionReviews.slice(0, 3).map((review) => (
                <div key={review.section_review_id} className="rounded-lg bg-white px-3 py-2 text-xs text-slate-700">
                  <div className="font-bold text-slate-900">{review.status}</div>
                  <div className="text-slate-500">{review.reviewer_note || review.anchor_id || 'Artifact-level note'}</div>
                </div>
              ))}
            </div>
          </div>
        )}
        {artifactViewMode === 'source' ? (
          <div data-artifact-source-view="true">
            <pre className="overflow-auto whitespace-pre-wrap rounded-xl border border-slate-200 bg-slate-50 p-4 font-mono text-xs leading-6 text-slate-800">
              {sourceContent}
            </pre>
          </div>
        ) : (
          <div className="prose prose-sm prose-slate max-w-none prose-headings:text-gray-800 prose-headings:font-black prose-a:text-indigo-600 prose-strong:text-gray-900 prose-code:text-indigo-600 prose-pre:bg-transparent prose-pre:p-0">
            <ReactMarkdown 
              remarkPlugins={[remarkGfm]}
              components={markdownComponents}
            >
              {content}
            </ReactMarkdown>
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="flex flex-col h-full">
      <div className="flex flex-wrap gap-2 mb-6">
        {filteredArtifacts.length > 0 ? (
          filteredArtifacts.map((filename) => (
            <button
              key={filename}
              onClick={() => onSelectFile(filename)}
              className={`flex items-center gap-2 px-3 py-1.5 rounded-lg border text-xs transition-all ${
                selectedFile === filename
                  ? 'bg-indigo-50 border-indigo-200 text-indigo-700 shadow-sm font-bold'
                  : 'bg-white border-gray-100 text-gray-500 hover:border-gray-200 hover:bg-gray-50'
              }`}
            >
              {getFileIcon(filename)}
              {filename}
            </button>
          ))
        ) : (
          <div className="w-full py-4 px-2 border border-dashed border-gray-200 rounded-xl flex items-center justify-center">
            <span className="text-xs font-medium text-gray-400 italic">
              {t('projectDetail.noArtifactsProduced') || 'No design artifacts produced yet for this node.'}
            </span>
          </div>
        )}
      </div>

      {renderContent()}

      {isDrawerOpen && (
        <div className="fixed inset-0 z-50 flex justify-end bg-slate-950/30 backdrop-blur-sm">
          <aside className="h-full w-full max-w-xl overflow-y-auto bg-white shadow-2xl">
            <div className="sticky top-0 z-10 flex items-center justify-between border-b border-slate-100 bg-white px-5 py-4">
              <div>
                <div className="text-[10px] font-black uppercase tracking-widest text-indigo-600">
                  {isManualRevision ? 'Manual Revision' : discussionScope === 'selection' ? 'Selection Review' : 'Artifact Review'}
                </div>
                <h3 className="text-base font-black text-slate-900">{selectedFile}</h3>
                {activeDesignArtifact && (
                  <div className="mt-1 text-xs font-semibold text-slate-500">
                    {activeDesignArtifact.expert_id} · v{activeDesignArtifact.artifact_version} · {activeDesignArtifact.status}
                  </div>
                )}
              </div>
              <button
                type="button"
                onClick={() => setIsDrawerOpen(false)}
                className="rounded-lg border border-slate-200 p-2 text-slate-400 hover:text-slate-700"
                title="关闭"
              >
                <X size={16} />
              </button>
            </div>

            <div className="space-y-5 p-5">
              {drawerError && (
                <div className="rounded-xl border border-rose-100 bg-rose-50 px-4 py-3 text-sm font-semibold text-rose-700">
                  {drawerError}
                </div>
              )}

              {isManualRevision ? (
                <section className="space-y-3">
                  <div className="rounded-xl border border-indigo-100 bg-indigo-50/60 p-3">
                    <div className="text-[10px] font-black uppercase tracking-widest text-indigo-600">Manual Edit</div>
                    <div className="mt-1 text-xs font-semibold text-slate-600">
                      直接编辑整份源文档，保存后生成待接受的修订版本。
                    </div>
                  </div>
                  <textarea
                    value={manualRevisionContent}
                    onChange={(event) => setManualRevisionContent(event.target.value)}
                    rows={22}
                    className="w-full rounded-xl border border-slate-200 px-3 py-2 font-mono text-xs leading-5 outline-none focus:border-indigo-300 focus:ring-2 focus:ring-indigo-100"
                  />
                  <section className="space-y-2">
                    <label className="text-[10px] font-black uppercase tracking-widest text-slate-400">Revision Note</label>
                    <textarea
                      value={feedback}
                      onChange={(event) => setFeedback(event.target.value)}
                      rows={3}
                      className="w-full rounded-xl border border-slate-200 px-3 py-2 text-sm outline-none focus:border-indigo-300 focus:ring-2 focus:ring-indigo-100"
                      placeholder="说明本次人工修订的原因"
                    />
                  </section>
                  <button
                    type="button"
                    onClick={handleApplyManualRevision}
                    disabled={isWorking || !manualRevisionContent.trim() || manualRevisionContent === getCurrentSourceContent()}
                    className="inline-flex w-full items-center justify-center gap-2 rounded-xl bg-indigo-600 px-4 py-3 text-sm font-black text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:bg-indigo-300"
                  >
                    <PencilLine size={16} />
                    {isWorking ? '保存中...' : '保存人工修订'}
                  </button>
                </section>
              ) : discussionScope === 'selection' ? (
                <section className="space-y-2">
                  <div className="text-[10px] font-black uppercase tracking-widest text-slate-400">Selected Range</div>
                  <pre className="max-h-40 overflow-auto whitespace-pre-wrap rounded-xl border border-slate-200 bg-slate-50 p-3 text-xs text-slate-700">
                    {selectedExcerpt || '未捕获选区'}
                  </pre>
                  <div className="grid gap-2 sm:grid-cols-2">
                    <button
                      type="button"
                      onClick={() => handleMarkSectionReview('accepted')}
                      disabled={isWorking || !selectedExcerpt.trim()}
                      className="rounded-lg border border-emerald-100 bg-emerald-50 px-3 py-2 text-xs font-black text-emerald-700 hover:bg-emerald-100 disabled:opacity-50"
                    >
                      标记局部已接受
                    </button>
                    <button
                      type="button"
                      onClick={() => handleMarkSectionReview('disputed')}
                      disabled={isWorking || !selectedExcerpt.trim()}
                      className="rounded-lg border border-amber-100 bg-amber-50 px-3 py-2 text-xs font-black text-amber-700 hover:bg-amber-100 disabled:opacity-50"
                    >
                      标记局部有疑问
                    </button>
                  </div>
                </section>
              ) : (
                <section className="rounded-xl border border-slate-200 bg-slate-50 p-3">
                  <div className="text-[10px] font-black uppercase tracking-widest text-slate-400">Scope</div>
                  <div className="mt-1 text-sm font-bold text-slate-800">整份产出</div>
                </section>
              )}

              {!isManualRevision && (
              <section className="space-y-2">
                <label className="text-[10px] font-black uppercase tracking-widest text-slate-400">Feedback</label>
                <textarea
                  value={feedback}
                  onChange={(event) => setFeedback(event.target.value)}
                  rows={4}
                  className="w-full rounded-xl border border-slate-200 px-3 py-2 text-sm outline-none focus:border-indigo-300 focus:ring-2 focus:ring-indigo-100"
                  placeholder="说明你想补充、质疑或修改什么"
                />
              </section>
              )}

              {!isManualRevision && !revisionSession && (
                <button
                  type="button"
                  onClick={handleStartRevision}
                  disabled={isWorking || !feedback.trim() || (discussionScope === 'selection' && !selectedExcerpt.trim())}
                  className="inline-flex w-full items-center justify-center gap-2 rounded-xl bg-indigo-600 px-4 py-3 text-sm font-black text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:bg-indigo-300"
                >
                  <Wand2 size={16} />
                  {isWorking ? '处理中...' : '形成修订意图'}
                </button>
              )}

              {!isManualRevision && revisionSession && (
                <section className="rounded-xl border border-indigo-100 bg-indigo-50/60 p-4">
                  <div className="text-[10px] font-black uppercase tracking-widest text-indigo-600">Intent</div>
                  <div className="mt-2 text-sm font-semibold text-slate-800">
                    {String(normalizedIntent.revision_type || 'supplement')}
                  </div>
                  <div className="mt-1 text-xs text-slate-500">
                    {String(normalizedIntent.revision_reason || '已记录结构化修订意图。')}
                  </div>
                  {revisionSuggestionRationale && (
                    <div className="mt-3 rounded-lg bg-white/70 px-3 py-2 text-xs leading-5 text-slate-600">
                      {revisionSuggestionRationale}
                    </div>
                  )}
                </section>
              )}

              {!isManualRevision && revisionSession && candidateConflictCount > 0 && (
                <section className={`space-y-3 rounded-xl border p-4 ${
                  decisionRequired ? 'border-rose-100 bg-rose-50' : 'border-amber-100 bg-amber-50'
                }`}>
                  <div className={`inline-flex items-center gap-2 text-[10px] font-black uppercase tracking-widest ${
                    decisionRequired ? 'text-rose-700' : 'text-amber-700'
                  }`}>
                    <ShieldAlert size={14} />
                    Context Conflict
                  </div>
                  <div className="text-sm font-bold text-slate-800">
                    {decisionRequired ? '需要先裁决，再判断是否影响下游产物' : '检测到 To-Be 或待确认差异'}
                  </div>
                  <div className="text-xs leading-5 text-slate-600">
                    系统判定：{String(normalizedIntent.semantic || 'missing_context')} · 候选冲突 {candidateConflictCount} 个
                  </div>
                  <div className="grid gap-2 text-xs font-semibold text-slate-700">
                    <div className="rounded-lg bg-white/70 px-3 py-2">作为目标设计继续，并生成变更建议</div>
                    <div className="rounded-lg bg-white/70 px-3 py-2">按当前资产事实调整专家产出</div>
                    <div className="rounded-lg bg-white/70 px-3 py-2">先标记待确认，用户修订后再评估下游影响</div>
                  </div>
                  <div className="grid gap-2 sm:grid-cols-2">
                    <button
                      type="button"
                      onClick={() => handleResolveConflict(String(candidateConflictIds[0] || ''), 'to_be')}
                      disabled={isWorking || candidateConflictIds.length === 0}
                      className="rounded-lg bg-white px-3 py-2 text-xs font-bold text-slate-700 hover:bg-slate-100 disabled:opacity-50"
                    >
                      作为 To-Be 目标
                    </button>
                    <button
                      type="button"
                      onClick={() => handleResolveConflict(String(candidateConflictIds[0] || ''), 'as_is')}
                      disabled={isWorking || candidateConflictIds.length === 0}
                      className="rounded-lg bg-white px-3 py-2 text-xs font-bold text-slate-700 hover:bg-slate-100 disabled:opacity-50"
                    >
                      以当前事实为准
                    </button>
                  </div>
                </section>
              )}

              {!isManualRevision && revisionSession && (
                <section className="space-y-2">
                  <label className="text-[10px] font-black uppercase tracking-widest text-slate-400">Replacement</label>
                  <textarea
                    value={replacementText}
                    onChange={(event) => {
                      setReplacementText(event.target.value);
                      setRevisionSuggestionHasChanges(event.target.value !== selectedExcerpt);
                    }}
                    rows={7}
                    disabled={discussionScope !== 'selection'}
                    className="w-full rounded-xl border border-slate-200 px-3 py-2 font-mono text-xs outline-none focus:border-indigo-300 focus:ring-2 focus:ring-indigo-100"
                  />
                  {revisionSuggestionHasChanges === false && (
                    <div className="rounded-lg border border-amber-100 bg-amber-50 px-3 py-2 text-xs font-semibold text-amber-700">
                      当前 Replacement 与原选区一致，系统不会创建无变化的新版本。
                    </div>
                  )}
                  <button
                    type="button"
                    onClick={handleCreatePatchPreview}
                    disabled={isWorking || discussionScope !== 'selection' || !replacementText.trim() || revisionSuggestionHasChanges === false}
                    className="inline-flex w-full items-center justify-center gap-2 rounded-xl border border-slate-200 bg-white px-4 py-3 text-sm font-black text-slate-700 hover:border-indigo-200 hover:text-indigo-700 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    <GitCompare size={16} />
                    {isWorking ? '处理中...' : '生成 Patch Preview'}
                  </button>
                </section>
              )}

              {patchPreview && (
                <section className="space-y-3 rounded-xl border border-slate-200 p-4">
                  <div className="flex items-center justify-between">
                    <div className="text-[10px] font-black uppercase tracking-widest text-slate-400">Patch Preview</div>
                    <span className="rounded-full bg-slate-100 px-2 py-1 text-[10px] font-black text-slate-600">
                      {patchPreview.patch_status}
                    </span>
                  </div>
                  <pre className="max-h-64 overflow-auto rounded-lg bg-slate-950 p-3 text-xs text-slate-100">
                    {(patchPreview.diff.unified_diff || []).join('\n') || 'No diff.'}
                  </pre>
                  <div className="rounded-lg bg-slate-50 px-3 py-2 text-xs font-semibold text-slate-600">
                    Policy: {patchPreview.preserve_policy}
                  </div>
                  <button
                    type="button"
                    onClick={handleApplyPatch}
                    disabled={isWorking || patchPreview.patch_status === 'applied'}
                    className="inline-flex w-full items-center justify-center gap-2 rounded-xl bg-emerald-600 px-4 py-3 text-sm font-black text-white hover:bg-emerald-700 disabled:cursor-not-allowed disabled:bg-emerald-300"
                  >
                    <Check size={16} />
                    {patchPreview.patch_status === 'applied'
                      ? (patchPreview.apply_result?.revision_mode === 'updated_existing_revision' ? '已应用到当前修订版本' : '已应用并生成新版本')
                      : isWorking ? '应用中...' : '应用补丁'}
                  </button>
                </section>
              )}
            </div>
          </aside>
        </div>
      )}
    </div>
  );
};
