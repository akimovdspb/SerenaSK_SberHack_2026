export type ExecutionMode =
  | "deterministic_template"
  | "live_ouroboros"
  | "replay"
  | "validation_only"
  | "mock";

export type Health = {
  status: string;
  environment: string;
  data_mode: string;
  external_send_enabled: boolean;
};

export type PublicConfig = {
  data_mode: "synthetic_only";
  external_send_enabled: false;
  approval_requires_human_session: true;
  runtime_modes: ExecutionMode[];
  default_execution_mode: "deterministic_template" | "live_ouroboros";
  human_actions_test_only: boolean;
  demo_reset_enabled: boolean;
  session_auth_enabled: boolean;
};

export type DemoResetResult = {
  reset_id: string;
  status: "RESET";
  catalog_case_count: number;
  observed_case_count: 0;
  live_case_count: 0;
  provider_calls: 0;
  reset_at: string;
};
export type CaseView = {
  case_id: string;
  title: string;
  expected_status: string;
  synthetic: true;
};

export type DashboardCase = {
  case: CaseView;
  campaign_id: string | null;
  actual_status: string | null;
  execution_mode: ExecutionMode | null;
  last_run_status: string | null;
  latency_ms: number | null;
  qa_score: number | null;
  blocker_count: number;
  package_id: string | null;
  updated_at: string | null;
};

export type DashboardMetrics = {
  catalog_case_count: number;
  target_business_case_count: number;
  observed_case_count: number;
  live_case_count: number;
  p50_latency_ms: number | null;
  p95_latency_ms: number | null;
  max_latency_ms: number | null;
  crash_count: number;
  timeout_count: number;
  provider_tokens: number;
  provider_cost_usd: number;
};

export type Dashboard = {
  generated_at: string;
  business_cases: DashboardCase[];
  chaos_cases: DashboardCase[];
  metrics: DashboardMetrics;
  synthetic: true;
  no_send: true;
};

export type BriefQuestion = {
  question_id: string;
  path: string;
  reason: string;
  message: string;
  options: string[];
};

export type Brief = {
  campaign_id: string;
  version: number;
  input_hash: string;
  name: string | null;
  objective: string | null;
  product_id: string | null;
  segment_id: string | null;
  trigger_id: string | null;
  channels: string[];
  cta_label: string | null;
  cta_url: string | null;
  tone: string | null;
  notes: string | null;
  synthetic: true;
};

export type Campaign = {
  campaign_id: string;
  state: string;
  draft_version: number;
  draft: Brief;
  validation: {
    status: string;
    questions: BriefQuestion[];
    blockers: string[];
    llm_calls: 0;
  } | null;
  ready_brief: (Brief & {
    mandatory_fact_ids: string[];
    mandatory_concept_ids: string[];
  }) | null;
  context_version: string | null;
  package_id: string | null;
  created_at: string;
  updated_at: string;
};

export type AuthoringFact = {
  fact_id: string;
  source_id: string;
  label: string;
  canonical_text: string;
  kind: string;
  source_label: string;
  normalized_value: unknown;
};

export type AuthoringProduct = {
  product_id: string;
  version: number;
  exact_name: string;
  cta_label: string;
  cta_url: string;
  facts: AuthoringFact[];
  origin: "catalog" | "custom";
  synthetic: true;
};

export type AuthoringPersona = {
  segment_id: string;
  trigger_id: string;
  label: string;
  tone_hint: string;
  connected_product_ids: string[];
  available_channels: Array<"sms" | "email">;
  synthetic: true;
};

export type EditorialReference = {
  reference_id: string;
  title: string;
  description: string;
  label: "EDITORIAL_REFERENCE_NOT_LIVE_NOT_RELEASE_EVIDENCE";
  brief: {
    name: string | null;
    objective: string | null;
    product_id: string | null;
    segment_id: string | null;
    trigger_id: string | null;
    channels: Array<"sms" | "email">;
    cta_label: string | null;
    cta_url: string | null;
    tone: string | null;
    offer_period: { start: string | null; end: string | null } | null;
    notes: string | null;
    synthetic: true;
  };
  custom_product: {
    exact_name: string;
    cta_label: string;
    cta_url: string;
    facts: Array<{
      label: string;
      canonical_text: string;
      kind: string;
      source_label: string;
      normalized_value: unknown;
      allowed_surface_forms: string[];
    }>;
  } | null;
};

export type AuthoringCatalog = {
  products: AuthoringProduct[];
  personas: AuthoringPersona[];
  references: EditorialReference[];
  synthetic: true;
  no_send: true;
};

export type RecentCampaign = {
  campaign_id: string;
  name: string | null;
  product_name: string | null;
  channels: Array<"sms" | "email">;
  state: string;
  updated_at: string;
  synthetic: true;
};

export type Fact = {
  fact_id: string;
  source_id: string;
  kind: string;
  canonical_text: string;
  normalized_value: unknown;
  synthetic: true;
};

export type Context = {
  classification: "untrusted_data";
  context_version: string;
  operation: "initial" | "revision" | "rule_proposal";
  product: {
    product_id: string;
    exact_name: string;
    version: number;
  };
  facts: Fact[];
  concepts: Array<{
    concept_id: string;
    accepted_surface_forms: string[];
    synthetic: true;
  }>;
  source_manifest: Array<{
    source_id: string;
    version: string;
    retrieved_at: string;
    synthetic: true;
  }>;
  active_rules: Array<Record<string, unknown>>;
  rules_version: string;
  content_plan: {
    selected_fact_ids: string[];
    selected_concept_ids: string[];
    selection_sources: string[];
    applied_rule_version_ids: string[];
  };
  allowed_changed_paths: string[];
  protected_paths: string[];
  output_schema_id: string;
};

export type ClaimEvidence = {
  claim_id: string;
  channel: "sms" | "email";
  artifact_path: string;
  text_fragment: string;
  claim_type: string;
  normalized_value: unknown;
  fact_id: string;
  source_id: string;
};

export type SmsArtifact = {
  text: string;
  cta_url: string;
  fact_refs: string[];
  personalization_refs: string[];
};

export type EmailArtifact = {
  subject: string;
  preheader: string;
  headline: string;
  sections: Array<{
    section_id: string;
    kind: string;
    heading: string;
    body: string;
    fact_refs: string[];
  }>;
  cta_label: string;
  cta_url: string;
  disclaimer_ids: string[];
  plain_text: string;
  fact_refs: string[];
};

export type Finding = {
  finding_id: string;
  check_id: string;
  severity: "BLOCKER" | "WARNING" | "INFO";
  artifact: string;
  path: string | null;
  quote: string | null;
  expected: string | null;
  actual: string | null;
  source_ids: string[];
  recommendation: string;
  blocking: boolean;
  status: string;
};

export type PackageView = {
  package_id: string;
  campaign_id: string;
  package_version: number;
  mode: ExecutionMode;
  context_version: string;
  package_hash: string;
  bundle: {
    summary: string;
    personalization_rationale: string[];
    sms: SmsArtifact | null;
    email: EmailArtifact | null;
    channel_suppressions: Array<{
      channel: "sms" | "email";
      reason_code: string;
      reason: string;
    }>;
    claim_evidence: ClaimEvidence[];
    warnings: string[];
  };
  quality_report: {
    approvable: boolean;
    findings: Finding[];
    checked_ids: string[];
    checked_fact_ids: string[];
    deterministic_score: number;
    sms_metrics: {
      encoding: string;
      characters: number;
      code_units: number;
      segments: number;
      units_per_segment: number;
    } | null;
  };
  email_html: string;
  created_at: string;
};

export type Feedback = {
  feedback_id: string;
  package_id: string;
  artifact_path: string;
  comment: string;
  scope: string;
  author_id: string;
  author_role: string;
  created_at: string;
};

export type PackageDiff = {
  diff_id: string;
  feedback_id: string;
  from_package_id: string;
  to_package_id: string;
  changed_paths: string[];
  protected_paths: string[];
  changes: Array<{
    path: string;
    before_hash: string;
    after_hash: string;
    before_preview: string;
    after_preview: string;
    protected: boolean;
  }>;
  created_at: string;
};

export type RuleProposal = {
  proposal_id: string;
  status: string;
  proposal: {
    source_feedback_id: string;
    type: string;
    scope: {
      product_ids: string[];
      channel: string | null;
      segment_ids: string[];
    };
    value: string;
    rationale: string;
    target_case_ids: string[];
    base_rules_version: string;
    candidate_rules_version: string;
    risk: string;
  };
  tests: Array<{
    case_id: string;
    test_kind: "target" | "regression" | "out_of_scope";
    expected_applied: boolean;
    actual_applied: boolean;
    passed: boolean;
    detail: string;
  }>;
  test_only: boolean | null;
  created_at: string;
};

export type RuleVersion = {
  rule_version_id: string;
  proposal_id: string;
  status: "APPROVED" | "ROLLED_BACK";
  rules_version: string;
  previous_rules_version: string;
  active: boolean;
  test_only: boolean;
  created_at: string;
};

export type Approval = {
  approval_id: string;
  package_id: string;
  package_hash: string;
  decision: string;
  test_only: boolean;
  approval_hash: string;
  created_at: string;
};

export type ExportRecord = {
  export_id: string;
  package_id: string;
  package_hash: string;
  approval_hash: string;
  archive_sha256: string;
  file_count: number;
  synthetic: true;
  no_send: true;
  created_at: string;
};

export type Run = {
  run_id: string;
  operation: string;
  mode: ExecutionMode;
  status: string;
  reason_code: string | null;
  task_id: string | null;
  package_id: string | null;
  context_version: string;
  physical_attempt_count: number;
  attempts: RunAttempt[];
  created_at: string;
  terminal_at: string | null;
};

export type RunAttempt = {
  attempt_id: string;
  attempt_number: number;
  task_id: string;
  status: string;
  provider: string;
  model: string;
  provider_profile: string;
  request_digest: string;
  context_digest: string;
  outcome: string;
  reason_code: string | null;
  failure_kind: string;
  retry_allowed: boolean;
  usage_status: "EXACT" | "UNKNOWN";
  draft_present: boolean;
  result_present: boolean;
  created_at: string;
  started_at: string | null;
  terminal_at: string | null;
  released_at: string | null;
};

export type SafeTraceEvent = {
  event_id: string;
  event_type: string;
  label: string;
  created_at: string;
  mode: ExecutionMode | null;
};

export type OperationPresentation = {
  run_id: string;
  operation: "initial" | "revision" | "rule_proposal";
  status:
    | "QUEUED"
    | "RUNNING"
    | "CANCEL_REQUESTED"
    | "COMPLETED"
    | "COMPLETED_FALLBACK"
    | "FAILED"
    | "CANCELLED";
  mode: ExecutionMode;
  active: boolean;
  title: string;
  stage: string;
  stage_label: string;
  attempt_number: 1 | 2;
  elapsed_from: string;
  result_hint: string;
  reason_code: string | null;
};

export type Workspace = {
  campaign: Campaign;
  context: Context | null;
  package: PackageView | null;
  package_history: PackageView[];
  feedback: Feedback[];
  diffs: PackageDiff[];
  rule_proposals: RuleProposal[];
  rule_versions: RuleVersion[];
  approvals: Approval[];
  exports: ExportRecord[];
  runs: Run[];
  safe_trace: SafeTraceEvent[];
  operation_state: OperationPresentation | null;
  approval_eligible: boolean;
  approval_disabled_reason: string | null;
  export_eligible: boolean;
  export_disabled_reason: string | null;
};

export type EvaluationSummary = {
  evaluation_id: string;
  label: string;
  status: string;
  frozen: boolean;
  generated_at: string;
  observed_case_count: number;
};

export type EvaluationRun = {
  evaluation_id: string;
  label: string;
  status: "NOT_FROZEN" | "FROZEN" | "FAILED";
  frozen: boolean;
  generated_at: string;
  business_cases: DashboardCase[];
  chaos_cases: DashboardCase[];
  metrics: DashboardMetrics;
  mode_counts: Record<string, number>;
  qualitative_review_status: "WAITING_FOR_OPERATOR" | "COMPLETE";
  report_links: Array<{
    label: string;
    format: string;
    href: string;
    checksum: string | null;
  }>;
  synthetic: true;
  no_send: true;
};

export type MvpResultCase = {
  case_id: string;
  title: string;
  actual_terminal: string;
  qa_score: number;
  latency_ms: number;
  provider_calls: number;
  provider_tokens: number;
  cost_usd: number;
  channels: Array<"sms" | "email">;
  sms: {
    text: string;
    segments: number | null;
  } | null;
  email: {
    subject: string;
    plain_text: string;
  } | null;
};

export type MvpResults = {
  results_id: string;
  status: "MVP_CONFIRMED_NON_RELEASE";
  generated_at: string;
  cases: MvpResultCase[];
  metrics: {
    confirmed_live_case_count: number;
    basket_live_case_count: number;
    full_basket_passed_count: number;
    full_basket_case_count: number;
    p50_latency_ms: number;
    p95_latency_ms: number;
    max_latency_ms: number;
    provider_calls: number;
    provider_tokens: number;
    provider_cost_usd: number;
  };
  report_links: Array<{
    label: string;
    format: string;
    href: string;
    checksum: string | null;
  }>;
  canonical_release_evidence: false;
  synthetic: true;
  no_send: true;
};

export type Diagnostics = {
  generated_at: string;
  components: Array<{
    component_id: string;
    label: string;
    status: "READY" | "DEGRADED" | "ISOLATED";
    detail: string;
  }>;
  runtime_tag: string | null;
  runtime_commit: string | null;
  skill_hash: string | null;
  prompt_hash: string | null;
  tool_inventory_hash: string | null;
  discovered_tools: string[];
  contract_generated_at: string | null;
  active_run_count: number;
  queue_state: "IDLE" | "ACTIVE";
  admission_state: "CLOSED" | "OPEN";
  latest_errors: Array<{
    run_id: string;
    reason_code: string | null;
    status: string;
    created_at: string;
  }>;
  public_config_only: true;
};
