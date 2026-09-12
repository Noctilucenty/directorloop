"""Typed domain contracts shared by every layer of DirectorLoop."""

from .assets import AssetCoverageMap, AssetKind, AssetManifest, AssetOrigin, AssetRecord, CoverageEntry, CoverageStatus, StreamInfo
from .brief import ACTION_COST_ORDER, Budget, ConstraintKind, CreativeBrief, GoalProfile, ProtectedConstraint, RepairAction, RequiredInformation
from .decision import Comparison, HardGate, Outcome, PromotionDecision, QuestionDelta
from .edit_plan import (
    AdjustCaptionTiming, Caption, CropRect, EditOp, EditOpList, EditPlan, EditPlanError, InsertGeneratedSegment,
    InsertSegment, MoveSegment, MusicTrack, NarrationTrack, OutputProfile, ProtectedInterval, RemoveCaption,
    RemoveSegment, ReplaceSegment, Segment, SetCaption, SetCrop, TrimSegment, apply_ops, describe_ops, plan_diff,
)
from .evaluation import (
    ConstraintCheck, EvaluationRun, LeakageControlResult, MeasurementType, MechanicalCheck, ProbeAnswer, ProbeModality,
    ProbeResult, QuestionSummary,
)
from .findings import FailureCategory, FailureFinding, FindingEvidence
from .ids import new_id, sha256_bytes, sha256_file, sha256_json, utc_now_iso
from .pack import DemoPack, load_pack, write_checksums
from .policy import PolicyEvidence, PolicyRule, PolicyStatus, PolicyStore, RouterStat
from .repair import ActionEstimate, GenerationRequest, LatencyEstimate, RejectedAlternative, RepairProposal
from .truth import (
    NOT_SHOWN_OPTION_ID, Claim, ClaimKind, EvaluationSuite, EvidenceModality, ProbeOption, ProbeQuestion,
    ProbeQuestionView, SourceReference, SourceTruth, SuiteSplit,
)
