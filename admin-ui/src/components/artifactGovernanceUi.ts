import type { DesignArtifact } from '../api';

const SYSTEM_CONTROLLED_ARTIFACT_FILES = new Set([
  'requirements.json',
  'input-requirements.md',
  'original-requirements.md',
  'clarified-requirements.md',
  'planner-reasoning.md',
  'planner-output.md',
]);

export const isSystemControlledArtifactFile = (fileName: string | null) => {
  const normalized = (fileName || '').toLowerCase();
  return (
    SYSTEM_CONTROLLED_ARTIFACT_FILES.has(normalized)
    || normalized.startsWith('planner-')
    || normalized.startsWith('validator')
    || normalized.startsWith('validation')
  );
};

export const isSystemControlledDesignArtifact = (
  fileName: string | null,
  artifact?: Pick<DesignArtifact, 'expert_id'> | null,
) => isSystemControlledArtifactFile(fileName) || artifact?.expert_id === 'validator';

export const canAcceptDesignArtifact = (
  artifact: Pick<DesignArtifact, 'status'> | null | undefined,
  canDiscuss: boolean,
  overallReviewStatus: string,
) => Boolean(
  canDiscuss
  && artifact
  && ['ready_for_review', 'reflection_warning'].includes(artifact.status)
  && overallReviewStatus !== 'blocked',
);
