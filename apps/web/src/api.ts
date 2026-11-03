export type Platform = "douyin" | "kuaishou";
export type VerificationStatus = "unverified" | "source_verified" | "human_confirmed";
export type EligibilityStatus = "unverified" | "manually_confirmed";
export type ReviewStatus = "pending" | "approved" | "rejected";
export type RunStatus = "queued" | "running" | "waiting_review" | "succeeded" | "failed" | "cancelled";
export type RunStage = "prepare" | "script" | "fact_check" | "review" | "storyboard" | "render" | "complete";

export interface BookFact {
  key: string;
  value: string;
  verification_status: VerificationStatus;
  source_note: string | null;
  evidence_id: string | null;
  revision?: number;
}

export interface BookFactRevision extends BookFact {
  book_id: string;
  revision: number;
  created_at: string;
  created_by_user_id: string;
}

export interface BookFactRevisionRequest {
  expected_revision: number;
  value: string;
  verification_status: VerificationStatus;
  source_note: string | null;
  evidence_id: string | null;
}

export interface BookCreate {
  isbn: string | null;
  title: string;
  edition_label: string;
  author: string;
  publisher: string | null;
  original_opinion: string;
  facts: BookFact[];
}

export interface BookEdition extends BookCreate {
  id: string;
  owner_user_id: string;
  created_at: string;
}

export interface BookPage {
  items: BookEdition[];
  next_cursor: string | null;
}

export interface PromotionCandidateCreate {
  book_id: string;
  platform: Platform;
  platform_product_id: string;
  product_url: string;
  eligibility_status: "unverified";
  eligibility_checked_at: null;
  eligibility_note: string | null;
}

export interface PromotionCandidate extends Omit<PromotionCandidateCreate, "eligibility_status" | "eligibility_checked_at"> {
  id: string;
  owner_user_id: string;
  created_at: string;
  eligibility_status: EligibilityStatus;
  eligibility_checked_at: string | null;
  eligibility_revision: number;
  eligibility_checked_by_user_id: string | null;
}

export interface PromotionEligibilityUpdate {
  eligibility_status: EligibilityStatus;
  expected_revision: number;
  eligibility_note: string | null;
  confirmation_basis: "creator_account_backend" | null;
}

export interface CancelRequest {
  reason: string | null;
}

export interface EvidenceCreate {
  source_type: "manual" | "licensed";
  source_note: string;
  source_url: string | null;
  observed_at: string;
  usage_basis: string;
  metric_name: string | null;
  metric_value: number | null;
  metric_window_start: string | null;
  metric_window_end: string | null;
  missing_reason: string | null;
}

export interface Evidence extends EvidenceCreate {
  id: string;
  owner_user_id: string;
  collected_at: string;
}

export interface EvidencePage {
  items: Evidence[];
  next_cursor: string | null;
}

// --- W4 research desk: metric evidence, content references, style cards, CSV import ---

export type MetricBasis =
  | "authorized_order_sales"
  | "platform_listing_display"
  | "third_party_or_manual_estimate"
  | "engagement_signal"
  | "legacy_unclassified";

export type MetricName =
  | "sales_units"
  | "sales_amount"
  | "listed_price"
  | "displayed_sales_label"
  | "plays"
  | "likes"
  | "comments"
  | "shares"
  | "favorites"
  | "follower_count"
  | "ranking_position"
  | "search_heat"
  | "other";

export type MetricUnit =
  | "count"
  | "minor_currency"
  | "currency_yuan"
  | "views"
  | "likes"
  | "percent"
  | "rank"
  | "score"
  | "text"
  | "unspecified";

export type ObjectType = "product_listing" | "creator_account" | "content_item" | "book_edition";

export interface MetricObservationCreate {
  platform: Platform | null;
  book_id: string | null;
  object_type: ObjectType;
  object_ref: string;
  object_label: string | null;
  metric_name: MetricName;
  metric_definition: string;
  basis: MetricBasis;
  unit: MetricUnit;
  value: number | null;
  value_text: string | null;
  window_start: string | null;
  window_end: string | null;
  observed_at: string;
  evidence_id: string | null;
  missing_reason: string | null;
  confidence: "high" | "medium" | "low" | null;
  expires_at: string | null;
  note: string | null;
}

export interface MetricObservation extends MetricObservationCreate {
  id: string;
  owner_user_id: string;
  collected_at: string;
  legacy_backfill: boolean;
}

export interface MetricObservationPage {
  items: MetricObservation[];
  next_cursor: string | null;
}

export type LicenseScope = "own_account" | "licensed_data_service" | "manual_observation";
export type CapturedField = "title" | "author" | "platform_ref" | "url" | "observed_note" | "publication_date" | "platform_label";

export interface ContentReferenceCreate {
  platform: Platform | null;
  book_id: string | null;
  ref_type: ObjectType;
  external_ref: string | null;
  reference_url: string | null;
  title: string;
  note: string | null;
  license_scope: LicenseScope;
  usage_basis: string;
  captured_fields: CapturedField[];
  evidence_ids: string[];
  observed_at: string | null;
  material_statement: "no_third_party_media_or_verbatim_transcript";
}

export interface ContentReference extends ContentReferenceCreate {
  id: string;
  owner_user_id: string;
  created_at: string;
}

export interface ContentReferencePage {
  items: ContentReference[];
  next_cursor: string | null;
}

export type StyleCardStatus = "draft" | "pending_review" | "approved" | "rejected";
export type StyleFeatureKey =
  | "opening_hook"
  | "narrative_structure"
  | "pacing"
  | "visual_style"
  | "subtitle_style"
  | "tone"
  | "cta"
  | "audience_match"
  | "other";

export interface StyleFeature {
  feature_key: StyleFeatureKey;
  label: string;
  explanation: string;
  evidence_ids: string[];
  sample_count: number;
}

export interface StyleCardCreate {
  title: string;
  target_platform: Platform | null;
  book_id: string | null;
  derived_by: "manual" | "workflow_extraction";
  features: StyleFeature[];
  evidence_ids: string[];
  reference_ids: string[];
  human_note: string | null;
}

export interface StyleCardUpdate {
  expected_revision: number;
  title?: string;
  target_platform?: Platform | null;
  features?: StyleFeature[];
  evidence_ids?: string[];
  reference_ids?: string[];
  human_note?: string | null;
}

export interface StyleCardReview {
  decision: "approve" | "reject";
  note: string | null;
}

export interface StyleCard extends StyleCardCreate {
  id: string;
  owner_user_id: string;
  created_at: string;
  updated_at: string;
  revision: number;
  review_status: StyleCardStatus;
  edited_by_user_id: string | null;
  last_edited_at: string | null;
  abstract_only: boolean;
  evidence_sample_count: number;
}

export interface StyleCardPage {
  items: StyleCard[];
  next_cursor: string | null;
}

export type ImportStatus = "preview" | "applied" | "partially_applied" | "failed";

export type ImportRowCode =
  | "encoding_error"
  | "malformed_row"
  | "missing_required_field"
  | "invalid_book_isbn"
  | "invalid_number"
  | "invalid_timestamp"
  | "invalid_platform"
  | "invalid_url"
  | "formula_injection"
  | "duplicate_natural_key"
  | "edition_mismatch_suspected"
  | "unknown_column"
  | "field_too_long"
  | "invalid_mapping"
  | "reference_rights_unsupported";

export interface ImportRowDiagnostic {
  row_number: number;
  field: string | null;
  code: ImportRowCode;
  message: string;
  severity: "error" | "warning";
}

export interface ImportPreview {
  id: string;
  owner_user_id: string;
  status: ImportStatus;
  filename: string;
  encoding_detected: "utf8" | "utf8_bom" | "unknown";
  file_sha256: string;
  size_bytes: number;
  columns: string[];
  total_rows: number;
  parsed_rows: number;
  valid_rows: number;
  failed_rows: number;
  skipped_duplicate_rows: number;
  mapping_suggestions: Record<string, string | null>;
  row_diagnostics: ImportRowDiagnostic[];
  created_at: string;
  expires_at: string;
}

export interface ImportConfirm {
  mapping: Record<string, string | null>;
  on_duplicate: "skip" | "update";
  match_by: "isbn" | "isbn_and_edition" | "title_author_fuzzy";
  normalize_isbn: boolean;
  note: string | null;
}

export interface ImportCounts {
  total: number;
  parsed: number;
  persisted: number;
  skipped_duplicate: number;
  failed: number;
  warnings: number;
}

export type ImportEntityKind = "book_edition" | "promotion_candidate" | "metric_observation" | "evidence" | "content_reference";

export interface ImportJob {
  id: string;
  owner_user_id: string;
  status: ImportStatus;
  filename: string;
  file_sha256: string;
  created_at: string;
  updated_at: string;
  counts: ImportCounts;
  row_diagnostics: ImportRowDiagnostic[];
  entity_kinds: ImportEntityKind[];
  mapping: Record<string, string | null>;
  on_duplicate: "skip" | "update";
  match_by: "isbn" | "isbn_and_edition" | "title_author_fuzzy";
}

export interface ImportPage {
  items: ImportJob[];
  next_cursor: string | null;
}

export interface CreativeProjectCreate {
  book_id: string;
  promotion_candidate_id: string;
  target_audience: string;
  target_platform: Platform;
  desired_seconds: number;
  original_angle: string;
  tone_instruction?: string | null;
  prohibited_claims: string[];
  evidence_ids: string[];
}

export interface CreativeProject extends CreativeProjectCreate {
  id: string;
  owner_user_id: string;
  created_at: string;
}

export interface CreativeProjectPage {
  items: CreativeProject[];
  next_cursor: string | null;
}

export interface ScriptSegment {
  order: number;
  onscreen_text: string;
  narration: string;
  fact_keys: string[];
  duration_seconds: number;
}

export interface ScriptVersionCreate {
  based_on_version_id: string | null;
  segments: ScriptSegment[];
  title_suggestion: string;
  description_suggestion: string;
}

export interface ScriptVersion extends ScriptVersionCreate {
  id: string;
  project_id: string;
  owner_user_id: string;
  version_number: number;
  review_status: ReviewStatus;
  generation_result_id?: string | null;
  fact_revision_refs?: { key: string; revision: number }[];
  created_at: string;
}

export interface RunCreate {
  brief_version: number;
}

export interface Run {
  id: string;
  project_id: string;
  owner_user_id: string;
  status: RunStatus;
  stage: RunStage;
  sequence: number;
  attempt: number;
  cancel_requested_at: string | null;
  error_code: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
}

export interface RunEvent {
  event_id: string;
  run_id: string;
  project_id: string;
  sequence: number;
  type: "run.created" | "run.stage_changed" | "run.review_requested" | "run.reviewed" | "run.failed" | "run.completed" | "run.cancel_requested" | "run.cancelled" | "run.retry_requested" | "run.generation_completed" | "run.generation_failed" | "run.cost_recorded";
  status: RunStatus;
  stage: RunStage;
  occurred_at: string;
  attempt?: number;
  generation_result_id?: string;
  cost_record_id?: string;
  script_version_id: string | null;
  render_id: string | null;
  reason_code: string | null;
  message: string | null;
}

export interface ReviewRequest {
  script_version_id: string;
  decision: "approve" | "reject";
  note: string | null;
  fact_confirmed: boolean;
  product_match_confirmed: boolean;
  asset_rights_confirmed: boolean;
}

export interface AssetRights {
  origin: "self_created_text" | "self_created_graphic" | "self_recorded_voice" | "licensed";
  usage_status: "allowed" | "blocked";
  license_basis: string | null;
  permitted_uses: string[];
  proof_ref: string | null;
  expires_at: string | null;
}

export interface RenderCreate {
  project_id: string;
  script_version_id: string;
  promotion_candidate_id: string;
  width: 1080;
  height: 1920;
  audio_asset_id: string | null;
  asset_rights: AssetRights[];
}

export interface RenderJob {
  id: string;
  project_id: string;
  script_version_id: string;
  audio_asset_id?: string | null;
  owner_user_id: string;
  status: RunStatus;
  attempt: number;
  cancel_requested_at: string | null;
  output_ref: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
}

export interface AudioAsset {
  id: string;
  owner_user_id: string;
  origin: "self_recorded_voice" | "licensed_voice";
  sha256: string;
  mime_type: "audio/wav" | "audio/mpeg" | "audio/mp4";
  duration_ms: number;
  rights_attested: boolean;
  rights_proof_note: string;
  permitted_uses: "render_and_manual_publish";
  expires_at: string | null;
  usage_status: "allowed" | "blocked";
  created_at: string;
}

export interface GenerationResult {
  id: string;
  run_id: string;
  owner_user_id: string;
  attempt: number;
  invocation_key: string;
  source: "deterministic_fixture" | "external_model";
  provider_key: string;
  model_key: string;
  status: "succeeded" | "failed";
  reason_code: string | null;
  input_digest: string;
  output_digest: string | null;
  script_version_id: string | null;
  created_at: string;
}

export interface ModelCostRecord {
  id: string;
  run_id: string;
  owner_user_id: string;
  generation_result_id: string;
  currency: string;
  amount_minor: number | null;
  missing_reason: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  recorded_at: string;
}

export interface CreativeWorkspace {
  project: CreativeProject;
  book: BookEdition;
  promotion_candidate: PromotionCandidate;
  evidence: Evidence[];
  script_versions: ScriptVersion[];
  runs: Run[];
  render_jobs: RenderJob[];
  publication_package: PublicationPackage | null;
}

export interface PageOptions {
  limit?: number;
  cursor?: string | null;
}

export interface PublicationPackage {
  id: string;
  project_id: string;
  book_id: string;
  script_version_id: string;
  promotion_candidate_id: string;
  render_id: string;
  title: string;
  description: string;
  cover_text?: string;
  video_download_url: string;
  publication_state: "needs_manual_checks" | "ready_for_manual_publish";
  checklist: {
    account_eligible: boolean;
    product_eligible: boolean;
    edition_matches: boolean;
    rights_confirmed: boolean;
    manual_attachment_required: true;
  };
}

export interface ApiErrorBody {
  code?: string;
  message?: string;
  request_id?: string;
  details?: Record<string, unknown>;
}

export class ApiRequestError extends Error {
  readonly status: number;
  readonly code: string | null;
  readonly requestId: string | null;
  readonly details: Record<string, unknown> | null;

  constructor(message: string, status: number, code: string | null, requestId: string | null, details: Record<string, unknown> | null = null) {
    super(message);
    this.name = "ApiRequestError";
    this.status = status;
    this.code = code;
    this.requestId = requestId;
    this.details = details;
  }
}

function idempotencyKey(): string {
  return globalThis.crypto?.randomUUID?.() ?? `web-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export class BookVideoApi {
  readonly baseUrl: string;

  constructor(baseUrl: string) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
  }

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    let response: Response;
    try {
      response = await fetch(`${this.baseUrl}${path}`, {
        ...init,
        headers: {
          Accept: "application/json",
          ...(init.body && !(init.body instanceof FormData) ? { "Content-Type": "application/json" } : {}),
          ...init.headers,
        },
      });
    } catch {
      throw new ApiRequestError(
        `无法连接到 ${this.baseUrl}。请确认 API 已启动，并检查 VITE_API_BASE_URL。`,
        0,
        null,
        null,
      );
    }

    const payload = (await response.json().catch(() => null)) as ApiErrorBody | T | null;
    if (!response.ok) {
      const error = payload as ApiErrorBody | null;
      const message = error?.message ?? `API 请求失败（HTTP ${response.status}）`;
      throw new ApiRequestError(message, response.status, error?.code ?? null, error?.request_id ?? null, error?.details ?? null);
    }
    return payload as T;
  }

  private async requestBlob(path: string): Promise<Blob> {
    let response: Response;
    try {
      response = await fetch(`${this.baseUrl}${path}`, { headers: { Accept: "video/mp4" } });
    } catch {
      throw new ApiRequestError(
        `无法连接到 ${this.baseUrl}。请确认 API 已启动，并检查 VITE_API_BASE_URL。`,
        0,
        null,
        null,
      );
    }
    if (!response.ok) {
      const error = (await response.json().catch(() => null)) as ApiErrorBody | null;
      throw new ApiRequestError(
        error?.message ?? `视频下载失败（HTTP ${response.status}）`,
        response.status,
        error?.code ?? null,
        error?.request_id ?? null,
        error?.details ?? null,
      );
    }
    return response.blob();
  }

  createBook(input: BookCreate): Promise<BookEdition> {
    return this.request("/books", { method: "POST", body: JSON.stringify(input) });
  }

  reviseBookFact(bookId: string, factKey: string, input: BookFactRevisionRequest): Promise<BookFactRevision> {
    return this.request(`/books/${encodeURIComponent(bookId)}/facts/${encodeURIComponent(factKey)}`, { method: "PATCH", body: JSON.stringify(input) });
  }

  listBookFactRevisions(bookId: string, factKey: string): Promise<BookFactRevision[]> {
    return this.request(`/books/${encodeURIComponent(bookId)}/facts/${encodeURIComponent(factKey)}/revisions`);
  }

  listBooks(options: PageOptions & { q?: string } = {}): Promise<BookPage> {
    const query = new URLSearchParams({ limit: String(options.limit ?? 20) });
    if (options.cursor) query.set("cursor", options.cursor);
    if (options.q?.trim()) query.set("q", options.q.trim());
    return this.request(`/books?${query.toString()}`);
  }

  createPromotionCandidate(input: PromotionCandidateCreate): Promise<PromotionCandidate> {
    return this.request("/promotion-candidates", { method: "POST", body: JSON.stringify(input) });
  }

  createEvidence(input: EvidenceCreate): Promise<Evidence> {
    return this.request("/evidence", { method: "POST", body: JSON.stringify(input) });
  }

  listEvidence(options: PageOptions & { source_type?: "manual" | "licensed"; q?: string } = {}): Promise<EvidencePage> {
    const query = new URLSearchParams({ limit: String(options.limit ?? 20) });
    if (options.cursor) query.set("cursor", options.cursor);
    if (options.source_type) query.set("source_type", options.source_type);
    if (options.q?.trim()) query.set("q", options.q.trim());
    return this.request(`/evidence?${query.toString()}`);
  }

  listMetrics(options: PageOptions & { platform?: Platform; object_type?: ObjectType; metric_name?: MetricName; basis?: MetricBasis; book_id?: string } = {}): Promise<MetricObservationPage> {
    const query = new URLSearchParams({ limit: String(options.limit ?? 20) });
    if (options.cursor) query.set("cursor", options.cursor);
    if (options.platform) query.set("platform", options.platform);
    if (options.object_type) query.set("object_type", options.object_type);
    if (options.metric_name) query.set("metric_name", options.metric_name);
    if (options.basis) query.set("basis", options.basis);
    if (options.book_id) query.set("book_id", options.book_id);
    return this.request(`/metrics?${query.toString()}`);
  }

  createMetric(input: MetricObservationCreate): Promise<MetricObservation> {
    return this.request("/metrics", { method: "POST", body: JSON.stringify(input) });
  }

  getMetric(observationId: string): Promise<MetricObservation> {
    return this.request(`/metrics/${encodeURIComponent(observationId)}`);
  }

  listContentReferences(options: PageOptions & { platform?: Platform; ref_type?: ObjectType; book_id?: string } = {}): Promise<ContentReferencePage> {
    const query = new URLSearchParams({ limit: String(options.limit ?? 20) });
    if (options.cursor) query.set("cursor", options.cursor);
    if (options.platform) query.set("platform", options.platform);
    if (options.ref_type) query.set("ref_type", options.ref_type);
    if (options.book_id) query.set("book_id", options.book_id);
    return this.request(`/content-references?${query.toString()}`);
  }

  createContentReference(input: ContentReferenceCreate): Promise<ContentReference> {
    return this.request("/content-references", { method: "POST", body: JSON.stringify(input) });
  }

  getContentReference(referenceId: string): Promise<ContentReference> {
    return this.request(`/content-references/${encodeURIComponent(referenceId)}`);
  }

  listStyleCards(options: PageOptions & { review_status?: StyleCardStatus; target_platform?: Platform; book_id?: string } = {}): Promise<StyleCardPage> {
    const query = new URLSearchParams({ limit: String(options.limit ?? 20) });
    if (options.cursor) query.set("cursor", options.cursor);
    if (options.review_status) query.set("review_status", options.review_status);
    if (options.target_platform) query.set("target_platform", options.target_platform);
    if (options.book_id) query.set("book_id", options.book_id);
    return this.request(`/style-cards?${query.toString()}`);
  }

  createStyleCard(input: StyleCardCreate): Promise<StyleCard> {
    return this.request("/style-cards", { method: "POST", body: JSON.stringify(input) });
  }

  getStyleCard(styleCardId: string): Promise<StyleCard> {
    return this.request(`/style-cards/${encodeURIComponent(styleCardId)}`);
  }

  updateStyleCard(styleCardId: string, input: StyleCardUpdate): Promise<StyleCard> {
    return this.request(`/style-cards/${encodeURIComponent(styleCardId)}`, { method: "PATCH", body: JSON.stringify(input) });
  }

  reviewStyleCard(styleCardId: string, input: StyleCardReview): Promise<StyleCard> {
    return this.request(`/style-cards/${encodeURIComponent(styleCardId)}/review`, { method: "POST", body: JSON.stringify(input) });
  }

  listImports(options: PageOptions & { status?: ImportStatus } = {}): Promise<ImportPage> {
    const query = new URLSearchParams({ limit: String(options.limit ?? 20) });
    if (options.cursor) query.set("cursor", options.cursor);
    if (options.status) query.set("status", options.status);
    return this.request(`/imports?${query.toString()}`);
  }

  previewImport(file: File, filenameHint?: string): Promise<ImportPreview> {
    const body = new FormData();
    body.set("file", file);
    if (filenameHint) body.set("filename_hint", filenameHint);
    return this.request("/imports/preview", {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey() },
      body,
    });
  }

  getImport(importId: string): Promise<ImportJob> {
    return this.request(`/imports/${encodeURIComponent(importId)}`);
  }

  confirmImport(importId: string, input: ImportConfirm): Promise<ImportJob> {
    return this.request(`/imports/${encodeURIComponent(importId)}/confirm`, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey() },
      body: JSON.stringify(input),
    });
  }

  createProject(input: CreativeProjectCreate): Promise<CreativeProject> {
    return this.request("/creative-projects", { method: "POST", body: JSON.stringify(input) });
  }

  listCreativeProjects(options: PageOptions & { bookId?: string } = {}): Promise<CreativeProjectPage> {
    const query = new URLSearchParams({ limit: String(options.limit ?? 20) });
    if (options.cursor) query.set("cursor", options.cursor);
    if (options.bookId) query.set("book_id", options.bookId);
    return this.request(`/creative-projects?${query.toString()}`);
  }

  getCreativeWorkspace(projectId: string): Promise<CreativeWorkspace> {
    return this.request(`/creative-projects/${encodeURIComponent(projectId)}/workspace`);
  }

  updatePromotionEligibility(candidateId: string, input: PromotionEligibilityUpdate): Promise<PromotionCandidate> {
    return this.request(`/promotion-candidates/${encodeURIComponent(candidateId)}/eligibility`, {
      method: "PATCH",
      body: JSON.stringify(input),
    });
  }

  cancelRun(runId: string, input: CancelRequest): Promise<Run> {
    return this.request(`/runs/${encodeURIComponent(runId)}/cancel`, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey() },
      body: JSON.stringify(input),
    });
  }

  retryRun(runId: string): Promise<Run> {
    return this.request(`/runs/${encodeURIComponent(runId)}/retry`, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey() },
    });
  }

  listScripts(projectId: string): Promise<ScriptVersion[]> {
    return this.request(`/creative-projects/${encodeURIComponent(projectId)}/scripts`);
  }

  saveScript(projectId: string, input: ScriptVersionCreate): Promise<ScriptVersion> {
    return this.request(`/creative-projects/${encodeURIComponent(projectId)}/scripts`, {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  startRun(projectId: string, input: RunCreate): Promise<Run> {
    return this.request(`/creative-projects/${encodeURIComponent(projectId)}/runs`, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey() },
      body: JSON.stringify(input),
    });
  }

  getRun(runId: string): Promise<Run> {
    return this.request(`/runs/${encodeURIComponent(runId)}`);
  }

  listGenerationResults(runId: string): Promise<GenerationResult[]> {
    return this.request(`/runs/${encodeURIComponent(runId)}/generation-results`);
  }

  listRunCosts(runId: string): Promise<ModelCostRecord[]> {
    return this.request(`/runs/${encodeURIComponent(runId)}/costs`);
  }

  listAudioAssets(): Promise<AudioAsset[]> {
    return this.request("/audio-assets");
  }

  getAudioAsset(audioAssetId: string): Promise<AudioAsset> {
    return this.request(`/audio-assets/${encodeURIComponent(audioAssetId)}`);
  }

  uploadAudioAsset(input: { file: File; sha256: string; rights_proof_note: string; expires_at: string | null }): Promise<AudioAsset> {
    const body = new FormData();
    body.set("file", input.file);
    body.set("sha256", input.sha256);
    body.set("origin", "self_recorded_voice");
    body.set("rights_attested", "true");
    body.set("rights_proof_note", input.rights_proof_note);
    body.set("permitted_uses", "render_and_manual_publish");
    if (input.expires_at) body.set("expires_at", input.expires_at);
    return this.request("/audio-assets", { method: "POST", body });
  }

  subscribeToRunEvents(
    runId: string,
    onEvent: (event: RunEvent) => void,
    onError: () => void,
  ): { close: () => void } {
    let closed = false;
    let lastSequence = 0;
    let controller: AbortController | null = null;
    let reconnectTimer: number | null = null;

    const connect = async () => {
      if (closed) return;
      controller = new AbortController();
      try {
        const headers = new Headers({ Accept: "text/event-stream" });
        if (lastSequence > 0) headers.set("Last-Event-ID", String(lastSequence));
        const response = await fetch(`${this.baseUrl}/runs/${encodeURIComponent(runId)}/events`, {
          headers,
          signal: controller.signal,
        });
        if (!response.ok || !response.body) throw new Error("SSE 连接不可用");

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (!closed) {
          const chunk = await reader.read();
          if (chunk.done) break;
          buffer += decoder.decode(chunk.value, { stream: true }).replace(/\r\n/g, "\n");
          let boundary = buffer.indexOf("\n\n");
          while (boundary >= 0) {
            const frame = buffer.slice(0, boundary);
            buffer = buffer.slice(boundary + 2);
            const data = frame.split("\n")
              .filter((line) => line.startsWith("data:"))
              .map((line) => line.slice(5).trimStart())
              .join("\n");
            if (data) {
              try {
                const event = JSON.parse(data) as RunEvent;
                if (event.sequence > lastSequence) {
                  lastSequence = event.sequence;
                  onEvent(event);
                }
              } catch {
                onError();
              }
            }
            boundary = buffer.indexOf("\n\n");
          }
        }
      } catch {
        if (!closed) onError();
      } finally {
        if (!closed) reconnectTimer = window.setTimeout(() => void connect(), 1500);
      }
    };

    void connect();
    return {
      close: () => {
        closed = true;
        controller?.abort();
        if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
      },
    };
  }

  reviewRun(runId: string, input: ReviewRequest): Promise<Run> {
    return this.request(`/runs/${encodeURIComponent(runId)}/review`, {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  startRender(input: RenderCreate): Promise<RenderJob> {
    return this.request("/renders", {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey() },
      body: JSON.stringify(input),
    });
  }

  getRender(renderId: string): Promise<RenderJob> {
    return this.request(`/renders/${encodeURIComponent(renderId)}`);
  }

  cancelRender(renderId: string, input: CancelRequest): Promise<RenderJob> {
    return this.request(`/renders/${encodeURIComponent(renderId)}/cancel`, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey() },
      body: JSON.stringify(input),
    });
  }

  retryRender(renderId: string): Promise<RenderJob> {
    return this.request(`/renders/${encodeURIComponent(renderId)}/retry`, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey() },
    });
  }

  downloadRender(renderId: string): Promise<Blob> {
    return this.requestBlob(`/renders/${encodeURIComponent(renderId)}/download`);
  }

  getPublicationPackage(projectId: string): Promise<PublicationPackage> {
    return this.request(`/creative-projects/${encodeURIComponent(projectId)}/publication-package`);
  }
}

export const DEFAULT_API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000/api/v1";
