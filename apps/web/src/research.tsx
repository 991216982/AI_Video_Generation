import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import {
  ApiRequestError,
  type BookVideoApi,
  type ContentReference,
  type Evidence,
  type ImportRowDiagnostic,
  type ImportJob,
  type ImportPreview,
  type MetricBasis,
  type MetricName,
  type MetricObservation,
  type MetricUnit,
  type ObjectType,
  type Platform,
  type StyleCard,
  type StyleCardStatus,
  type StyleFeatureKey,
  type StyleFeature,
} from "./api";

type ResearchTab = "metrics" | "references" | "styles" | "imports";
type PageState = { cursor: string | null; history: (string | null)[]; next: string | null };

const EMPTY_PAGE: PageState = { cursor: null, history: [], next: null };

const MAPPING_TARGETS: { value: string; label: string }[] = [
  { value: "book.isbn", label: "图书 · ISBN" },
  { value: "book.title", label: "图书 · 书名" },
  { value: "book.edition_label", label: "图书 · 版次" },
  { value: "book.author", label: "图书 · 作者" },
  { value: "book.publisher", label: "图书 · 出版社" },
  { value: "book.original_opinion", label: "图书 · 原创观点" },
  { value: "candidate.platform", label: "拟挂商品 · 平台" },
  { value: "candidate.platform_product_id", label: "拟挂商品 · 商品 ID" },
  { value: "candidate.product_url", label: "拟挂商品 · 链接" },
  { value: "reference.ref_type", label: "内容参考 · 对象类型" },
  { value: "reference.external_ref", label: "内容参考 · 外部标识" },
  { value: "reference.reference_url", label: "内容参考 · 链接" },
  { value: "reference.title", label: "内容参考 · 标题" },
  { value: "reference.note", label: "内容参考 · 观察笔记" },
  { value: "reference.license_scope", label: "内容参考 · 授权范围" },
  { value: "reference.usage_basis", label: "内容参考 · 使用依据" },
  { value: "metric.object_type", label: "指标 · 对象类型" },
  { value: "metric.object_ref", label: "指标 · 对象标识" },
  { value: "metric.object_label", label: "指标 · 对象名称" },
  { value: "metric.platform", label: "指标 · 平台" },
  { value: "metric.metric_name", label: "指标 · 名称" },
  { value: "metric.metric_definition", label: "指标 · 统计口径" },
  { value: "metric.basis", label: "指标 · 依据类别" },
  { value: "metric.unit", label: "指标 · 单位" },
  { value: "metric.value", label: "指标 · 数值" },
  { value: "metric.value_text", label: "指标 · 页面展示文案" },
  { value: "metric.window_start", label: "指标 · 时间窗开始" },
  { value: "metric.window_end", label: "指标 · 时间窗结束" },
  { value: "metric.observed_at", label: "指标 · 观察时间" },
  { value: "metric.missing_reason", label: "指标 · 缺失原因" },
  { value: "metric.book_id", label: "指标 · 关联图书 ID" },
  { value: "evidence.source_type", label: "证据 · 来源类型" },
  { value: "evidence.source_note", label: "证据 · 来源说明" },
  { value: "evidence.source_url", label: "证据 · 来源链接" },
  { value: "evidence.observed_at", label: "证据 · 观察时间" },
  { value: "evidence.usage_basis", label: "证据 · 使用依据" },
];

const BASIS_META: Record<MetricBasis, { label: string; className: string; hint: string }> = {
  authorized_order_sales: { label: "授权订单实际销量", className: "basis-order", hint: "唯一可表述为实际销量的口径" },
  platform_listing_display: { label: "平台页面展示值", className: "basis-display", hint: "页面档位或文案，不等于订单数据" },
  third_party_or_manual_estimate: { label: "第三方 / 人工估算", className: "basis-estimate", hint: "估算值，不能写成实际销量" },
  engagement_signal: { label: "热度信号", className: "basis-engagement", hint: "播放、点赞等关注度，不是销量" },
  legacy_unclassified: { label: "待分类（回填）", className: "basis-legacy", hint: "旧数据回填，重新核实前不参与排名" },
};

const OBJECT_TYPE_LABELS: Record<ObjectType, string> = {
  product_listing: "商品",
  creator_account: "账号",
  content_item: "作品",
  book_edition: "图书版本",
};

const METRIC_NAME_LABELS: Record<MetricName, string> = {
  sales_units: "销量件数",
  sales_amount: "销售额",
  listed_price: "页面标价",
  displayed_sales_label: "展示档位文案",
  plays: "播放量",
  likes: "点赞数",
  comments: "评论数",
  shares: "分享数",
  favorites: "收藏数",
  follower_count: "粉丝数",
  ranking_position: "排名位置",
  search_heat: "搜索热度",
  other: "其他指标",
};

const FEATURE_LABELS: Record<StyleFeatureKey, string> = {
  opening_hook: "开场钩子",
  narrative_structure: "叙事结构",
  pacing: "节奏",
  visual_style: "视觉风格",
  subtitle_style: "字幕风格",
  tone: "语气",
  cta: "行动号召",
  audience_match: "受众匹配",
  other: "其他",
};

const STYLE_STATUS_LABELS: Record<StyleCardStatus, string> = {
  draft: "草稿",
  pending_review: "待审核",
  approved: "已通过",
  rejected: "待修改",
};

const LICENSE_LABELS: Record<string, string> = {
  own_account: "自有账号",
  licensed_data_service: "获许可数据服务",
  manual_observation: "人工观察",
};

function researchError(error: unknown): string {
  if (error instanceof ApiRequestError) {
    const suffix = error.requestId ? `（请求编号 ${error.requestId}）` : "";
    return `${error.message}${suffix}`;
  }
  if (error instanceof Error) return error.message;
  return "操作未完成，请检查输入后重试。";
}

function formatTime(value: string | null | undefined): string {
  if (!value) return "尚无记录";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(parsed);
}

function shortId(value: string | undefined): string {
  if (!value) return "—";
  return value.length > 17 ? `${value.slice(0, 8)}…${value.slice(-5)}` : value;
}

function localDateToIso(value: string): string | null {
  if (!value.trim()) return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed.toISOString();
}

export function ResearchDesk({ api }: { api: BookVideoApi }) {
  const [activeTab, setActiveTab] = useState<ResearchTab>("metrics");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");

  const [metrics, setMetrics] = useState<MetricObservation[]>([]);
  const [references, setReferences] = useState<ContentReference[]>([]);
  const [styles, setStyles] = useState<StyleCard[]>([]);
  const [imports, setImports] = useState<ImportJob[]>([]);
  const [evidenceMap, setEvidenceMap] = useState<Record<string, Evidence>>({});

  const [basisFilter, setBasisFilter] = useState<MetricBasis | "">("");
  const [platformFilter, setPlatformFilter] = useState<Platform | "">("");
  const [objectTypeFilter, setObjectTypeFilter] = useState<ObjectType | "">("");
  const [metricNameFilter, setMetricNameFilter] = useState<MetricName | "">("");
  const [styleStatusFilter, setStyleStatusFilter] = useState<StyleCardStatus | "">("");
  const [metricDraft, setMetricDraft] = useState({
    platform: "douyin" as Platform, objectType: "content_item" as ObjectType,
    objectRef: "", metricName: "plays" as MetricName, definition: "",
    basis: "engagement_signal" as MetricBasis, unit: "views" as MetricUnit,
    value: "", valueText: "", missingReason: "", windowStart: "", windowEnd: "",
    observedAt: "", evidenceId: "",
  });
  const [referenceDraft, setReferenceDraft] = useState({
    platform: "douyin" as Platform, refType: "content_item" as ObjectType,
    title: "", url: "", note: "", licenseScope: "manual_observation" as ContentReference["license_scope"],
    usageBasis: "", observedAt: "", evidenceId: "",
  });
  const [styleDraft, setStyleDraft] = useState({
    title: "", featureKey: "opening_hook" as StyleFeatureKey,
    label: "", explanation: "", evidenceId: "", note: "",
  });

  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [previewMapping, setPreviewMapping] = useState<Record<string, string | null>>({});
  const [lastImport, setLastImport] = useState<ImportJob | null>(null);
  const [importing, setImporting] = useState(false);
  const [drawerEvidence, setDrawerEvidence] = useState<Evidence | null>(null);
  const [pages, setPages] = useState<Record<ResearchTab, PageState>>({
    metrics: { ...EMPTY_PAGE }, references: { ...EMPTY_PAGE },
    styles: { ...EMPTY_PAGE }, imports: { ...EMPTY_PAGE },
  });

  function setNextPage(tab: ResearchTab, next: string | null) {
    setPages((current) => ({ ...current, [tab]: { ...current[tab], next } }));
  }

  function resetPages(...tabs: ResearchTab[]) {
    setPages((current) => {
      const updated = { ...current };
      for (const tab of tabs) updated[tab] = { ...EMPTY_PAGE };
      return updated;
    });
  }

  function movePage(tab: ResearchTab, direction: "previous" | "next") {
    setPages((current) => {
      const page = current[tab];
      if (direction === "next" && page.next) {
        return { ...current, [tab]: { cursor: page.next, history: [...page.history, page.cursor], next: null } };
      }
      if (direction === "previous" && page.history.length) {
        return { ...current, [tab]: { cursor: page.history[page.history.length - 1], history: page.history.slice(0, -1), next: null } };
      }
      return current;
    });
  }

  const loadEvidenceMap = useCallback(async () => {
    const page = await api.listEvidence({ limit: 100 });
    setEvidenceMap(Object.fromEntries(page.items.map((item) => [item.id, item])));
  }, [api]);

  const loadMetrics = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const page = await api.listMetrics({
        limit: 50,
        cursor: pages.metrics.cursor || undefined,
        basis: basisFilter || undefined,
        platform: platformFilter || undefined,
        object_type: objectTypeFilter || undefined,
        metric_name: metricNameFilter || undefined,
      });
      setMetrics(page.items);
      setNextPage("metrics", page.next_cursor);
    } catch (requestError) {
      setError(researchError(requestError));
    } finally {
      setLoading(false);
    }
  }, [api, basisFilter, platformFilter, objectTypeFilter, metricNameFilter, pages.metrics.cursor]);

  const loadReferences = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const page = await api.listContentReferences({ limit: 50, cursor: pages.references.cursor || undefined, platform: platformFilter || undefined, ref_type: objectTypeFilter || undefined });
      setReferences(page.items);
      setNextPage("references", page.next_cursor);
    } catch (requestError) {
      setError(researchError(requestError));
    } finally {
      setLoading(false);
    }
  }, [api, platformFilter, objectTypeFilter, pages.references.cursor]);

  const loadStyles = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const page = await api.listStyleCards({ limit: 50, cursor: pages.styles.cursor || undefined, review_status: styleStatusFilter || undefined });
      setStyles(page.items);
      setNextPage("styles", page.next_cursor);
    } catch (requestError) {
      setError(researchError(requestError));
    } finally {
      setLoading(false);
    }
  }, [api, styleStatusFilter, pages.styles.cursor]);

  const loadImports = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const page = await api.listImports({ limit: 50, cursor: pages.imports.cursor || undefined });
      setImports(page.items);
      setNextPage("imports", page.next_cursor);
    } catch (requestError) {
      setError(researchError(requestError));
    } finally {
      setLoading(false);
    }
  }, [api, pages.imports.cursor]);

  useEffect(() => {
    void loadEvidenceMap().catch(() => undefined);
  }, [loadEvidenceMap]);

  useEffect(() => {
    if (activeTab === "metrics") void loadMetrics();
    else if (activeTab === "references") void loadReferences();
    else if (activeTab === "styles") void loadStyles();
    else void loadImports();
  }, [activeTab, loadMetrics, loadReferences, loadStyles, loadImports]);

  async function openEvidenceDrawer(evidenceId: string | null) {
    if (!evidenceId) return;
    const cached = evidenceMap[evidenceId];
    if (cached) {
      setDrawerEvidence(cached);
      return;
    }
    try {
      let cursor: string | null = null;
      const foundRecords: Evidence[] = [];
      do {
        const page = await api.listEvidence({ limit: 100, cursor: cursor || undefined });
        foundRecords.push(...page.items);
        const found = page.items.find((item) => item.id === evidenceId);
        if (found) {
          setEvidenceMap((existing) => ({ ...existing, ...Object.fromEntries(foundRecords.map((item) => [item.id, item])) }));
          setDrawerEvidence(found);
          return;
        }
        cursor = page.next_cursor;
      } while (cursor);
      setError("未找到这条证据；请刷新研究列表后重试。");
    } catch (requestError) {
      setError(researchError(requestError));
    }
  }

  async function handleStyleReview(card: StyleCard, decision: "approve" | "reject", note: string) {
    setError("");
    try {
      const updated = await api.reviewStyleCard(card.id, { decision, note: note.trim() || null });
      setStyles((existing) => existing.map((item) => (item.id === updated.id ? updated : item)));
      setSuccess(decision === "approve" ? "风格卡已通过人工审核。" : "风格卡已退回修改。");
    } catch (requestError) {
      setError(researchError(requestError));
    }
  }

  async function handleStyleRevision(card: StyleCard, title: string, note: string, features: StyleFeature[]) {
    setError("");
    try {
      const updated = await api.updateStyleCard(card.id, {
        expected_revision: card.revision,
        title: title.trim() || card.title,
        human_note: note.trim() || null,
        features,
      });
      setStyles((existing) => existing.map((item) => (item.id === updated.id ? updated : item)));
      setSuccess("风格卡已保存修订；已通过的卡片回到待审核状态。");
    } catch (requestError) {
      setError(researchError(requestError));
    }
  }

  async function handleMetricCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const numeric = metricDraft.value.trim() ? Number(metricDraft.value) : null;
    if (numeric !== null && !Number.isFinite(numeric)) {
      setError("指标数值必须是有效数字；未知值请留空并填写缺失原因。");
      return;
    }
    const start = localDateToIso(metricDraft.windowStart);
    const end = localDateToIso(metricDraft.windowEnd);
    if ((metricDraft.windowStart && !start) || (metricDraft.windowEnd && !end)) {
      setError("请填写有效的统计时间窗。");
      return;
    }
    try {
      setError("");
      await api.createMetric({
        platform: metricDraft.platform, book_id: null,
        object_type: metricDraft.objectType, object_ref: metricDraft.objectRef.trim(), object_label: null,
        metric_name: metricDraft.metricName, metric_definition: metricDraft.definition.trim(),
        basis: metricDraft.basis, unit: metricDraft.unit, value: numeric,
        value_text: metricDraft.valueText.trim() || null,
        window_start: start, window_end: end,
        observed_at: localDateToIso(metricDraft.observedAt) || new Date().toISOString(),
        evidence_id: metricDraft.evidenceId.trim() || null,
        missing_reason: numeric === null ? (metricDraft.missingReason.trim() || (metricDraft.valueText.trim() ? "页面仅展示档位，精确值未取得" : null)) : null,
        confidence: null, expires_at: null, note: null,
      });
      setSuccess("人工指标已保存；请核对来源、口径和时间窗。");
      resetPages("metrics");
      await loadMetrics();
    } catch (requestError) {
      setError(researchError(requestError));
    }
  }

  async function handleReferenceCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    try {
      setError("");
      await api.createContentReference({
        platform: referenceDraft.platform, book_id: null, ref_type: referenceDraft.refType,
        external_ref: null, reference_url: referenceDraft.url.trim() || null,
        title: referenceDraft.title.trim(), note: referenceDraft.note.trim() || null,
        license_scope: referenceDraft.licenseScope, usage_basis: referenceDraft.usageBasis.trim(),
        captured_fields: ["title", "url", "observed_note"],
        evidence_ids: referenceDraft.evidenceId.trim() ? [referenceDraft.evidenceId.trim()] : [],
        observed_at: localDateToIso(referenceDraft.observedAt),
        material_statement: "no_third_party_media_or_verbatim_transcript",
      });
      setSuccess("内容参考已保存；链接仅供追溯，不作为成片素材。");
      resetPages("references");
      await loadReferences();
    } catch (requestError) {
      setError(researchError(requestError));
    }
  }

  async function handleStyleCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const evidenceId = styleDraft.evidenceId.trim();
    if (!evidenceId) {
      setError("人工风格卡至少需要一条证据 ID。");
      return;
    }
    try {
      setError("");
      await api.createStyleCard({
        title: styleDraft.title.trim(), target_platform: null, book_id: null,
        derived_by: "manual", evidence_ids: [evidenceId], reference_ids: [],
        features: [{ feature_key: styleDraft.featureKey, label: styleDraft.label.trim(),
          explanation: styleDraft.explanation.trim(), evidence_ids: [evidenceId], sample_count: 1 }],
        human_note: styleDraft.note.trim() || null,
      });
      setSuccess("风格卡已保存为待审记录；审核通过后才会进入创作。");
      resetPages("styles");
      await loadStyles();
    } catch (requestError) {
      setError(researchError(requestError));
    }
  }

  async function handlePreview(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const input = event.currentTarget.querySelector<HTMLInputElement>("input[type=file]");
    const file = input?.files?.[0];
    if (!file) {
      setError("请先选择要导入的 CSV 文件。");
      return;
    }
    setImporting(true);
    setError("");
    setSuccess("");
    try {
      const result = await api.previewImport(file, file.name);
      setPreview(result);
      setPreviewMapping(Object.fromEntries(result.columns.map((column) => [column, result.mapping_suggestions[column] ?? null])));
      setLastImport(null);
      setSuccess(`已解析 ${result.total_rows} 行；确认映射无误后再写入数据。`);
    } catch (requestError) {
      setError(researchError(requestError));
    } finally {
      setImporting(false);
    }
  }

  async function handleConfirm() {
    if (!preview) return;
    const targets = Object.values(previewMapping).filter((value): value is string => value !== null);
    if (targets.length !== new Set(targets).size) {
      setError("多个 CSV 列不能映射到同一目标字段，请先调整映射。");
      return;
    }
    setImporting(true);
    setError("");
    try {
      const job = await api.confirmImport(preview.id, {
        mapping: previewMapping,
        on_duplicate: "skip",
        match_by: "isbn_and_edition",
        normalize_isbn: true,
        note: null,
      });
      setPreview(null);
      setLastImport(job);
      setSuccess(`导入完成：${job.counts.persisted} 条写入，${job.counts.failed} 行失败，${job.counts.skipped_duplicate} 行重复跳过。`);
      await loadImports();
    } catch (requestError) {
      setError(researchError(requestError));
    } finally {
      setImporting(false);
    }
  }

  const pagination = (tab: ResearchTab) => (
    (pages[tab].history.length > 0 || pages[tab].next) && (
      <div className="research-pagination">
        <button className="button button-muted" disabled={loading || pages[tab].history.length === 0} onClick={() => movePage(tab, "previous")} type="button">上一页</button>
        <span>第 {pages[tab].history.length + 1} 页</span>
        <button className="button button-muted" disabled={loading || !pages[tab].next} onClick={() => movePage(tab, "next")} type="button">下一页</button>
      </div>
    )
  );

  const tabs: { id: ResearchTab; label: string }[] = [
    { id: "metrics", label: "指标证据" },
    { id: "references", label: "内容参考" },
    { id: "styles", label: "风格卡" },
    { id: "imports", label: "CSV 导入" },
  ];

  return (
    <section className="research-desk" aria-label="人工研究台">
      <div className="research-heading">
        <div>
          <span className="eyebrow">RESEARCH DESK</span>
          <h2>人工研究台</h2>
          <p>只保留人工输入或已取得明确授权的材料。估算与热度不是销量；未授权媒体与完整字幕不会进入成片。</p>
        </div>
      </div>

      <div className="research-tabs" role="tablist">
        {tabs.map((tab) => (
          <button
            key={tab.id}
            role="tab"
            aria-selected={activeTab === tab.id}
            className={activeTab === tab.id ? "active" : ""}
            onClick={() => setActiveTab(tab.id)}
            type="button"
          >
            {tab.label}
          </button>
        ))}
      </div>

      {error && <div className="notice notice-error" role="alert"><span>!</span><div><strong>操作未完成</strong><p>{error}</p></div><button aria-label="关闭错误提示" onClick={() => setError("")} type="button">×</button></div>}
      {success && <div className="notice notice-success" role="status"><span>✓</span><p>{success}</p><button aria-label="关闭提示" onClick={() => setSuccess("")} type="button">×</button></div>}

      {activeTab === "metrics" && (
        <div className="research-panel">
          <div className="research-filters">
            <label>口径
              <select value={basisFilter} onChange={(event) => { setBasisFilter(event.target.value as MetricBasis | ""); resetPages("metrics"); }}>
                <option value="">全部</option>
                {Object.entries(BASIS_META).map(([value, meta]) => <option key={value} value={value}>{meta.label}</option>)}
              </select>
            </label>
            <label>平台
              <select value={platformFilter} onChange={(event) => { setPlatformFilter(event.target.value as Platform | ""); resetPages("metrics", "references"); }}>
                <option value="">全部</option>
                <option value="douyin">抖音</option>
                <option value="kuaishou">快手</option>
              </select>
            </label>
            <label>对象
              <select value={objectTypeFilter} onChange={(event) => { setObjectTypeFilter(event.target.value as ObjectType | ""); resetPages("metrics", "references"); }}>
                <option value="">全部</option>
                {Object.entries(OBJECT_TYPE_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </select>
            </label>
            <label>指标
              <select value={metricNameFilter} onChange={(event) => { setMetricNameFilter(event.target.value as MetricName | ""); resetPages("metrics"); }}>
                <option value="">全部</option>
                {Object.entries(METRIC_NAME_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </select>
            </label>
            <button className="button button-muted" disabled={loading} onClick={() => void loadMetrics()} type="button">刷新 <span>↻</span></button>
          </div>

          <details className="research-create">
            <summary>人工录入指标</summary>
            <form className="research-create-grid" onSubmit={(event) => void handleMetricCreate(event)}>
              <label>平台<select value={metricDraft.platform} onChange={(event) => setMetricDraft({ ...metricDraft, platform: event.target.value as Platform })}><option value="douyin">抖音</option><option value="kuaishou">快手</option></select></label>
              <label>对象类型<select value={metricDraft.objectType} onChange={(event) => setMetricDraft({ ...metricDraft, objectType: event.target.value as ObjectType })}>{Object.entries(OBJECT_TYPE_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
              <label>对象标识<input required value={metricDraft.objectRef} onChange={(event) => setMetricDraft({ ...metricDraft, objectRef: event.target.value })} placeholder="已授权对象 ID 或人工标识" /></label>
              <label>指标<select value={metricDraft.metricName} onChange={(event) => setMetricDraft({ ...metricDraft, metricName: event.target.value as MetricName })}>{Object.entries(METRIC_NAME_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
              <label>依据类别<select value={metricDraft.basis} onChange={(event) => setMetricDraft({ ...metricDraft, basis: event.target.value as MetricBasis })}>{Object.entries(BASIS_META).filter(([value]) => value !== "legacy_unclassified").map(([value, meta]) => <option key={value} value={value}>{meta.label}</option>)}</select></label>
              <label>单位<select value={metricDraft.unit} onChange={(event) => setMetricDraft({ ...metricDraft, unit: event.target.value as MetricUnit })}>{["count", "minor_currency", "currency_yuan", "views", "likes", "percent", "rank", "score", "text", "unspecified"].map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
              <label>统计口径说明<input required value={metricDraft.definition} onChange={(event) => setMetricDraft({ ...metricDraft, definition: event.target.value })} placeholder="说明指标如何统计" /></label>
              <label>数值<input type="number" step="any" value={metricDraft.value} onChange={(event) => setMetricDraft({ ...metricDraft, value: event.target.value })} placeholder="未知请留空，不填 0" /></label>
              <label>页面展示文案<input value={metricDraft.valueText} onChange={(event) => setMetricDraft({ ...metricDraft, valueText: event.target.value })} placeholder="例如：已售 10 万+" /></label>
              <label>缺失原因<input value={metricDraft.missingReason} onChange={(event) => setMetricDraft({ ...metricDraft, missingReason: event.target.value })} placeholder="数值未知时填写" /></label>
              <label>时间窗开始<input type="datetime-local" value={metricDraft.windowStart} onChange={(event) => setMetricDraft({ ...metricDraft, windowStart: event.target.value })} /></label>
              <label>时间窗结束<input type="datetime-local" value={metricDraft.windowEnd} onChange={(event) => setMetricDraft({ ...metricDraft, windowEnd: event.target.value })} /></label>
              <label>观察时间<input type="datetime-local" value={metricDraft.observedAt} onChange={(event) => setMetricDraft({ ...metricDraft, observedAt: event.target.value })} /></label>
              <label>来源证据 ID<input value={metricDraft.evidenceId} onChange={(event) => setMetricDraft({ ...metricDraft, evidenceId: event.target.value })} placeholder="有数值时必填" /></label>
              <p>只有获授权商家订单可选“实际销量”；估算、展示文案和热度必须保持各自口径。保存前由服务端校验来源与时间窗。</p>
              <button className="button button-primary" type="submit">保存人工指标</button>
            </form>
          </details>

          {metrics.length === 0 && !loading ? (
            <div className="empty-state"><span>◷</span><strong>还没有指标记录</strong><p>人工录入或通过 CSV 导入。未取得数据时保留缺失原因，不要填 0。</p></div>
          ) : (
            <div className="research-list">
              {metrics.map((metric) => {
                const basis = BASIS_META[metric.basis];
                return (
                  <article className="research-row" key={metric.id}>
                    <div className="research-row-main">
                      <strong>{OBJECT_TYPE_LABELS[metric.object_type]} · {metric.object_ref}</strong>
                      <p>{metric.metric_name} · {metric.metric_definition}</p>
                      <small>{metric.window_start || metric.window_end ? `时间窗 ${formatTime(metric.window_start)} 至 ${formatTime(metric.window_end)}` : "无统计时间窗"} · 观察于 {formatTime(metric.observed_at)}{metric.book_id ? " · 关联图书" : ""}</small>
                    </div>
                    <div className="research-row-side">
                      <span className={`basis-pill ${basis.className}`}>{basis.label}</span>
                      <strong className={metric.value === null && !metric.value_text ? "value-missing" : ""}>
                        {metric.value_text || (metric.value === null ? "未取得" : metric.value.toLocaleString("zh-CN"))}
                      </strong>
                      {metric.value_text && <small>平台展示文案，不代表订单实际销量</small>}
                      {metric.value === null && !metric.value_text && metric.missing_reason && <small className="missing-reason">原因：{metric.missing_reason}</small>}
                      {metric.legacy_backfill && <small className="legacy-note">旧记录回填，核实前不参与排名</small>}
                      {metric.evidence_id ? (
                        <button className="text-button" onClick={() => void openEvidenceDrawer(metric.evidence_id)} type="button">查看来源 <span>↗</span></button>
                      ) : <span className="muted-copy">无关联证据</span>}
                    </div>
                  </article>
                );
              })}
            </div>
          )}
          {pagination("metrics")}
        </div>
      )}

      {activeTab === "references" && (
        <div className="research-panel">
          <div className="inline-warning"><span>!</span><p>参考只保留有使用依据的链接与抽象观察。第三方原视频、音轨、书封、商品图和完整字幕不作为成片素材；链接本身不等于使用许可。</p></div>
          <details className="research-create">
            <summary>人工录入内容参考</summary>
            <form className="research-create-grid" onSubmit={(event) => void handleReferenceCreate(event)}>
              <label>平台<select value={referenceDraft.platform} onChange={(event) => setReferenceDraft({ ...referenceDraft, platform: event.target.value as Platform })}><option value="douyin">抖音</option><option value="kuaishou">快手</option></select></label>
              <label>对象类型<select value={referenceDraft.refType} onChange={(event) => setReferenceDraft({ ...referenceDraft, refType: event.target.value as ObjectType })}>{Object.entries(OBJECT_TYPE_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
              <label>标题<input required value={referenceDraft.title} onChange={(event) => setReferenceDraft({ ...referenceDraft, title: event.target.value })} /></label>
              <label>来源链接<input type="url" value={referenceDraft.url} onChange={(event) => setReferenceDraft({ ...referenceDraft, url: event.target.value })} placeholder="https://…" /></label>
              <label>授权范围<select value={referenceDraft.licenseScope} onChange={(event) => setReferenceDraft({ ...referenceDraft, licenseScope: event.target.value as ContentReference["license_scope"] })}>{Object.entries(LICENSE_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
              <label>观察时间<input type="datetime-local" value={referenceDraft.observedAt} onChange={(event) => setReferenceDraft({ ...referenceDraft, observedAt: event.target.value })} /></label>
              <label>使用依据<input required value={referenceDraft.usageBasis} onChange={(event) => setReferenceDraft({ ...referenceDraft, usageBasis: event.target.value })} placeholder="说明链接和观察范围为何可用" /></label>
              <label>关联证据 ID<input value={referenceDraft.evidenceId} onChange={(event) => setReferenceDraft({ ...referenceDraft, evidenceId: event.target.value })} placeholder="可选" /></label>
              <label className="research-create-wide">抽象观察笔记<textarea value={referenceDraft.note} onChange={(event) => setReferenceDraft({ ...referenceDraft, note: event.target.value })} placeholder="不要粘贴完整字幕或第三方文案" /></label>
              <button className="button button-primary" type="submit">保存内容参考</button>
            </form>
          </details>
          {references.length === 0 && !loading ? (
            <div className="empty-state"><span>▤</span><strong>还没有内容参考</strong><p>人工整理的同类作品观察会出现在这里。</p></div>
          ) : (
            <div className="research-list">
              {references.map((ref) => (
                <article className="research-row" key={ref.id}>
                  <div className="research-row-main">
                    <strong>{OBJECT_TYPE_LABELS[ref.ref_type]} · {ref.title}</strong>
                    <p>{ref.note || "无观察笔记"}</p>
                    <small>{LICENSE_LABELS[ref.license_scope] ?? ref.license_scope} · 依据：{ref.usage_basis}{ref.observed_at ? ` · 观察于 ${formatTime(ref.observed_at)}` : ""}</small>
                    {referenceUrl(ref.reference_url)}
                  </div>
                  <div className="research-row-side">
                    <span className="mini-tag">材料隔离</span>
                    <small>不存原视频 / 音频 / 封面 / 字幕</small>
                  </div>
                </article>
              ))}
            </div>
          )}
          {pagination("references")}
        </div>
      )}

      {activeTab === "styles" && (
        <div className="research-panel">
          <p className="muted-copy">风格卡只表达有证据支持的抽象特征。无证据的草稿不能通过审核；人工修订后需再次审核才能作为创作参考。</p>
          <div className="research-filters">
            <label>审核状态
              <select value={styleStatusFilter} onChange={(event) => { setStyleStatusFilter(event.target.value as StyleCardStatus | ""); resetPages("styles"); }}>
                <option value="">全部</option>
                {Object.entries(STYLE_STATUS_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </select>
            </label>
            <button className="button button-muted" disabled={loading} onClick={() => void loadStyles()} type="button">刷新 <span>↻</span></button>
          </div>
          <details className="research-create">
            <summary>人工创建证据化风格卡</summary>
            <form className="research-create-grid" onSubmit={(event) => void handleStyleCreate(event)}>
              <label>卡片标题<input required value={styleDraft.title} onChange={(event) => setStyleDraft({ ...styleDraft, title: event.target.value })} /></label>
              <label>特征类型<select value={styleDraft.featureKey} onChange={(event) => setStyleDraft({ ...styleDraft, featureKey: event.target.value as StyleFeatureKey })}>{Object.entries(FEATURE_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
              <label>抽象标签<input required value={styleDraft.label} onChange={(event) => setStyleDraft({ ...styleDraft, label: event.target.value })} placeholder="例如：提问式开场" /></label>
              <label>证据 ID<input required value={styleDraft.evidenceId} onChange={(event) => setStyleDraft({ ...styleDraft, evidenceId: event.target.value })} /></label>
              <label className="research-create-wide">解释<textarea required value={styleDraft.explanation} onChange={(event) => setStyleDraft({ ...styleDraft, explanation: event.target.value })} placeholder="描述抽象模式，不复制原句" /></label>
              <label className="research-create-wide">人工备注<textarea value={styleDraft.note} onChange={(event) => setStyleDraft({ ...styleDraft, note: event.target.value })} /></label>
              <button className="button button-primary" type="submit">创建待审卡片</button>
            </form>
          </details>
          {styles.length === 0 && !loading ? (
            <div className="empty-state"><span>✎</span><strong>还没有风格卡</strong><p>由人工观察或提炼流程生成，经审核后进入创作参考。</p></div>
          ) : (
            <div className="research-list">
              {styles.map((card) => (
                <StyleCardRow
                  key={card.id}
                  card={card}
                  evidenceMap={evidenceMap}
                  onReview={(decision, note) => void handleStyleReview(card, decision, note)}
                  onRevision={(title, note, features) => void handleStyleRevision(card, title, note, features)}
                  onOpenEvidence={openEvidenceDrawer}
                />
              ))}
            </div>
          )}
          {pagination("styles")}
        </div>
      )}

      {activeTab === "imports" && (
        <div className="research-panel">
          <form className="import-form" onSubmit={handlePreview}>
            <div className="field"><label>CSV 文件（UTF-8，含 BOM 亦可）</label><input type="file" accept=".csv,text/csv" /></div>
            <div className="form-actions"><button className="button button-secondary" disabled={importing} type="submit">{importing ? "解析中…" : "预览导入"}<span>↗</span></button><span>预览不会写入业务数据；公式前缀单元格会被转义为纯文本。</span></div>
          </form>

          {preview && (
            <div className="import-preview">
              <h3>导入预览 · {preview.filename}</h3>
              <p>行数 {preview.total_rows} · 有效 {preview.valid_rows} · 失败 {preview.failed_rows} · 重复跳过 {preview.skipped_duplicate_rows} · 编码 {preview.encoding_detected}</p>
              <div className="import-mapping" aria-label="CSV 字段映射">
                <strong>核对并调整字段映射</strong>
                <p>不需要的列选“不导入”。下面的行级诊断依据建议映射；确认时服务端会按最终映射重新校验。</p>
                <div className="import-mapping-grid">
                  {preview.columns.map((column) => (
                    <label key={column}>
                      <span>CSV 列：{column}</span>
                      <select value={previewMapping[column] ?? ""} onChange={(event) => setPreviewMapping((current) => ({ ...current, [column]: event.target.value || null }))}>
                        <option value="">不导入</option>
                        {MAPPING_TARGETS.map((target) => <option key={target.value} value={target.value}>{target.label}</option>)}
                      </select>
                    </label>
                  ))}
                </div>
              </div>
              {preview.row_diagnostics.length > 0 && (
                <ul className="diagnostic-list">
                  {preview.row_diagnostics.map((diag, index) => (
                    <li key={`${diag.row_number}-${index}`} className={diag.severity === "error" ? "diag-error" : "diag-warning"}>
                      <strong>第 {diag.row_number} 行</strong> · {diagnosticLabel(diag.code)} {diag.field ? `（${diag.field}）` : ""}<p>{diag.message}</p>
                    </li>
                  ))}
                </ul>
              )}
              <div className="form-actions">
                <button className="button button-primary" disabled={importing} onClick={() => void handleConfirm()} type="button">
                  {importing ? "写入中…" : "按当前映射确认导入"}
                </button>
                <button className="button button-muted" onClick={() => { setPreview(null); setPreviewMapping({}); }} type="button">放弃预览</button>
              </div>
            </div>
          )}

          {lastImport && (
            <div className="import-preview" role="status">
              <h3>最近一次导入结果</h3>
              <p>状态 {importStatusLabel(lastImport.status)} · 写入 {lastImport.counts.persisted} · 失败 {lastImport.counts.failed} · 重复跳过 {lastImport.counts.skipped_duplicate}</p>
              {lastImport.row_diagnostics.length > 0 && <ul className="diagnostic-list">{lastImport.row_diagnostics.map((diag, index) => (
                <li key={`${diag.row_number}-${index}`} className={diag.severity === "error" ? "diag-error" : "diag-warning"}>
                  <strong>第 {diag.row_number} 行</strong> · {diagnosticLabel(diag.code)} {diag.field ? `（${diag.field}）` : ""}<p>{diag.message}</p>
                </li>
              ))}</ul>}
            </div>
          )}

          {imports.length > 0 && (
            <div className="import-history">
              <h3>历史导入作业</h3>
              {imports.map((job) => (
                <article className="research-row" key={job.id}>
                  <div className="research-row-main">
                    <strong>{job.filename}</strong>
                    <p>状态 {importStatusLabel(job.status)} · 写入 {job.counts.persisted} · 失败 {job.counts.failed} · 重复 {job.counts.skipped_duplicate}</p>
                    <small>实体：{job.entity_kinds.map((kind) => kindLabel(kind)).join("、") || "无"} · {formatTime(job.created_at)}</small>
                  </div>
                </article>
              ))}
            </div>
          )}
          {pagination("imports")}
        </div>
      )}

      {drawerEvidence && (
        <aside className="evidence-drawer" aria-label="证据来源">
          <div className="evidence-drawer-head">
            <strong>证据来源</strong>
            <button aria-label="关闭证据抽屉" onClick={() => setDrawerEvidence(null)} type="button">×</button>
          </div>
          <p><strong>{drawerEvidence.source_type === "manual" ? "人工来源" : "已授权来源"}</strong></p>
          <p>{drawerEvidence.source_note}</p>
          <p>使用依据：{drawerEvidence.usage_basis}</p>
          <p>观察于 {formatTime(drawerEvidence.observed_at)}</p>
          {drawerEvidence.source_url && referenceUrl(drawerEvidence.source_url)}
          {drawerEvidence.missing_reason && <p className="missing-reason">指标缺失：{drawerEvidence.missing_reason}</p>}
        </aside>
      )}
    </section>
  );
}

function referenceUrl(url: string | null): React.ReactNode {
  if (!url) return null;
  return <a className="text-button" href={url} rel="noreferrer" target="_blank">查看来源链接 <span>↗</span></a>;
}

function diagnosticLabel(code: ImportRowDiagnostic["code"]): string {
  const labels: Record<ImportRowDiagnostic["code"], string> = {
    encoding_error: "编码错误",
    malformed_row: "行结构错误",
    missing_required_field: "缺少必填字段",
    invalid_book_isbn: "ISBN 无效",
    invalid_number: "数值无效",
    invalid_timestamp: "时间戳无效",
    invalid_platform: "平台无效",
    invalid_url: "链接无效",
    formula_injection: "公式注入风险",
    duplicate_natural_key: "重复记录",
    edition_mismatch_suspected: "同名异版",
    unknown_column: "未知列",
    field_too_long: "字段超长",
    invalid_mapping: "映射无效",
    reference_rights_unsupported: "权利依据不足",
  };
  return labels[code];
}

function importStatusLabel(status: ImportJob["status"]): string {
  const labels: Record<ImportJob["status"], string> = {
    preview: "预览中",
    applied: "已全部写入",
    partially_applied: "部分写入",
    failed: "未写入",
  };
  return labels[status];
}

function kindLabel(kind: ImportJob["entity_kinds"][number]): string {
  const labels: Record<ImportJob["entity_kinds"][number], string> = {
    book_edition: "图书版本",
    promotion_candidate: "拟挂商品",
    metric_observation: "指标",
    evidence: "证据",
    content_reference: "内容参考",
  };
  return labels[kind];
}

function StyleCardRow({
  card,
  evidenceMap,
  onReview,
  onRevision,
  onOpenEvidence,
}: {
  card: StyleCard;
  evidenceMap: Record<string, Evidence>;
  onReview: (decision: "approve" | "reject", note: string) => void;
  onRevision: (title: string, note: string, features: StyleFeature[]) => void;
  onOpenEvidence: (evidenceId: string | null) => void;
}) {
  const [title, setTitle] = useState(card.title);
  const [note, setNote] = useState(card.human_note ?? "");
  const [features, setFeatures] = useState<StyleFeature[]>(card.features);
  const [reviewNote, setReviewNote] = useState("");
  const status = STYLE_STATUS_LABELS[card.review_status];

  useEffect(() => {
    setTitle(card.title);
    setNote(card.human_note ?? "");
    setFeatures(card.features);
  }, [card]);

  return (
    <article className="research-row style-card-row">
      <div className="research-row-main">
        <strong>{card.title}</strong>
        <p>{card.derived_by === "workflow_extraction" ? "流程提炼" : "人工创建"} · 证据样本 {card.evidence_sample_count} · 修订 r{card.revision}</p>
        <ul className="feature-list">
          {card.features.map((feature) => (
            <li key={feature.feature_key}>
              <span className="mini-tag">{FEATURE_LABELS[feature.feature_key]}</span>
              <strong>{feature.label}</strong>
              <small>{feature.explanation} · 样本 {feature.sample_count}</small>
              {feature.evidence_ids.map((evidenceId) => (
                <button key={evidenceId} className="text-button" onClick={() => onOpenEvidence(evidenceId)} type="button">来源 {shortId(evidenceMap[evidenceId]?.source_note)}</button>
              ))}
            </li>
          ))}
          {card.features.length === 0 && <li className="muted-copy">暂无有证据的特征</li>}
        </ul>
        {features.length > 0 && (
          <details className="style-feature-editor">
            <summary>修订抽象特征</summary>
            {features.map((feature, index) => (
              <div className="style-feature-edit" key={`${feature.feature_key}-${index}`}>
                <strong>{FEATURE_LABELS[feature.feature_key]} · {feature.sample_count} 条证据</strong>
                <label>标签<input value={feature.label} onChange={(event) => setFeatures((current) => current.map((item, at) => at === index ? { ...item, label: event.target.value } : item))} /></label>
                <label>解释<textarea value={feature.explanation} onChange={(event) => setFeatures((current) => current.map((item, at) => at === index ? { ...item, explanation: event.target.value } : item))} /></label>
              </div>
            ))}
            <small>修订仅改变抽象描述；证据 ID 与样本数保留。保存后需再次审核。</small>
          </details>
        )}
      </div>
      <div className="research-row-side">
        <span className={`review-pill ${card.review_status === "approved" ? "approved" : card.review_status === "rejected" ? "rejected" : ""}`}>{status}</span>
        {card.review_status !== "approved" && (
          <div className="style-card-actions">
            <button className="button button-primary" onClick={() => onReview("approve", reviewNote)} type="button">通过</button>
            <button className="button button-danger" onClick={() => onReview("reject", reviewNote)} type="button">退回</button>
            <input value={reviewNote} onChange={(event) => setReviewNote(event.target.value)} placeholder="审核备注" />
          </div>
        )}
        <div className="style-card-actions">
          <input value={title} onChange={(event) => setTitle(event.target.value)} placeholder="标题" />
          <input value={note} onChange={(event) => setNote(event.target.value)} placeholder="人工备注" />
          <button className="button button-muted" onClick={() => onRevision(title, note, features)} type="button">保存修订</button>
        </div>
      </div>
    </article>
  );
}
