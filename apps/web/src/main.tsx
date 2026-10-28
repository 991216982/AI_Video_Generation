import { createRoot } from "react-dom/client";
import { useEffect, useMemo, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import {
  ApiRequestError,
  BookVideoApi,
  DEFAULT_API_BASE_URL,
} from "./api";
import type {
  BookCreate,
  BookEdition,
  CreativeProject,
  CreativeProjectCreate,
  Evidence,
  EvidenceCreate,
  Platform,
  PromotionCandidate,
  PromotionCandidateCreate,
  PublicationPackage,
  RenderJob,
  ReviewRequest,
  Run,
  RunEvent,
  ScriptSegment,
  ScriptVersion,
  ScriptVersionCreate,
} from "./api";
import "./styles.css";

type Mode = "demo" | "api";
type Step = "source" | "brief" | "script" | "review" | "package";

interface BookDraft {
  title: string;
  editionLabel: string;
  author: string;
  isbn: string;
  publisher: string;
  originalOpinion: string;
  factKey: string;
  factValue: string;
  factStatus: "unverified" | "source_verified" | "human_confirmed";
  factSourceNote: string;
}

interface CandidateDraft {
  platform: Platform;
  productId: string;
  productUrl: string;
  eligibilityNote: string;
}

interface EvidenceDraft {
  sourceNote: string;
  sourceUrl: string;
  usageBasis: string;
  metricName: string;
  metricValue: string;
  observedAt: string;
  windowStart: string;
  windowEnd: string;
  missingReason: string;
}

interface BriefDraft {
  audience: string;
  platform: Platform;
  seconds: number;
  angle: string;
  prohibitedClaims: string;
}

const EMPTY_BOOK: BookDraft = {
  title: "",
  editionLabel: "",
  author: "",
  isbn: "",
  publisher: "",
  originalOpinion: "",
  factKey: "title",
  factValue: "",
  factStatus: "unverified",
  factSourceNote: "",
};

const EMPTY_CANDIDATE: CandidateDraft = {
  platform: "douyin",
  productId: "",
  productUrl: "",
  eligibilityNote: "",
};

const EMPTY_EVIDENCE: EvidenceDraft = {
  sourceNote: "",
  sourceUrl: "",
  usageBasis: "",
  metricName: "",
  metricValue: "",
  observedAt: "",
  windowStart: "",
  windowEnd: "",
  missingReason: "未取得获授权的平台数据；未记录销量指标。",
};

const EMPTY_BRIEF: BriefDraft = {
  audience: "",
  platform: "douyin",
  seconds: 30,
  angle: "",
  prohibitedClaims: "疗效、保证结果、未核实销量",
};

const STEP_ITEMS: { id: Step; number: string; label: string; caption: string }[] = [
  { id: "source", number: "01", label: "选书与商品", caption: "版本、出处、拟挂商品" },
  { id: "brief", number: "02", label: "创作 Brief", caption: "受众、角度与证据" },
  { id: "script", number: "03", label: "脚本版本", caption: "生成、查看与改稿" },
  { id: "review", number: "04", label: "人工审核", caption: "事实、商品与素材" },
  { id: "package", number: "05", label: "发布包", caption: "成片与手动核对卡" },
];

function makeId(): string {
  return globalThis.crypto?.randomUUID?.() ?? `demo-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function nowIso(): string {
  return new Date().toISOString();
}

function dateInputToIso(value: string): string | null {
  if (!value) return null;
  const parsed = new Date(`${value}T00:00:00.000Z`);
  return Number.isNaN(parsed.getTime()) ? null : parsed.toISOString();
}

function shortId(value: string | undefined): string {
  if (!value) return "—";
  return value.length > 17 ? `${value.slice(0, 8)}…${value.slice(-5)}` : value;
}

function platformName(platform: Platform): string {
  return platform === "douyin" ? "抖音" : "快手";
}

function statusName(status: Run["status"]): string {
  const names: Record<Run["status"], string> = {
    queued: "排队中",
    running: "处理中",
    waiting_review: "等待人工审核",
    succeeded: "已完成",
    failed: "失败",
    cancelled: "已取消",
  };
  return names[status];
}

function stageName(stage: Run["stage"]): string {
  const names: Record<Run["stage"], string> = {
    prepare: "准备",
    script: "脚本",
    fact_check: "事实核对",
    review: "人工审核",
    storyboard: "分镜",
    render: "渲染",
    complete: "完成",
  };
  return names[stage];
}

function formatTime(value: string | undefined): string {
  if (!value) return "尚无记录";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? value
    : new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(parsed);
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiRequestError) {
    const suffix = error.requestId ? `（请求编号 ${error.requestId}）` : "";
    return `${error.message}${suffix}`;
  }
  if (error instanceof Error) return error.message;
  return "操作未完成，请检查输入后重试。";
}

function makeEvent(run: Run, sequence: number, type: RunEvent["type"], message: string): RunEvent {
  return {
    event_id: makeId(),
    run_id: run.id,
    project_id: run.project_id,
    sequence,
    type,
    status: run.status,
    stage: run.stage,
    occurred_at: nowIso(),
    script_version_id: null,
    render_id: null,
    reason_code: null,
    message,
  };
}

function demoScriptVersions(project: CreativeProject, book: BookEdition, angle: string): ScriptVersion[] {
  const first: ScriptSegment[] = [
    {
      order: 0,
      onscreen_text: "把日子写成地图",
      narration: "如果把生活画成一张地图，你会先标出哪里？",
      fact_keys: [],
      duration_seconds: 8,
    },
    {
      order: 1,
      onscreen_text: "从一个小选择开始",
      narration: `我喜欢用地图比喻日常选择。${angle}`,
      fact_keys: [],
      duration_seconds: 12,
    },
    {
      order: 2,
      onscreen_text: "你会从哪里落笔？",
      narration: "今天先记下一件想做的小事，再看看下一步会通向哪里。",
      fact_keys: [],
      duration_seconds: 10,
    },
  ];
  const second: ScriptSegment[] = [
    {
      order: 0,
      onscreen_text: "先画一条很短的路",
      narration: "改变计划不一定要从很远的地方开始。",
      fact_keys: [],
      duration_seconds: 7,
    },
    {
      order: 1,
      onscreen_text: "把选择写在纸上",
      narration: `这个虚构演示把日常想成地图：${angle}`,
      fact_keys: [],
      duration_seconds: 13,
    },
    {
      order: 2,
      onscreen_text: "留一个位置给自己",
      narration: "如果由你来画，第一站会写什么？",
      fact_keys: [],
      duration_seconds: 10,
    },
  ];
  return [
    {
      id: makeId(),
      project_id: project.id,
      owner_user_id: project.owner_user_id,
      version_number: 1,
      based_on_version_id: null,
      segments: first,
      title_suggestion: `${book.title}：从一个小选择开始`,
      description_suggestion: "虚构演示脚本，仅展示原创表达结构；不是事实核验或商品推荐结论。",
      review_status: "pending",
      created_at: nowIso(),
    },
    {
      id: makeId(),
      project_id: project.id,
      owner_user_id: project.owner_user_id,
      version_number: 2,
      based_on_version_id: null,
      segments: second,
      title_suggestion: "先画一条很短的路｜虚构演示稿",
      description_suggestion: "演示用的第二种原创叙述角度，不包含真实销量、疗效或未经核实事实。",
      review_status: "pending",
      created_at: nowIso(),
    },
  ];
}

function App() {
  const [mode, setMode] = useState<Mode>("demo");
  const [activeStep, setActiveStep] = useState<Step>("source");
  const [apiBaseUrl, setApiBaseUrl] = useState(DEFAULT_API_BASE_URL);
  const api = useMemo(() => new BookVideoApi(apiBaseUrl), [apiBaseUrl]);
  const [isSampleLoaded, setIsSampleLoaded] = useState(false);
  const [bookDraft, setBookDraft] = useState<BookDraft>(EMPTY_BOOK);
  const [candidateDraft, setCandidateDraft] = useState<CandidateDraft>(EMPTY_CANDIDATE);
  const [evidenceDraft, setEvidenceDraft] = useState<EvidenceDraft>(EMPTY_EVIDENCE);
  const [briefDraft, setBriefDraft] = useState<BriefDraft>(EMPTY_BRIEF);
  const [manualCandidateConfirmation, setManualCandidateConfirmation] = useState(false);

  const [book, setBook] = useState<BookEdition | null>(null);
  const [candidate, setCandidate] = useState<PromotionCandidate | null>(null);
  const [evidence, setEvidence] = useState<Evidence | null>(null);
  const [project, setProject] = useState<CreativeProject | null>(null);
  const [versions, setVersions] = useState<ScriptVersion[]>([]);
  const [selectedVersionId, setSelectedVersionId] = useState("");
  const selectedVersion = versions.find((version) => version.id === selectedVersionId) ?? null;
  const [editorTitle, setEditorTitle] = useState("");
  const [editorDescription, setEditorDescription] = useState("");
  const [editorSegments, setEditorSegments] = useState<ScriptSegment[]>([]);

  const [run, setRun] = useState<Run | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [runStreamWarning, setRunStreamWarning] = useState("");
  const [reviewDecision, setReviewDecision] = useState<"approve" | "reject">("approve");
  const [reviewNote, setReviewNote] = useState("");
  const [reviewFlags, setReviewFlags] = useState({ fact: false, product: false, rights: false });

  const [renderRightsConfirmed, setRenderRightsConfirmed] = useState(false);
  const [renderJob, setRenderJob] = useState<RenderJob | null>(null);
  const [publicationPackage, setPublicationPackage] = useState<PublicationPackage | null>(null);
  const [packagePreviewShown, setPackagePreviewShown] = useState(false);

  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");

  useEffect(() => {
    if (!selectedVersion) return;
    setEditorTitle(selectedVersion.title_suggestion);
    setEditorDescription(selectedVersion.description_suggestion);
    setEditorSegments(selectedVersion.segments.map((segment, index) => ({ ...segment, order: index })));
  }, [selectedVersion?.id]);

  useEffect(() => {
    if (!selectedVersionId && versions[0]) setSelectedVersionId(versions[0].id);
  }, [selectedVersionId, versions]);

  useEffect(() => {
    if (mode !== "api" || !run?.id) return;
    const runId = run.id;
    const projectId = project?.id;
    const stream = api.subscribeToRunEvents(
      runId,
      (event) => {
        setEvents((existing) => {
          if (existing.some((item) => item.run_id === event.run_id && item.sequence === event.sequence)) return existing;
          return [...existing, event].sort((left, right) => left.sequence - right.sequence);
        });
        setRun((current) => {
          if (!current || current.id !== event.run_id || event.sequence < current.sequence) return current;
          return { ...current, status: event.status, stage: event.stage, sequence: event.sequence, updated_at: event.occurred_at };
        });
      },
      () => setRunStreamWarning("实时事件暂不可用，页面会继续轮询任务状态。"),
    );
    const timer = window.setInterval(async () => {
      try {
        const latest = await api.getRun(runId);
        setRun(latest);
        if (projectId) setVersions(await api.listScripts(projectId));
      } catch (requestError) {
        setError(errorMessage(requestError));
      }
    }, 3000);
    return () => {
      stream.close();
      window.clearInterval(timer);
    };
  }, [api, mode, run?.id, run?.status, project?.id]);

  useEffect(() => {
    if (mode !== "api" || !renderJob?.id || ["succeeded", "failed", "cancelled"].includes(renderJob.status)) return;
    const renderId = renderJob.id;
    const timer = window.setInterval(async () => {
      try {
        setRenderJob(await api.getRender(renderId));
      } catch (requestError) {
        setError(errorMessage(requestError));
      }
    }, 3000);
    return () => window.clearInterval(timer);
  }, [api, mode, renderJob?.id, renderJob?.status]);

  useEffect(() => {
    if (mode !== "api" || !project?.id) return;
    let active = true;
    api.listScripts(project.id).then((items) => {
      if (active) setVersions(items);
    }).catch((requestError: unknown) => {
      if (active) setError(errorMessage(requestError));
    });
    return () => { active = false; };
  }, [api, mode, project?.id]);

  function clearSession() {
    setBookDraft(EMPTY_BOOK);
    setCandidateDraft(EMPTY_CANDIDATE);
    setEvidenceDraft(EMPTY_EVIDENCE);
    setBriefDraft(EMPTY_BRIEF);
    setManualCandidateConfirmation(false);
    setBook(null);
    setCandidate(null);
    setEvidence(null);
    setProject(null);
    setVersions([]);
    setSelectedVersionId("");
    setEditorTitle("");
    setEditorDescription("");
    setEditorSegments([]);
    setRun(null);
    setEvents([]);
    setRunStreamWarning("");
    setReviewDecision("approve");
    setReviewNote("");
    setReviewFlags({ fact: false, product: false, rights: false });
    setRenderRightsConfirmed(false);
    setRenderJob(null);
    setPublicationPackage(null);
    setPackagePreviewShown(false);
    setIsSampleLoaded(false);
    setActiveStep("source");
    setError("");
    setSuccess("");
  }

  function changeMode(nextMode: Mode) {
    if (nextMode === mode) return;
    if (isSampleLoaded || book || candidate || project) clearSession();
    setMode(nextMode);
    setError("");
    setSuccess(nextMode === "api" ? "已切换到 API 模式。保存操作将发送至当前 API 基址。" : "已切换到本地演示模式。演示数据不会发送到 API。");
  }

  function loadContractSample() {
    clearSession();
    setMode("demo");
    setIsSampleLoaded(true);
    setBookDraft({
      title: "把日子写成地图",
      editionLabel: "虚构演示版 1.0",
      author: "林例",
      isbn: "",
      publisher: "",
      originalOpinion: "我喜欢用地图比喻日常选择；这本虚构样书仅用于演示原创观点如何进入脚本。",
      factKey: "title",
      factValue: "把日子写成地图",
      factStatus: "human_confirmed",
      factSourceNote: "虚构演示数据，非真实图书",
    });
    setCandidateDraft({
      platform: "douyin",
      productId: "DEMO-NOT-REAL",
      productUrl: "https://example.invalid/demo-product",
      eligibilityNote: "演示占位，不可发布或挂车",
    });
    setEvidenceDraft({
      sourceNote: "创作者手工输入的虚构示例",
      sourceUrl: "",
      usageBasis: "仅限本地开发测试",
      metricName: "",
      metricValue: "",
      observedAt: "2026-10-28",
      windowStart: "",
      windowEnd: "",
      missingReason: "未接入获授权的数据源；未提供销量数据。",
    });
    setBriefDraft({
      audience: "喜欢个人规划的成年读者",
      platform: "douyin",
      seconds: 30,
      angle: "用自制地图图形表达一次小选择，邀请观众分享自己的方法。",
      prohibitedClaims: "真实销量、疗效、保证改变人生",
    });
    setSuccess("已载入契约中的虚构人工样例。它没有真实商品或发布资格，操作只保存在本地演示状态。");
    setActiveStep("source");
  }

  function createBookPayload(): BookCreate {
    return {
      isbn: bookDraft.isbn.trim() || null,
      title: bookDraft.title.trim(),
      edition_label: bookDraft.editionLabel.trim(),
      author: bookDraft.author.trim(),
      publisher: bookDraft.publisher.trim() || null,
      original_opinion: bookDraft.originalOpinion.trim(),
      facts: [{
        key: bookDraft.factKey.trim(),
        value: bookDraft.factValue.trim(),
        verification_status: bookDraft.factStatus,
        source_note: bookDraft.factSourceNote.trim() || null,
        evidence_id: null,
      }],
    };
  }

  async function saveBook(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy("book");
    setError("");
    try {
      const payload = createBookPayload();
      const created: BookEdition = mode === "api"
        ? await api.createBook(payload)
        : { ...payload, id: makeId(), owner_user_id: makeId(), created_at: nowIso() };
      setBook(created);
      setCandidate(null);
      setProject(null);
      setEvidence(null);
      setVersions([]);
      setRun(null);
      setPublicationPackage(null);
      setSuccess(mode === "api" ? "图书版本已由 API 保存。" : "图书版本已保存在本地演示状态。未向后端发送数据。");
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setBusy(null);
    }
  }

  async function saveCandidate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!book) return;
    setBusy("candidate");
    setError("");
    try {
      const confirmed = mode === "api" && manualCandidateConfirmation;
      const payload: PromotionCandidateCreate = {
        book_id: book.id,
        platform: candidateDraft.platform,
        platform_product_id: candidateDraft.productId.trim(),
        product_url: candidateDraft.productUrl.trim(),
        eligibility_status: confirmed ? "manually_confirmed" : "unverified",
        eligibility_checked_at: confirmed ? nowIso() : null,
        eligibility_note: candidateDraft.eligibilityNote.trim()
          || (confirmed ? "创作者在平台客户端手动核对；非平台自动验证。" : null),
      };
      const created: PromotionCandidate = mode === "api"
        ? await api.createPromotionCandidate(payload)
        : { ...payload, eligibility_status: "unverified", eligibility_checked_at: null, id: makeId(), owner_user_id: makeId(), created_at: nowIso() };
      setCandidate(created);
      setProject(null);
      setVersions([]);
      setRun(null);
      setPublicationPackage(null);
      setSuccess(mode === "api"
        ? "拟挂商品已保存。平台资格仍以创作者本人在平台客户端核对为准。"
        : "虚构拟挂商品已保存在本地演示状态，资格保持未核实。");
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setBusy(null);
    }
  }

  function createEvidencePayload(): EvidenceCreate {
    const metricValue = evidenceDraft.metricValue.trim() ? Number(evidenceDraft.metricValue) : null;
    const completeMetric = Boolean(evidenceDraft.metricName.trim()) && metricValue !== null && Number.isFinite(metricValue);
    return {
      source_type: "manual",
      source_note: evidenceDraft.sourceNote.trim(),
      source_url: evidenceDraft.sourceUrl.trim() || null,
      observed_at: dateInputToIso(evidenceDraft.observedAt) ?? nowIso(),
      usage_basis: evidenceDraft.usageBasis.trim(),
      metric_name: evidenceDraft.metricName.trim() || null,
      metric_value: completeMetric ? metricValue : null,
      metric_window_start: dateInputToIso(evidenceDraft.windowStart),
      metric_window_end: dateInputToIso(evidenceDraft.windowEnd),
      missing_reason: completeMetric ? null : (evidenceDraft.missingReason.trim() || "未取得获授权的数据；未记录该指标。"),
    };
  }

  async function saveEvidence(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy("evidence");
    setError("");
    try {
      const payload = createEvidencePayload();
      const created: Evidence = mode === "api"
        ? await api.createEvidence(payload)
        : { ...payload, id: makeId(), owner_user_id: makeId(), collected_at: nowIso() };
      setEvidence(created);
      setSuccess(mode === "api" ? "人工证据来源已保存。" : "虚构人工证据已保存在本地演示状态。");
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setBusy(null);
    }
  }

  async function saveProject(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!book || !candidate) return;
    setBusy("project");
    setError("");
    try {
      const payload: CreativeProjectCreate = {
        book_id: book.id,
        promotion_candidate_id: candidate.id,
        target_audience: briefDraft.audience.trim(),
        target_platform: briefDraft.platform,
        desired_seconds: Number(briefDraft.seconds),
        original_angle: briefDraft.angle.trim(),
        prohibited_claims: briefDraft.prohibitedClaims.split(/[，,\n]/).map((claim) => claim.trim()).filter(Boolean),
        evidence_ids: evidence ? [evidence.id] : [],
      };
      const created: CreativeProject = mode === "api"
        ? await api.createProject(payload)
        : { ...payload, id: makeId(), owner_user_id: makeId(), created_at: nowIso() };
      setProject(created);
      setVersions([]);
      setRun(null);
      setEvents([]);
      setPublicationPackage(null);
      setSuccess(mode === "api" ? "创作 Brief 已保存。" : "创作 Brief 已保存在本地演示状态。");
      setActiveStep("script");
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setBusy(null);
    }
  }

  async function startGeneration() {
    if (!project || !book) return;
    setBusy("run");
    setError("");
    setRunStreamWarning("");
    try {
      if (mode === "api") {
        const created = await api.startRun(project.id, { brief_version: 1 });
        setRun(created);
        setEvents([]);
        setVersions(await api.listScripts(project.id));
        setSuccess("生成任务已提交。页面会读取 SSE 事件，并轮询当前任务状态。");
      } else {
        const created: Run = {
          id: makeId(),
          project_id: project.id,
          owner_user_id: project.owner_user_id,
          status: "waiting_review",
          stage: "review",
          sequence: 2,
          error_code: null,
          error_message: null,
          created_at: nowIso(),
          updated_at: nowIso(),
        };
        const sampleVersions = demoScriptVersions(project, book, briefDraft.angle);
        setRun(created);
        setVersions(sampleVersions);
        setSelectedVersionId(sampleVersions[0]?.id ?? "");
        setEvents([
          makeEvent({ ...created, status: "queued", stage: "prepare", sequence: 1 }, 1, "run.created", "虚构演示任务已在本地创建；没有调用模型或后端。"),
          makeEvent(created, 2, "run.review_requested", "演示脚本已生成，等待查看与人工审核。"),
        ]);
        setSuccess("已创建两版虚构原创脚本样稿。该本地演示没有调用模型或后端。");
      }
      setActiveStep("script");
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setBusy(null);
    }
  }

  function currentScriptPayload(): ScriptVersionCreate {
    return {
      based_on_version_id: selectedVersion?.id ?? null,
      segments: editorSegments.map((segment, index) => ({ ...segment, order: index })),
      title_suggestion: editorTitle.trim(),
      description_suggestion: editorDescription.trim(),
    };
  }

  async function saveScript(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!project || editorSegments.length === 0) return;
    if (editorSegments.some((segment) => !Number.isFinite(segment.duration_seconds) || segment.duration_seconds <= 0)) {
      setError("每段时长必须是大于 0 的数字。");
      return;
    }
    setBusy("script");
    setError("");
    try {
      const payload = currentScriptPayload();
      const saved: ScriptVersion = mode === "api"
        ? await api.saveScript(project.id, payload)
        : {
            ...payload,
            id: makeId(),
            project_id: project.id,
            owner_user_id: project.owner_user_id,
            version_number: Math.max(0, ...versions.map((version) => version.version_number)) + 1,
            review_status: "pending",
            created_at: nowIso(),
          };
      setVersions((existing) => [...existing, saved].sort((left, right) => left.version_number - right.version_number));
      setSelectedVersionId(saved.id);
      setSuccess(mode === "api" ? `脚本已另存为 v${saved.version_number}。` : `脚本样稿已另存为本地演示 v${saved.version_number}。`);
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setBusy(null);
    }
  }

  function eventLabel(event: RunEvent): string {
    const labels: Record<RunEvent["type"], string> = {
      "run.created": "任务创建",
      "run.stage_changed": "阶段变化",
      "run.review_requested": "请求审核",
      "run.reviewed": "审核记录",
      "run.failed": "任务失败",
      "run.completed": "任务完成",
      "run.cancelled": "任务取消",
    };
    return labels[event.type];
  }

  async function submitReview(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!run || !selectedVersion) return;
    const candidateIsManuallyConfirmed = candidate?.eligibility_status === "manually_confirmed";
    const factsHaveSources = Boolean(book?.facts.length && book.facts.every((fact) => fact.verification_status !== "unverified"));
    if (reviewDecision === "approve" && (!reviewFlags.fact || !reviewFlags.product || !reviewFlags.rights || !factsHaveSources || !candidateIsManuallyConfirmed)) {
      setError("暂不能提交通过：请先核对书籍事实、商品版本与素材权利，并在 API 保存拟挂商品时记录本人已在平台客户端完成的人工核对。示例商品资格未核实。 ");
      return;
    }
    setBusy("review");
    setError("");
    const payload: ReviewRequest = {
      script_version_id: selectedVersion.id,
      decision: reviewDecision,
      note: reviewNote.trim() || null,
      fact_confirmed: reviewFlags.fact,
      product_match_confirmed: reviewFlags.product,
      asset_rights_confirmed: reviewFlags.rights,
    };
    try {
      if (mode === "api") {
        const updated = await api.reviewRun(run.id, payload);
        setRun(updated);
        setVersions(await api.listScripts(project?.id ?? run.project_id));
        setSuccess("审核记录已提交到 API。审核与商品/素材资格仍由服务端校验。");
      } else {
        const reviewedVersion: ScriptVersion = {
          ...selectedVersion,
          review_status: reviewDecision === "approve" ? "approved" : "rejected",
        };
        setVersions((existing) => existing.map((version) => version.id === selectedVersion.id ? reviewedVersion : version));
        const eventRecord = makeEvent(run, run.sequence + 1, "run.reviewed", "仅本地演示记录了审核选择；不代表真实服务审核或发布资格。");
        setRun((current) => current ? { ...current, sequence: current.sequence + 1, updated_at: eventRecord.occurred_at } : current);
        setEvents((existing) => [...existing, eventRecord]);
        setSuccess("审核选择已记录在本地演示状态。它不会生成成片或改变平台资格。");
      }
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setBusy(null);
    }
  }

  async function startRender() {
    if (!project || !candidate || !selectedVersion || selectedVersion.review_status !== "approved") return;
    if (mode === "demo") {
      setError("本地演示不生成视频。请连接 API，由服务端检查已审核脚本、商品资格和素材权利后提交渲染。");
      return;
    }
    if (!renderRightsConfirmed) {
      setError("请先确认本次只使用自制文字与图形，或已取得明确许可的素材。");
      return;
    }
    if (candidate.eligibility_status !== "manually_confirmed") {
      setError("拟挂商品仍未记录平台客户端人工核对。渲染和发布包资格由服务端校验。");
      return;
    }
    setBusy("render");
    setError("");
    try {
      const created = await api.startRender({
        project_id: project.id,
        script_version_id: selectedVersion.id,
        promotion_candidate_id: candidate.id,
        width: 1080,
        height: 1920,
        asset_rights: [
          { origin: "self_created_text", usage_status: "allowed", license_basis: "创作者原创文字", permitted_uses: ["render", "manual_publication"], proof_ref: null, expires_at: null },
          { origin: "self_created_graphic", usage_status: "allowed", license_basis: "自制图形", permitted_uses: ["render", "manual_publication"], proof_ref: null, expires_at: null },
        ],
      });
      setRenderJob(created);
      setPublicationPackage(null);
      setPackagePreviewShown(false);
      setSuccess("渲染任务已提交。任务状态由 API 返回。");
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setBusy(null);
    }
  }

  async function loadPublicationPackage() {
    if (!project || !book || !candidate || !selectedVersion) return;
    setBusy("package");
    setError("");
    try {
      if (mode === "api") {
        const loaded = await api.getPublicationPackage(project.id);
        setPublicationPackage(loaded);
        setPackagePreviewShown(false);
        setSuccess("发布包元数据已从 API 加载。实际发布与挂车仍需创作者在平台客户端手动完成。");
      } else {
        const preview: PublicationPackage = {
          id: "LOCAL-DEMO-ONLY",
          render_id: "LOCAL-DEMO-ONLY",
          project_id: project.id,
          book_id: book.id,
          script_version_id: selectedVersion.id,
          promotion_candidate_id: candidate.id,
          title: selectedVersion.title_suggestion,
          description: selectedVersion.description_suggestion,
          cover_text: "本地演示核对卡；没有视频文件。",
          video_download_url: "",
          publication_state: "needs_manual_checks",
          checklist: {
            account_eligible: false,
            product_eligible: false,
            edition_matches: false,
            rights_confirmed: false,
            manual_attachment_required: true,
          },
        };
        setPublicationPackage(preview);
        setPackagePreviewShown(true);
        setSuccess("已生成本地演示核对卡。未生成视频，也没有确认账号、商品或素材资格。");
      }
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setBusy(null);
    }
  }

  async function downloadRenderedVideo() {
    if (!publicationPackage?.render_id || mode !== "api") return;
    setBusy("download");
    setError("");
    try {
      const video = await api.downloadRender(publicationPackage.render_id);
      const url = URL.createObjectURL(video);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `book-video-${publicationPackage.render_id.slice(0, 8)}.mp4`;
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      setSuccess("成片已下载。");
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setBusy(null);
    }
  }

  function downloadPackageJson() {
    if (!publicationPackage) return;
    const blob = new Blob([JSON.stringify(publicationPackage, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = mode === "demo" ? "demo-manual-publication-checklist.json" : "manual-publication-package.json";
    anchor.click();
    URL.revokeObjectURL(url);
  }

  const currentDuration = editorSegments.reduce((total, segment) => total + (Number(segment.duration_seconds) || 0), 0);
  const factsHaveSources = Boolean(book?.facts.length && book.facts.every((fact) => fact.verification_status !== "unverified"));
  const candidateIsManuallyConfirmed = candidate?.eligibility_status === "manually_confirmed";
  const reviewFlagsComplete = reviewFlags.fact && reviewFlags.product && reviewFlags.rights;
  const canApprove = reviewFlagsComplete && factsHaveSources && candidateIsManuallyConfirmed;
  const approvedVersion = versions.find((version) => version.review_status === "approved") ?? null;
  const selectedVersionApproved = selectedVersion?.review_status === "approved";

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand-lockup">
          <div className="brand-mark"><span /><span /><span /></div>
          <div>
            <strong>页间</strong>
            <small>原创图书短视频工作台</small>
          </div>
        </div>

        <div className="workspace-label">创作空间 <span className="tiny-dot" /></div>
        <nav className="step-nav" aria-label="创作流程">
          {STEP_ITEMS.map((item) => (
            <button
              className={`nav-step ${activeStep === item.id ? "is-active" : ""}`}
              key={item.id}
              onClick={() => setActiveStep(item.id)}
              type="button"
            >
              <span className="nav-number">{item.number}</span>
              <span className="nav-copy"><strong>{item.label}</strong><small>{item.caption}</small></span>
              <span className="nav-chevron">›</span>
            </button>
          ))}
        </nav>

        <div className="sidebar-spacer" />
        <div className="sidebar-data-note">
          <div className="note-icon">i</div>
          <div>
            <strong>授权数据未取得</strong>
            <p>不抓取平台，不推断销量。素材权限不明时不可用于成片。</p>
          </div>
        </div>
        <div className="sidebar-footer">
          <span className={`mode-dot ${mode}`} />
          <span>{mode === "demo" ? "本地虚构演示" : "API 工作区"}</span>
          <span className="version-chip">W1 · 01</span>
        </div>
      </aside>

      <main className="main-area">
        <header className="topbar">
          <div className="breadcrumb"><span>我的工作台</span><b>/</b><strong>{STEP_ITEMS.find((item) => item.id === activeStep)?.label}</strong></div>
          <div className="top-actions">
            <label className="mode-switch" aria-label="工作模式">
              <button className={mode === "demo" ? "selected" : ""} onClick={() => changeMode("demo")} type="button">演示</button>
              <button className={mode === "api" ? "selected" : ""} onClick={() => changeMode("api")} type="button">API</button>
            </label>
            <div className="avatar" aria-label="本人工作区">我</div>
          </div>
        </header>

        <div className="page-content">
          {mode === "demo" && (
            <div className="mode-banner demo-banner">
              <span className="banner-symbol">✳</span>
              <span><strong>本地演示模式</strong> 所有样例均为虚构数据，操作只保存在当前页面状态，不会生成视频或连接平台。</span>
              <button type="button" onClick={loadContractSample}>载入虚构样例 <span>↗</span></button>
            </div>
          )}
          {mode === "api" && (
            <div className="mode-banner api-banner">
              <span className="banner-symbol">↗</span>
              <span><strong>API 模式</strong> 保存操作会发送到你配置的服务；没有授权平台数据时请保留空值与缺失原因。</span>
              <label className="api-base-field">
                <span>API 基址</span>
                <input aria-label="API 基址" onChange={(event) => setApiBaseUrl(event.target.value)} value={apiBaseUrl} />
              </label>
            </div>
          )}

          {error && <div className="notice notice-error" role="alert"><span>!</span><div><strong>操作未完成</strong><p>{error}</p></div><button aria-label="关闭错误提示" onClick={() => setError("")} type="button">×</button></div>}
          {success && <div className="notice notice-success" role="status"><span>✓</span><p>{success}</p><button aria-label="关闭提示" onClick={() => setSuccess("")} type="button">×</button></div>}

          <section className="welcome-row">
            <div>
              <p className="eyebrow">原创内容 · 人工把关 · 手动发布</p>
              <h1>{activeStep === "source" ? "从一本书，开始一条新内容。" : STEP_ITEMS.find((item) => item.id === activeStep)?.label}</h1>
              <p className="welcome-copy">把版本信息、个人观点和人工核对放在一起，再把成片与检查卡交到你手里。</p>
            </div>
            <div className="welcome-meta">
              <div className="meta-mark"><div className="orbit orbit-one" /><div className="orbit orbit-two" /><span>页</span></div>
              <small>每一条内容<br />都从自己的观点开始</small>
            </div>
          </section>

          <section className="workflow-summary" aria-label="工作流状态">
            <SummaryCell label="图书版本" value={book ? book.title : "待录入"} detail={book ? book.edition_label : "保留具体版次"} icon="▤" />
            <SummaryCell label="拟挂商品" value={candidate ? (candidate.eligibility_status === "manually_confirmed" ? "已记录人工核对" : "未核实") : "待录入"} detail={candidate ? platformName(candidate.platform) : "资格由你在平台确认"} icon="◇" />
            <SummaryCell label="脚本版本" value={versions.length ? `${versions.length} 个版本` : "待创作"} detail={selectedVersion ? `当前 v${selectedVersion.version_number}` : "创作后可逐版修改"} icon="✎" />
            <SummaryCell label="任务状态" value={run ? statusName(run.status) : "未开始"} detail={run ? stageName(run.stage) : "等待提交创作"} icon="◷" />
          </section>

          <div className="workspace-grid">
            <section className="work-panel">
              {activeStep === "source" && (
                <>
                  <PanelHeading overline="01 / SOURCE" title="图书与拟挂商品" description="一条内容对应一个具体版本。图书事实由你提供并核实；商品资格不会自动判断。" />
                  <div className="form-section-title"><span className="section-index">A</span><div><h2>图书版本</h2><p>先把书的基本信息和你的原创观点记下来。</p></div>{book && <span className="saved-pill">已保存</span>}</div>
                  {book && <div className="saved-record"><span className="saved-icon">✓</span><div><strong>{book.title}</strong><p>{book.author} · {book.edition_label}</p></div><small>记录编号 {shortId(book.id)}</small></div>}
                  <form className="form-grid" onSubmit={saveBook}>
                    <Field label="书名" required><input disabled={Boolean(book)} onChange={(event) => setBookDraft({ ...bookDraft, title: event.target.value })} placeholder="输入书名" value={bookDraft.title} /></Field>
                    <Field label="具体版次" required><input disabled={Boolean(book)} onChange={(event) => setBookDraft({ ...bookDraft, editionLabel: event.target.value })} placeholder="如：2025 年修订版" value={bookDraft.editionLabel} /></Field>
                    <Field label="作者" required><input disabled={Boolean(book)} onChange={(event) => setBookDraft({ ...bookDraft, author: event.target.value })} placeholder="输入作者" value={bookDraft.author} /></Field>
                    <Field label="ISBN（可选）"><input disabled={Boolean(book)} onChange={(event) => setBookDraft({ ...bookDraft, isbn: event.target.value })} placeholder="978…" value={bookDraft.isbn} /></Field>
                    <Field label="出版社（可选）"><input disabled={Boolean(book)} onChange={(event) => setBookDraft({ ...bookDraft, publisher: event.target.value })} placeholder="输入出版社" value={bookDraft.publisher} /></Field>
                    <div className="field span-2"><label>你自己的荐书观点 <em>*</em></label><textarea disabled={Boolean(book)} onChange={(event) => setBookDraft({ ...bookDraft, originalOpinion: event.target.value })} placeholder="写下你为什么想聊这本书。不要粘贴未获许可的书籍内文。" required rows={3} value={bookDraft.originalOpinion} /></div>
                    <div className="field span-2 fact-entry">
                      <div className="field-row-title"><div><label>人工录入的一条事实</label><span className="field-hint">来源不清时保留“未核实”，不要带入成片。</span></div><span className="mini-tag">事实记录</span></div>
                      <div className="fact-grid">
                        <Field label="事实键"><input disabled={Boolean(book)} onChange={(event) => setBookDraft({ ...bookDraft, factKey: event.target.value })} value={bookDraft.factKey} /></Field>
                        <Field label="事实内容"><input disabled={Boolean(book)} onChange={(event) => setBookDraft({ ...bookDraft, factValue: event.target.value })} placeholder="例如：书名" required value={bookDraft.factValue} /></Field>
                        <Field label="核实状态"><select disabled={Boolean(book)} onChange={(event) => setBookDraft({ ...bookDraft, factStatus: event.target.value as BookDraft["factStatus"] })} value={bookDraft.factStatus}><option value="unverified">未核实</option><option value="source_verified">有来源核实</option><option value="human_confirmed">本人已确认</option></select></Field>
                      </div>
                      <Field label="来源或缺失说明"><input disabled={Boolean(book)} onChange={(event) => setBookDraft({ ...bookDraft, factSourceNote: event.target.value })} placeholder="来源、范围、时间窗，或为什么缺少" value={bookDraft.factSourceNote} /></Field>
                    </div>
                    <div className="form-actions span-2"><button className="button button-primary" disabled={Boolean(book) || busy === "book"} type="submit">{busy === "book" ? "保存中…" : book ? "图书版本已保存" : "保存图书版本"}<span>→</span></button><span>图书封面、内文和商品图不会显示在本页或进入演示成片。</span></div>
                  </form>

                  <div className="section-divider" />
                  <div className="form-section-title"><span className="section-index">B</span><div><h2>拟挂商品</h2><p>记录商品链接与平台，资格状态默认未核实。</p></div>{candidate && <span className={`status-pill ${candidate.eligibility_status === "manually_confirmed" ? "status-verified" : "status-pending"}`}>{candidate.eligibility_status === "manually_confirmed" ? "本人记录已核对" : "未核实"}</span>}</div>
                  {candidate && <div className="saved-record candidate-record"><span className="saved-icon">◇</span><div><strong>{candidate.platform_product_id}</strong><p>{platformName(candidate.platform)} · {candidate.eligibility_note || "没有核验说明"}</p></div><small>{candidate.eligibility_status === "manually_confirmed" ? "人工记录" : "待人工核对"}</small></div>}
                  <form className="form-grid" onSubmit={saveCandidate}>
                    <Field label="发布平台" required><select disabled={!book || Boolean(candidate)} onChange={(event) => setCandidateDraft({ ...candidateDraft, platform: event.target.value as Platform })} value={candidateDraft.platform}><option value="douyin">抖音</option><option value="kuaishou">快手</option></select></Field>
                    <Field label="平台商品 ID" required><input disabled={!book || Boolean(candidate)} onChange={(event) => setCandidateDraft({ ...candidateDraft, productId: event.target.value })} placeholder="手工填写拟挂商品 ID" value={candidateDraft.productId} /></Field>
                    <div className="field span-2"><label>商品链接 <em>*</em></label><input disabled={!book || Boolean(candidate)} onChange={(event) => setCandidateDraft({ ...candidateDraft, productUrl: event.target.value })} placeholder="https://…" required type="url" value={candidateDraft.productUrl} /></div>
                    <div className="field span-2"><label>核对说明</label><textarea disabled={!book || Boolean(candidate)} onChange={(event) => setCandidateDraft({ ...candidateDraft, eligibilityNote: event.target.value })} placeholder="未核实时说明缺少什么；已在平台客户端核对时记录核对范围。" rows={2} value={candidateDraft.eligibilityNote} /></div>
                    <label className={`attestation span-2 ${mode === "demo" ? "attestation-disabled" : ""}`}>
                      <input checked={manualCandidateConfirmation} disabled={!book || Boolean(candidate) || mode === "demo" || isSampleLoaded} onChange={(event) => setManualCandidateConfirmation(event.target.checked)} type="checkbox" />
                      <span><strong>我已在自己的平台客户端手动核对该商品的可推广状态。</strong><small>只记录你对拟挂商品的人工确认，不调用平台接口，也不代表平台确认可推广。账号资质仍需单独在平台后台核对。</small></span>
                    </label>
                    <div className="form-actions span-2"><button className="button button-secondary" disabled={!book || Boolean(candidate) || busy === "candidate"} type="submit">{busy === "candidate" ? "保存中…" : candidate ? "拟挂商品已保存" : "保存拟挂商品"}<span>→</span></button><span>演示商品 ID 是占位文本，始终保持未核实。</span></div>
                  </form>
                </>
              )}

              {activeStep === "brief" && (
                <>
                  <PanelHeading overline="02 / CREATIVE BRIEF" title="给脚本一个清楚的起点" description="用自己的选题角度组织内容。没有授权数据时，销量指标留空并记录缺失原因。" />
                  {!book || !candidate ? <PrerequisiteCard title="先完成图书与商品录入" body="保存一个具体图书版本和拟挂商品后，才能建立创作 Brief。" onClick={() => setActiveStep("source")} /> : (
                    <>
                      <div className="context-strip"><span className="context-icon">▤</span><span><strong>{book.title}</strong><small>{book.edition_label} · 拟挂 {platformName(candidate.platform)} 商品 {shortId(candidate.platform_product_id)}</small></span><span className="status-pill status-pending">{candidate.eligibility_status === "manually_confirmed" ? "人工已核对" : "商品未核实"}</span></div>
                      <form className="form-grid brief-form" onSubmit={saveProject}>
                        <Field label="目标受众" required><input disabled={Boolean(project)} onChange={(event) => setBriefDraft({ ...briefDraft, audience: event.target.value })} placeholder="描述你想和谁交流" value={briefDraft.audience} /></Field>
                        <Field label="目标平台" required><select disabled={Boolean(project)} onChange={(event) => setBriefDraft({ ...briefDraft, platform: event.target.value as Platform })} value={briefDraft.platform}><option value="douyin">抖音</option><option value="kuaishou">快手</option></select></Field>
                        <Field label="目标时长" required><select disabled={Boolean(project)} onChange={(event) => setBriefDraft({ ...briefDraft, seconds: Number(event.target.value) })} value={briefDraft.seconds}><option value={20}>20 秒</option><option value={25}>25 秒</option><option value={30}>30 秒</option><option value={35}>35 秒</option><option value={40}>40 秒</option><option value={45}>45 秒</option></select></Field>
                        <Field label="原创角度" required><textarea disabled={Boolean(project)} onChange={(event) => setBriefDraft({ ...briefDraft, angle: event.target.value })} placeholder="用一句话描述你想讲的观点" required rows={3} value={briefDraft.angle} /></Field>
                        <div className="field span-2"><label>禁止出现的表述</label><textarea disabled={Boolean(project)} onChange={(event) => setBriefDraft({ ...briefDraft, prohibitedClaims: event.target.value })} placeholder="用逗号分隔，例如：疗效、保证结果、未核实销量" rows={2} value={briefDraft.prohibitedClaims} /></div>
                        <div className="form-actions span-2"><button className="button button-primary" disabled={Boolean(project) || busy === "project"} type="submit">{busy === "project" ? "保存中…" : project ? "Brief 已创建" : "创建创作 Brief"}<span>→</span></button><span>创建后契约当前没有 Brief 修改接口。</span></div>
                      </form>

                      <div className="section-divider" />
                      <div className="evidence-heading"><div><span className="eyebrow">OPTIONAL · MANUAL ONLY</span><h2>补充一条人工证据</h2><p>只记录你提供的来源。此处不采集平台数据或自动抓取网页。</p></div><span className="mini-tag">没有来源也可以继续</span></div>
                      {evidence && <div className="evidence-saved"><span>✓</span><div><strong>人工证据已保存</strong><small>{evidence.source_note} · {evidence.metric_value === null ? evidence.missing_reason : `${evidence.metric_name}: ${evidence.metric_value}`}</small></div></div>}
                      <form className="form-grid evidence-form" onSubmit={saveEvidence}>
                        <Field label="来源说明" required><input disabled={Boolean(evidence)} onChange={(event) => setEvidenceDraft({ ...evidenceDraft, sourceNote: event.target.value })} placeholder="例如：创作者手工观察记录" required value={evidenceDraft.sourceNote} /></Field>
                        <Field label="来源链接（可选）"><input disabled={Boolean(evidence)} onChange={(event) => setEvidenceDraft({ ...evidenceDraft, sourceUrl: event.target.value })} placeholder="https://…" type="url" value={evidenceDraft.sourceUrl} /></Field>
                        <Field label="使用依据" required><input disabled={Boolean(evidence)} onChange={(event) => setEvidenceDraft({ ...evidenceDraft, usageBasis: event.target.value })} placeholder="仅限本项目使用" required value={evidenceDraft.usageBasis} /></Field>
                        <Field label="观察日期"><input disabled={Boolean(evidence)} onChange={(event) => setEvidenceDraft({ ...evidenceDraft, observedAt: event.target.value })} type="date" value={evidenceDraft.observedAt} /></Field>
                        <Field label="指标名称（可选）"><input disabled={Boolean(evidence)} onChange={(event) => setEvidenceDraft({ ...evidenceDraft, metricName: event.target.value })} placeholder="没有数据请留空" value={evidenceDraft.metricName} /></Field>
                        <Field label="指标数值（可选）"><input disabled={Boolean(evidence)} onChange={(event) => setEvidenceDraft({ ...evidenceDraft, metricValue: event.target.value })} min="0" placeholder="不填不会补 0" type="number" value={evidenceDraft.metricValue} /></Field>
                        <Field label="时间窗起始"><input disabled={Boolean(evidence)} onChange={(event) => setEvidenceDraft({ ...evidenceDraft, windowStart: event.target.value })} type="date" value={evidenceDraft.windowStart} /></Field>
                        <Field label="时间窗结束"><input disabled={Boolean(evidence)} onChange={(event) => setEvidenceDraft({ ...evidenceDraft, windowEnd: event.target.value })} type="date" value={evidenceDraft.windowEnd} /></Field>
                        <div className="field span-2"><label>缺失原因</label><input disabled={Boolean(evidence)} onChange={(event) => setEvidenceDraft({ ...evidenceDraft, missingReason: event.target.value })} placeholder="无指标时说明为什么缺失" value={evidenceDraft.missingReason} /></div>
                        <div className="form-actions span-2"><button className="button button-muted" disabled={Boolean(evidence) || busy === "evidence"} type="submit">{busy === "evidence" ? "保存中…" : evidence ? "证据已保存" : "保存人工证据"}<span>＋</span></button><span>未提供指标时以 null 保存，不会把缺失值当作 0。</span></div>
                      </form>
                    </>
                  )}
                </>
              )}

              {activeStep === "script" && (
                <>
                  <PanelHeading overline="03 / SCRIPT ROOM" title="脚本与版本" description="每一次修改都会保存为新版本。审核通过的版本不可原地改写。" />
                  {!project ? <PrerequisiteCard title="先创建创作 Brief" body="确定目标受众和原创角度后，才能开始脚本生成。" onClick={() => setActiveStep(book && candidate ? "brief" : "source")} /> : (
                    <>
                      <div className="run-control">
                        <div className="run-control-copy"><span className="run-control-icon">✦</span><div><strong>{run ? `任务 ${shortId(run.id)}` : "从当前 Brief 生成脚本"}</strong><small>{run ? `${statusName(run.status)} · ${stageName(run.stage)} · 更新于 ${formatTime(run.updated_at)}` : `目标时长 ${project.desired_seconds} 秒 · 原创角度已保存`}</small></div></div>
                        <button className="button button-primary" disabled={busy === "run" || Boolean(run && ["queued", "running", "waiting_review"].includes(run.status))} onClick={() => void startGeneration()} type="button">{busy === "run" ? "提交中…" : run && ["queued", "running", "waiting_review"].includes(run.status) ? "任务进行中" : "开始生成"}<span>✦</span></button>
                      </div>
                      {mode === "demo" && <div className="inline-warning"><span>!</span><p>演示脚本由本地模板生成，仅用于体验版本与审核界面；没有模型调用，也没有引用书籍内文。</p></div>}
                      {mode === "api" && runStreamWarning && <div className="inline-warning"><span>↻</span><p>{runStreamWarning} 可继续查看状态页。</p></div>}

                      <div className="versions-header"><div><h2>脚本版本</h2><p>选择一个版本查看或编辑。</p></div><span className="version-count">{versions.length} 版</span></div>
                      {versions.length === 0 ? (
                        <EmptyState icon="✎" title="还没有脚本版本" body="提交生成任务后，脚本版本会出现在这里。" />
                      ) : (
                        <div className="version-tabs">
                          {versions.map((version) => (
                            <button className={`version-tab ${selectedVersionId === version.id ? "is-selected" : ""}`} key={version.id} onClick={() => setSelectedVersionId(version.id)} type="button">
                              <span className="version-number">v{version.version_number}</span><span className="version-tab-title">{version.title_suggestion}</span><ReviewPill status={version.review_status} />
                            </button>
                          ))}
                        </div>
                      )}
                      {selectedVersion && (
                        <form className="script-editor" onSubmit={saveScript}>
                          <div className="editor-heading"><div><span className="eyebrow">SCRIPT EDITOR</span><h2>编辑 v{selectedVersion.version_number}</h2><p>保存会创建下一版，不会覆盖当前版本。</p></div><ReviewPill status={selectedVersion.review_status} /></div>
                          <Field label="标题建议" required><input onChange={(event) => setEditorTitle(event.target.value)} required value={editorTitle} /></Field>
                          <Field label="简介建议" required><textarea onChange={(event) => setEditorDescription(event.target.value)} required rows={2} value={editorDescription} /></Field>
                          <div className="segments-heading"><div><h3>镜头文字与旁白</h3><p>只写原创表达；事实键须来自已记录的事实。</p></div><span>{currentDuration} 秒</span></div>
                          <div className="segment-list">
                            {editorSegments.map((segment, index) => (
                              <article className="segment-card" key={`${selectedVersion.id}-${index}`}>
                                <div className="segment-top"><span className="segment-index">{String(index + 1).padStart(2, "0")}</span><span>文字段落</span><label>时长 <input aria-label={`第 ${index + 1} 段时长`} min="0.1" onChange={(event) => setEditorSegments((existing) => existing.map((item, itemIndex) => itemIndex === index ? { ...item, duration_seconds: Number(event.target.value) } : item))} step="0.1" type="number" value={segment.duration_seconds} /> 秒</label><button aria-label={`删除第 ${index + 1} 段`} disabled={editorSegments.length <= 1} onClick={() => setEditorSegments((existing) => existing.filter((_, itemIndex) => itemIndex !== index).map((item, itemIndex) => ({ ...item, order: itemIndex })))} type="button">×</button></div>
                                <Field label="屏幕文字"><input onChange={(event) => setEditorSegments((existing) => existing.map((item, itemIndex) => itemIndex === index ? { ...item, onscreen_text: event.target.value } : item))} value={segment.onscreen_text} /></Field>
                                <Field label="旁白"><textarea onChange={(event) => setEditorSegments((existing) => existing.map((item, itemIndex) => itemIndex === index ? { ...item, narration: event.target.value } : item))} rows={2} value={segment.narration} /></Field>
                              </article>
                            ))}
                          </div>
                          <div className="editor-actions"><button className="text-button" onClick={() => setEditorSegments((existing) => [...existing, { order: existing.length, onscreen_text: "", narration: "", fact_keys: [], duration_seconds: 5 }])} type="button">＋ 添加一段</button><button className="button button-primary" disabled={busy === "script"} type="submit">{busy === "script" ? "保存中…" : "另存为新版本"}<span>→</span></button></div>
                        </form>
                      )}
                    </>
                  )}
                </>
              )}

              {activeStep === "review" && (
                <>
                  <PanelHeading overline="04 / HUMAN REVIEW" title="把关，再进入渲染" description="只有你来核对书籍事实、商品版本与素材权利。提交后仍由 API 校验审核状态。" />
                  {!run || !selectedVersion ? <PrerequisiteCard title="还没有待审核脚本" body="先生成或选择一个脚本版本，再来记录人工审核决定。" onClick={() => setActiveStep("script")} /> : (
                    <>
                      <div className="review-state-card">
                        <div className="state-icon">{run.status === "waiting_review" ? "◉" : "◷"}</div>
                        <div><span className="eyebrow">RUN {shortId(run.id)}</span><strong>{statusName(run.status)} <i>·</i> {stageName(run.stage)}</strong><small>当前审核版本：v{selectedVersion.version_number} · {formatTime(run.updated_at)}</small></div>
                        <span className={`run-pill ${run.status}`}>{statusName(run.status)}</span>
                      </div>
                      <div className="review-bookkeeping">
                        <div><span>事实状态</span><strong>{factsHaveSources ? "有来源或本人已确认" : "含未核实事实"}</strong></div>
                        <div><span>拟挂商品</span><strong>{candidate?.eligibility_status === "manually_confirmed" ? "已记录人工核对" : "平台资格未核实"}</strong></div>
                        <div><span>素材范围</span><strong>原创文字 / 自制图形</strong></div>
                      </div>
                      {mode === "demo" && <div className="inline-warning"><span>!</span><p>此虚构样例中的商品 ID 不是实际商品，审核通过会被阻止；可以在本地演示中记录驳回，不能模拟真实资格。</p></div>}
                      {candidate?.eligibility_status !== "manually_confirmed" && mode === "api" && <div className="inline-warning"><span>!</span><p>该候选商品没有记录平台客户端的人工核对。冻结契约没有更新商品资格的接口，请在保存候选商品时记录人工核对，或由集成负责人补充接口。</p></div>}

                      <form className="review-form" onSubmit={submitReview}>
                        <div className="review-choice-row"><div><span className="eyebrow">YOUR DECISION</span><h2>这版脚本的审核结论</h2></div><div className="decision-toggle"><button className={reviewDecision === "approve" ? "active" : ""} onClick={() => setReviewDecision("approve")} type="button">通过</button><button className={reviewDecision === "reject" ? "active reject-active" : ""} onClick={() => setReviewDecision("reject")} type="button">退回修改</button></div></div>
                        <div className="review-checks">
                          <label className="review-check"><input checked={reviewFlags.fact} onChange={(event) => setReviewFlags({ ...reviewFlags, fact: event.target.checked })} type="checkbox" /><span className="check-visual">✓</span><span><strong>书籍事实已核对</strong><small>脚本没有带入未经核实的事实、销量或疗效表述。</small></span><b>{book?.facts.every((fact) => fact.verification_status !== "unverified") ? "记录有来源" : "待核对"}</b></label>
                          <label className="review-check"><input checked={reviewFlags.product} onChange={(event) => setReviewFlags({ ...reviewFlags, product: event.target.checked })} type="checkbox" /><span className="check-visual">✓</span><span><strong>商品与书籍版本匹配</strong><small>在平台客户端确认拟挂商品对应本次录入的具体版次。</small></span><b>{candidate?.eligibility_status === "manually_confirmed" ? "本人已记录" : "未核实"}</b></label>
                          <label className="review-check"><input checked={reviewFlags.rights} onChange={(event) => setReviewFlags({ ...reviewFlags, rights: event.target.checked })} type="checkbox" /><span className="check-visual">✓</span><span><strong>素材权利已确认</strong><small>不使用未获许可的封面、内文、商品图、音乐、肖像或声音。</small></span><b>需本人确认</b></label>
                        </div>
                        <Field label="审核备注"><textarea onChange={(event) => setReviewNote(event.target.value)} placeholder="记录修改意见、事实缺失或商品版本差异" rows={3} value={reviewNote} /></Field>
                        <div className="review-submit"><p>{reviewDecision === "approve" ? "通过只会提交审核请求；商品推广资格和素材权利由服务端继续校验。" : "退回后应修改脚本并保存为新版本。"}</p><button className={`button ${reviewDecision === "approve" ? "button-primary" : "button-danger"}`} disabled={busy === "review" || run.status !== "waiting_review" || (reviewDecision === "approve" && !canApprove)} type="submit">{busy === "review" ? "提交中…" : reviewDecision === "approve" ? "提交通过审核" : "记录退回意见"}<span>→</span></button></div>
                        {reviewDecision === "approve" && !canApprove && <p className="blocked-hint">通过按钮会在事实有来源、三项核对完成且商品已记录人工核对后开放。</p>}
                      </form>

                      <div className="timeline-block"><div className="timeline-heading"><div><h2>任务事件</h2><p>按事件序号展示，可用于恢复任务进度。</p></div><span>{events.length} 条</span></div>
                        {events.length === 0 ? <EmptyState icon="◷" title="暂无事件" body="API 事件到达后会显示在这里。" /> : <ol className="event-list">{events.slice(-8).map((event) => <li key={`${event.run_id}-${event.sequence}`}><span className="event-sequence">{String(event.sequence).padStart(2, "0")}</span><div><strong>{eventLabel(event)} <i>·</i> {stageName(event.stage)}</strong><p>{event.message || statusName(event.status)}</p></div><time>{formatTime(event.occurred_at)}</time></li>)}</ol>}
                      </div>
                    </>
                  )}
                </>
              )}

              {activeStep === "package" && (
                <>
                  <PanelHeading overline="05 / PUBLICATION PACKAGE" title="成片与手动核对卡" description="发布包只整理本地素材和检查项。是否发布、是否挂车，最后都由你在平台客户端操作。" />
                  {!project || !candidate || !selectedVersion ? <PrerequisiteCard title="先完成脚本审核" body="创建 Brief、生成脚本并记录人工审核后，才能查看发布包。" onClick={() => setActiveStep(project ? "review" : "source")} /> : (
                    <>
                      {mode === "demo" ? (
                        <div className="demo-package-note"><span className="demo-stamp">SAMPLE</span><div><strong>这是核对卡预览，不是可发布的视频包。</strong><p>虚构商品资格未核实，演示模式没有视频文件。你可以下载 JSON 格式的本地核对卡预览。</p></div></div>
                      ) : (
                        <div className="render-box">
                          <div className="render-box-head"><div className="render-glyph">▧</div><div><strong>{renderJob ? `渲染 ${shortId(renderJob.id)}` : "竖屏文字动效成片"}</strong><small>{renderJob ? `${statusName(renderJob.status)}${renderJob.error_message ? ` · ${renderJob.error_message}` : ""}` : "1080 × 1920 · 使用审核通过的脚本"}</small></div>{renderJob && <span className={`run-pill ${renderJob.status}`}>{statusName(renderJob.status)}</span>}</div>
                          <label className="attestation render-attestation"><input checked={renderRightsConfirmed} onChange={(event) => setRenderRightsConfirmed(event.target.checked)} type="checkbox" /><span><strong>我确认本次使用的文字为原创内容，自制图形可用于本次渲染。</strong><small>默认静音；无许可凭据的外部图片、音乐、肖像和声音不会进入成片。</small></span></label>
                          <div className="render-box-actions"><button className="button button-primary" disabled={busy === "render" || !selectedVersionApproved || !renderRightsConfirmed || !candidateIsManuallyConfirmed || Boolean(renderJob && ["queued", "running"].includes(renderJob.status))} onClick={() => void startRender()} type="button">{busy === "render" ? "提交中…" : renderJob && ["queued", "running"].includes(renderJob.status) ? "渲染进行中" : renderJob?.status === "succeeded" ? "重新提交渲染" : "提交渲染"}<span>→</span></button><span>{!selectedVersionApproved ? "选择一版审核通过的脚本。" : !candidateIsManuallyConfirmed ? "先记录平台客户端人工核对。" : "仅提交给 API，不会发布到平台。"}</span></div>
                        </div>
                      )}

                      <div className="package-toolbar"><div><span className="eyebrow">MANUAL CHECKLIST</span><h2>发布前人工核对</h2><p>以服务端返回的状态为准。平台资格和挂车结果需你在平台后台确认。</p></div><div className="package-buttons"><button className="button button-secondary" disabled={busy === "package" || (mode === "api" && renderJob?.status !== "succeeded")} onClick={() => void loadPublicationPackage()} type="button">{busy === "package" ? "加载中…" : packagePreviewShown || publicationPackage ? "刷新核对卡" : mode === "demo" ? "展示演示核对卡" : "加载发布包"}<span>↻</span></button>{publicationPackage && <button className="button button-muted" onClick={downloadPackageJson} type="button">下载核对卡 JSON <span>↓</span></button>}</div></div>
                      {mode === "api" && renderJob?.status !== "succeeded" && <div className="inline-warning"><span>!</span><p>API 尚未返回渲染完成状态，发布包不会标记为可下载。审核通过并完成渲染后再加载。</p></div>}
                      {publicationPackage ? (
                        <div className="publication-card">
                          <div className="publication-card-header"><div><span className="package-symbol">⇧</span><div><span className="eyebrow">{mode === "demo" ? "LOCAL PREVIEW" : `PACKAGE ${shortId(publicationPackage.id)}`}</span><h3>{publicationPackage.title}</h3><p>{publicationPackage.description}</p></div></div><span className={`package-state ${publicationPackage.publication_state}`}>{publicationPackage.publication_state === "ready_for_manual_publish" ? "准备人工发布" : "仍需人工核对"}</span></div>
                          {publicationPackage.render_id && mode === "api" ? <button className="download-video" disabled={busy === "download"} onClick={() => void downloadRenderedVideo()} type="button"><span>▶</span><span><strong>{busy === "download" ? "下载中…" : "下载成片"}</strong><small>下载已完成渲染的 MP4 文件</small></span><b>↓</b></button> : <div className="no-video"><span>▧</span><div><strong>没有可下载的视频文件</strong><small>本地样例只展示核对卡；API 模式需先完成渲染。</small></div></div>}
                          <div className="checklist-grid">
                            <ChecklistItem label="账号具备发布资格" checked={publicationPackage.checklist.account_eligible} />
                            <ChecklistItem label="商品可推广" checked={publicationPackage.checklist.product_eligible} />
                            <ChecklistItem label="商品与图书版次一致" checked={publicationPackage.checklist.edition_matches} />
                            <ChecklistItem label="素材权利已确认" checked={publicationPackage.checklist.rights_confirmed} />
                            <ChecklistItem label="必须手动挂车" checked={publicationPackage.checklist.manual_attachment_required} alwaysRequired />
                          </div>
                          <div className="manual-publish-callout"><span>1</span><div><strong>由你在平台客户端完成最后一步</strong><p>下载视频和文案后，在抖音或快手客户端核对账号资格、商品信息，再手动发布并挂车。本地准备状态不表示平台已确认或已完成挂车。</p></div></div>
                        </div>
                      ) : (
                        <EmptyState icon="⇧" title="还没有发布包核对卡" body={mode === "demo" ? "先生成本地演示核对卡，查看人工检查项。" : "渲染完成后，从 API 加载发布包和视频下载地址。"} />
                      )}
                    </>
                  )}
                </>
              )}
            </section>

            <aside className="right-rail">
              <div className="rail-card rail-progress">
                <div className="rail-card-heading"><div><span className="eyebrow">YOUR FLOW</span><h2>创作进度</h2></div><span className="progress-ring"><b>{project ? "3" : candidate ? "2" : book ? "1" : "0"}</b><small>/ 5</small></span></div>
                <ol className="mini-progress-list">
                  {STEP_ITEMS.map((item, index) => {
                    const complete = item.id === "source" ? Boolean(book && candidate) : item.id === "brief" ? Boolean(project) : item.id === "script" ? Boolean(versions.length) : item.id === "review" ? Boolean(approvedVersion) : Boolean(publicationPackage);
                    return <li className={activeStep === item.id ? "current" : complete ? "complete" : ""} key={item.id}><span className="mini-progress-dot">{complete ? "✓" : String(index + 1)}</span><button onClick={() => setActiveStep(item.id)} type="button">{item.label}</button>{complete && <small>完成</small>}</li>;
                  })}
                </ol>
              </div>
              <div className="rail-card data-card">
                <div className="data-card-title"><span className="data-icon">↗</span><div><span className="eyebrow">DATA & RIGHTS</span><h2>数据与素材</h2></div></div>
                <div className="data-state"><span className="state-led" /><div><strong>授权平台数据未取得</strong><small>当前仅支持人工输入</small></div></div>
                <ul className="data-rules"><li>没有授权时不展示销量或竞品数据。</li><li>书封、内文、商品图、音乐和肖像默认不可用。</li><li>视频发布与挂车由你手动完成。</li></ul>
                <div className="data-source"><span>数据源</span><strong>{evidence ? `人工 · ${evidence.source_note}` : "尚未添加人工证据"}</strong></div>
              </div>
              <div className="rail-card current-record-card">
                <div className="rail-card-heading"><div><span className="eyebrow">CURRENT RECORD</span><h2>当前项目</h2></div><span className="record-badge">{project ? "活跃" : "草稿"}</span></div>
                <RecordLine label="图书" value={book?.title ?? "待录入"} />
                <RecordLine label="商品" value={candidate ? shortId(candidate.platform_product_id) : "待录入"} />
                <RecordLine label="项目 ID" value={shortId(project?.id)} />
                <RecordLine label="当前脚本" value={selectedVersion ? `v${selectedVersion.version_number}` : "尚无版本"} />
                <button className="rail-link" onClick={() => setActiveStep("source")} type="button">查看录入信息 <span>→</span></button>
              </div>
              <div className="help-card"><span>?</span><div><strong>使用边界</strong><p>本工具准备原创内容和核对卡，不登录平台、不代发、不自动挂商品。</p></div></div>
            </aside>
          </div>
          <footer className="page-footer"><span>页间 · 个人创作工具</span><span>阶段 1 / 内部演示</span><span>内容发布与商品挂车由创作者本人负责</span></footer>
        </div>
      </main>
    </div>
  );
}

function SummaryCell({ label, value, detail, icon }: { label: string; value: string; detail: string; icon: string }) {
  return <div className="summary-cell"><span className="summary-icon">{icon}</span><div><span className="summary-label">{label}</span><strong title={value}>{value}</strong><small>{detail}</small></div></div>;
}

function PanelHeading({ overline, title, description }: { overline: string; title: string; description: string }) {
  return <div className="panel-heading"><span className="eyebrow">{overline}</span><h2>{title}</h2><p>{description}</p></div>;
}

function Field({ label, required = false, children }: { label: string; required?: boolean; children: ReactNode }) {
  return <div className="field"><label>{label}{required && <em> *</em>}</label>{children}</div>;
}

function ReviewPill({ status }: { status: ScriptVersion["review_status"] }) {
  const labels: Record<ScriptVersion["review_status"], string> = { pending: "待审核", approved: "已通过", rejected: "待修改" };
  return <span className={`review-pill ${status}`}>{labels[status]}</span>;
}

function PrerequisiteCard({ title, body, onClick }: { title: string; body: string; onClick: () => void }) {
  return <div className="prerequisite"><span>◇</span><div><strong>{title}</strong><p>{body}</p></div><button className="button button-secondary" onClick={onClick} type="button">前往录入 <span>→</span></button></div>;
}

function EmptyState({ icon, title, body }: { icon: string; title: string; body: string }) {
  return <div className="empty-state"><span>{icon}</span><strong>{title}</strong><p>{body}</p></div>;
}

function ChecklistItem({ label, checked, alwaysRequired = false }: { label: string; checked: boolean; alwaysRequired?: boolean }) {
  return <div className={`checklist-item ${checked ? "checked" : "unchecked"}`}><span className="check-status">{checked ? "✓" : "—"}</span><span>{label}</span><b>{alwaysRequired ? "必须" : checked ? "已记录" : "待确认"}</b></div>;
}

function RecordLine({ label, value }: { label: string; value: string }) {
  return <div className="record-line"><span>{label}</span><strong title={value}>{value}</strong></div>;
}

createRoot(document.getElementById("root")!).render(<App />);
