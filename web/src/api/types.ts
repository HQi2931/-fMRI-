import type { components } from "./schema.generated";

type Schemas = components["schemas"];

export type PaperIngestResult = Schemas["PaperIngestResult"];
export type Citation = {
  citation_id: string; chunk_id: string; source: string; title: string; excerpt: string;
  paper_id?: string | null; section?: string | null; subsection?: string | null;
  page_start?: number | null; page_end?: number | null;
};
export type Health = Schemas["HealthView"];
export type EnvironmentProbe = Schemas["EnvironmentProbeView"];
export type EnvironmentConfig = Schemas["EnvironmentConfigView"];
export type Project = Schemas["ProjectView"];
export type Dataset = Schemas["DatasetView"];
export type Manifest = Schemas["ManifestRevisionView"];
export type Demographics = Schemas["DemographicsRevisionView"];
export type DatasetSplit = Schemas["DatasetSplitView"];
export type Skill = Schemas["SkillSpec"];
export type CompiledSkillPlan = Schemas["SkillPlan"];
export type PlanRevision = Schemas["PlanRevisionView"];
export type Run = Schemas["RunView"];
export type Artifact = Schemas["ArtifactView"];
export type RuntimeEvent = Schemas["RuntimeEventView"];
export type QcReview = Schemas["QcReviewView"];
export type ModelProfile = Schemas["ModelProfileView"];
export type AgentTask = Schemas["AgentTaskView"];
export type WorkspaceCheck = Omit<Schemas["WorkspaceCheckView"], "free_space_bytes" | "total_space_bytes" | "issues" | "stage_candidates">
  & Pick<Schemas["WorkspaceCheckView"], "path" | "kind" | "file_count" | "nifti_count" | "dicom_count" | "subject_count" | "functional_subject_count" | "anatomical_subject_count" | "input_stage" | "selected_input_stage" | "stage_selection_required" | "output_directories" | "invalid_nifti_files" | "warnings" | "subjects" | "checked_at">
  & { free_space_bytes?: number; total_space_bytes?: number; issues: string[]; stage_candidates: Array<Schemas["WorkspaceStageCandidate"] & { issues: string[] }> };
export type WorkspacePick = Schemas["WorkspacePickView"];
export type WorkspaceFileEntry = Omit<Schemas["WorkspaceFileEntryResponse"], "size" | "format" | "readable">
  & { size?: number; format?: string; readable?: boolean };
export type WorkspaceFileListing = Omit<Schemas["WorkspaceFileListingResponse"], "entries">
  & { entries: WorkspaceFileEntry[] };
export type WorkspaceConsent = Schemas["WorkspaceContentConsent"];
export type WorkspaceProposal = Schemas["WorkspaceChangeProposal"];
export type WorkspacePatchApplyView = Schemas["WorkspacePatchApplyView"];
export type WorkspacePatchUndoView = Schemas["WorkspacePatchUndoView"];
export type ConversationMode = Schemas["ConversationMode"];
export type ConversationMessage = Omit<Schemas["ConversationMessageView"], "payload"> & Required<Pick<Schemas["ConversationMessageView"], "payload">>;
export type ConversationToolCall = Schemas["ConversationToolCallView"];
export type Conversation = Omit<Schemas["ConversationView"], "foreground_agent_role" | "work_tasks" | "messages" | "tool_calls">
  & { foreground_agent_role?: Schemas["ConversationView"]["foreground_agent_role"]; work_tasks?: Schemas["ConversationView"]["work_tasks"]; messages: ConversationMessage[]; tool_calls: ConversationToolCall[] };
export type ConversationTurn = Omit<Schemas["ConversationTurnView"], "conversation" | "user_message" | "assistant_message" | "tool_call" | "tool_calls">
  & { conversation: Conversation; user_message: ConversationMessage; assistant_message: ConversationMessage; tool_call: ConversationToolCall | null; tool_calls: ConversationToolCall[] };
export type ConversationContext = Schemas["ConversationContextView"];
export type ConversationMemory = Schemas["MemoryView"];
export type CompositeGoalProgress = Schemas["CompositeGoalProgressView"];
export type StepPlanReviewDetail = Schemas["StepPlanReviewDetailView"];
export type StepPlanReviewSummary = Schemas["StepPlanReviewSummaryView"];
export type StepPlanReviewList = Schemas["StepPlanReviewListView"];
type ConversationTurnDefaults = "action" | "allow_remote_search" | "paper_ids" | "real_execution_confirmed" | "start_new_analysis_goal" | "stream";
export type ConversationTurnBody = Omit<Schemas["ConversationTurnCreate"], ConversationTurnDefaults>
  & Partial<Pick<Schemas["ConversationTurnCreate"], ConversationTurnDefaults>>
  & { workspace_file_scope?: { paths: string[] } | null };
export type StatisticalDesign = Schemas["StatisticalDesignView"];
export type StatisticalDesignCreateBody = Schemas["StatisticalDesignCreate"];
export type StatisticalResult = Schemas["StatisticalResultView"];
export type StatisticalResultDetail = Schemas["StatisticalResultDetailView"];
export type ExternalAnalysisImport = Schemas["ExternalAnalysisImportView"];
export type ExternalAnalysisImportSummary = Schemas["ExternalAnalysisImportSummaryView"];
export type ExternalDemographicsRevision = Schemas["ExternalDemographicsRevisionView"];
export type StatisticalClusterRoi = Schemas["StatisticalClusterRoiView"];
export type StatisticalAtlas = Schemas["StatisticalAtlasView"];
export type StatisticalAtlasSelection = Schemas["StatisticalAtlasSelection"];
export type StatisticalMethodSummary = Schemas["StatisticalMethodSummary"];
export type StatisticalResultLocalization = Schemas["StatisticalResultLocalizationView"];
export type StatisticalResultSliceInfo = Schemas["StatisticalResultSliceInfoView"];
export type RunDiagnosis = Schemas["RunDiagnosisView"];
export type MlTableInspection = Schemas["MlTableInspectView"];
export type MlTemplate = Schemas["MlTemplateView"];
export type RoiTable = Schemas["RoiTableView"];
export type ClusterLocalization = Schemas["ClusterLocalizationView"];
export type RsFmriAnswer = Schemas["RsFmriAnswerView"];
export type OrganizationPreview = Schemas["OrganizationPreviewView"];
export type SkillPlanResolveBody = Schemas["SkillPlanResolveRequest"];
export type PreprocessingInput = NonNullable<SkillPlanResolveBody["request"]["preprocessing"]>;

export type ProblemDetails = Schemas["ErrorBody"];
