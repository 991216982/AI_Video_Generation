# 共享契约 v0.4（G4 冻结稿）

`openapi.yaml` 是 W4 的 HTTP 源契约；`events.schema.json` 定义按 `GET /runs/{run_id}/events` 发送的 SSE `data` JSON。`examples/` 只含虚构输入和元数据，不含真实录音、商品、抓取结果或模型输出。契约字段使用 snake_case、UTC RFC 3339 时间戳、UUID 字符串；费用使用最小货币单位整数。v0.4 端点已在本地单用户基线随 W4 集成落地并通过虚构样例测试；真实平台发布、账号资格与远端 CI 仍待账号持有人和集成负责人核实，本稿不宣称真实带货验收已通过。

本地集成基线：从 `e475a95` 建立 `codex/integrate-w4-crowdtest` 分支，将五份众测产物中的 W4 实现审查后接入。远端当前 SHA 与本分支 CI 尚未核实；本地检查见 [`docs/23-W4众测产物集成与修复记录.md`](../../docs/23-W4众测产物集成与修复记录.md)。契约检查：`python scripts/verify_stage0.py`。

## 必须遵守的语义

- 所有私有记录带 `owner_user_id`。阶段 1 本地单用户模式也必须在服务端设定并校验，不能由客户端任意指定他人的 ID。
- `BookEdition` 是具体版本，不凭书名自动合并。`BookFact` 有出处或 `verification_status=human_confirmed`，没有依据的事实不能进入成片。
- `PromotionCandidate` 是**拟挂商品**；`eligibility_status=unverified` 是默认值。只有创作者在自己的平台后台核对后才可标记 `manually_confirmed`，系统不自动判断平台资格。
- `Evidence` 的 `source_type` 仅为 `manual` 或未来的 `licensed`。缺少指标时返回 `null` 和 `missing_reason`，不填 0。阶段 1 不提供竞品销量字段。
- `AssetRights` 缺许可凭据或过期时 `usage_status=blocked`。阶段 1 默认只使用自制文字/图形；语音仅限本人录音或已核实商用许可。
- `RunStatus` 只能按下表迁移；每次变化产生一个单调递增 `sequence` 的 `RunEvent`。SSE 可按 `Last-Event-ID` 从上次 `sequence` 继续；重复事件以 `(run_id, sequence)` 去重。
- `ScriptVersion` 审核通过后不可原地修改。修改要创建下一版本。渲染请求必须携带已通过审核的 `script_version_id`、同版本 `promotion_candidate_id` 及 `idempotency_key`。
- `PublicationPackage` 的 `publication_state` 只表示本地准备情况；不表示视频已发布或商品已挂车。导出包始终包含人工检查项。
- `video_download_url` 指向服务端按当前所有者校验的 `GET /renders/{render_id}/download`；仅返回本服务已完成的 MP4，不允许客户端指定文件路径。
- 外部模型调用前持久化 `invocation_key` 占用；恢复时先读服务端结果、费用与私有结构化脚本。供应商无法按 key 查询结果且不保证请求幂等时，未确认的调用记录为 `manual_reconciliation_required`，费用未知保留原因，运行停止并等待人工核对，不自动重发。

## G4 增量、迁移与兼容影响

G4 为人工研究台冻结指标证据、内容参考、证据化风格卡和 CSV 导入契约；原 G4 契约阶段未实现 W4。当前集成分支已接入并修复 W4 实现，不启动 G5。所有新资源仍是当前 owner 私有；owner 由服务端从认证身份设定，客户端不得指定他人 ID，跨 owner 读写或引用一律按 404 `not_found`。

### 1. 指标口径：四类证据分层，缺失保留原因

`MetricObservation`（`/metrics`）把研究指标从旧 `Evidence.metric_*` 扁平字段拆成独立、可追溯的记录。`basis` 为四类口径，不可混排：

| basis | 含义 | 可否称实际销量 |
| --- | --- | --- |
| `authorized_order_sales` | 获授权商家订单实际销量/销售额（授权店铺范围、统计期内） | **唯一**可表述为实际销量的口径 |
| `platform_listing_display` | 平台页面展示值/展示档位文案（如“已售10万+”） | 否；展示值不等于订单，不等于抓取授权 |
| `third_party_or_manual_estimate` | 获许可第三方服务估算或人工估算 | 否；界面必须标注“估算” |
| `engagement_signal` | 播放、点赞、评论、分享、粉丝、排行、搜索热度等热度信号 | 否；只表示关注度/内容表现，不是销量 |
| `legacy_unclassified` | v0.3 旧证据回填、口径未能分类的历史记录 | 否；重新核实前不参与排名，仅作待整理数据 |

字段约束（schema 与领域层共同执行）：

- 有数值的观察必须带同 owner 的 `evidence_id`、`metric_definition`（口径/范围/算法）和 `observed_at`；无来源、无定义或无时间窗的数值不得作为确定值展示。
- `value=null` 表示数据缺失，**必须**有非空 `missing_reason`；`value=0` 只表示有依据的实测零，禁止用 0 冒充未知。非数值展示档位用 `value_text`，不参与数值排名。
- `window_start/window_end` 同时存在或同时为空；领域层校验起始不晚于结束且时间戳带时区。
- 可比性：排行与图表必须限定同一 `platform`、`object_type`/对象范围、`metric_name`、`unit`、`basis` 和时间窗；跨平台、跨口径或证据不足时显示“证据不足/未取得”，不强制生成绝对名次。`confidence`、`expires_at`、`note` 为可选辅助字段。
- 旧 `Evidence.metric_*` 字段保留可读，新观察一律走 `/metrics`。

### 2. 内容参考：只存有使用依据的研究材料

`ContentReference`（`/content-references`）只保存研究所需的最小信息：类型（商品/账号/作品/图书版本）、平台原生稳定 ID 或人工稳定标签、链接、抽象观察笔记、授权范围与使用依据、关联证据和观察时间。

- `license_scope` ∈ `own_account`（自有账号）、`licensed_data_service`（获许可数据服务）、`manual_observation`（人工观察）。链接不等于授权。
- `material_statement` 固定为 `no_third_party_media_or_verbatim_transcript`：**不**存储第三方原视频、音轨、书封/商品图、完整字幕或逐字文案作为成片素材；`captured_fields` 仅声明允许留存的最小字段。未授权第三方媒体永远不能进入渲染素材链。
- 引用的 `evidence_id` 必须同 owner；跨 owner 引用按 404。`reference_url` 仅限 http/https，不接受客户端本地路径或媒体字节（沿用 `Evidence.source_url` 与 `RenderCreate.audio_asset_id` 的校验口径）。

### 3. 风格卡：只表达有证据支持的抽象特征，人工可修订审核

`StyleCard`（`/style-cards`）输出抽象标签与解释（开场、结构、节奏、画面、字幕、语气、CTA 等），不复制第三方字幕、原句或素材。

- `features[]` 每个特征关联 ≥1 条证据、`sample_count` ≥1；卡片整体 `evidence_ids` 与 `reference_ids` 可回溯。`abstract_only` 恒为 `true`。
- 审核状态 `draft` / `pending_review` / `approved` / `rejected`。**无证据的卡片只能是草稿/待审**，不得标 `approved`；`approve` 时若证据为空或特征无证据，返回 422。已通过卡片不覆盖人工修订；修订走 `PATCH` 携带 `expected_revision`，冲突返回 409 `revision_conflict`，`revision` 递增。
- `derived_by` ∈ `manual`、`workflow_extraction`；工作流提炼结果必须先关联证据并经人工审核才能作为创作输入。

### 4. CSV 导入：安全、可纠错、可重复执行

导入分三步：`POST /imports/preview`（解析不落库）→ 人工核对映射 → `POST /imports/{id}/confirm`（按映射写入，部分失败逐行记录）。预览草稿有过期时间，过期不能确认。

- 文件：UTF-8（允许 BOM）；大小、行数服务端上限；公式前缀（`=`、`+`、`-`、`@` 等）单元格按纯文本转义并标记 `formula_injection`，绝不执行。`filename_hint` 仅作展示。
- 行级诊断 `ImportRowDiagnostic` 带行号、字段、稳定错误码与级别（`error`/`warning`）；错误码见 `ImportRowDiagnostic.code`（`encoding_error`、`malformed_row`、`missing_required_field`、`invalid_book_isbn`、`invalid_number`、`invalid_timestamp`、`invalid_platform`、`invalid_url`、`formula_injection`、`duplicate_natural_key`、`edition_mismatch_suspected`、`unknown_column`、`field_too_long`、`invalid_mapping`、`reference_rights_unsupported`）。
- 图书版次：自然键默认 `isbn_and_edition`（ISBN + 版次标签），可选 `isbn` 或 `title_author_fuzzy`；**同名异版不自动合并**，命中可疑差异给 `edition_mismatch_suspected` 警告。ISBN 可规范化（去连字符/空白），`normalize_isbn` 控制。
- 重复与幂等：`on_duplicate` ∈ `skip`（默认）、`update`；同文件同映射重放结果可重复。写请求带 `Idempotency-Key`；同 key 同请求只产生一次导入结果，同 key 不同请求返回 409 `idempotency_key_conflict`。预览本身不落业务数据。
- 部分失败：成功行提交，失败行进诊断，`status` ∈ `applied` / `partially_applied` / `failed`；`counts` 汇总 total/parsed/persisted/skipped_duplicate/failed/warnings。`entity_kinds` 记录本次涉及的实体类型。
- 支持导入的实体：图书版次、拟挂商品、指标观察、证据、内容参考；风格卡不通过 CSV 批量生成。

### 5. 分页、过滤、错误码与事件

- 新列表（`/evidence`、`/metrics`、`/content-references`、`/style-cards`、`/imports`）沿用 v0.2 游标分页：仅当前 owner，`limit` 1–100 默认 20，`cursor` 绑定 owner 与过滤条件，换条件须丢弃旧游标；末页 `next_cursor=null`，空列表 `items=[]`。指标按 `(observed_at, id)` 倒序，其余按 `(created_at, id)` 倒序稳定排序。过滤参数见各端点定义；非法游标返回 422 `invalid_cursor`。
- 错误正文沿用 `ApiError(code, message, request_id, details)`。本轮新增/复用码：`not_found`(404)、`validation_error`(422)、`revision_conflict`(409)、`idempotency_key_conflict`(409)、`invalid_cursor`(422)；导入作业级 422 码 `import_preview_expired`、`import_file_too_large`、`import_too_many_rows`、`import_mapping_invalid`，409 `import_already_applied`。
- **事件判断：本轮不新增 SSE 事件。** CSV 导入是请求/响应内的同步流程（行数有上限，结果在行级诊断里返回），研究与风格卡写入是人工驱动、不属于 run 生命周期；W4-C 的证据化提炼在现有 run/确定性流程内消费服务端字段。若 W4 集成后确有后台长任务进度需求，再由集成负责人走契约变更增补事件，当前 `events.schema.json` 的 v0.3 事件集合保持冻结。

### 6. 迁移默认值与 v0.3 兼容影响

- 旧 `Evidence` 中带 `metric_name/metric_value/metric_window_*` 的记录：W4-A 迁移时回填一条 `MetricObservation`，`basis=legacy_unclassified`、`legacy_backfill=true`、`confidence=null`、`evidence_id` 指向原证据；数值缺失保留原 `missing_reason`。这些记录在重新核实前不参与排行。
- 旧 `BookEdition`、`PromotionCandidate`、创作链记录字段与语义不变；历史数据不被默认视为授权数据或已核实资格。
- v0.3 客户端：所有旧路径与请求字段保留；新端点为增量。新响应字段可被旧客户端忽略；旧 v0.3 服务端不提供新端点，前后端须同版本部署。严格枚举事件的消费者无需改动（无新事件类型）。旧 `Evidence.metric_*` 仍可读，但新研究数据应通过 `/metrics` 查询。

## G3 增量、迁移与兼容影响

1. **拟挂商品创建必须未核实**：`POST /promotion-candidates` 仅允许省略状态或传 `eligibility_status=unverified`、`eligibility_checked_at=null`，且不得带 `confirmation_basis`。提交 `manually_confirmed`、非空确认时间或创建时确认依据一律返回 422 `validation_error`，不得静默降级为未核实。人工确认仍用现有 PATCH，必须有非空说明及 `confirmation_basis=creator_account_backend`，服务端填写确认时间、人和修订号。原 v0.2 直接创建即确认的客户端会收到 422，需改为创建后 PATCH；此拦截已在 v0.3 运行代码实现。已存在确认记录不自动撤销，但迁移需核查其形成途径；缺少可审计人工确认依据者降为 `unverified`，不可凭原时间戳视为已核实。
2. **事实修订**：新 `PATCH /books/{book_id}/facts/{fact_key}` 使用 `expected_revision`；成功追加 `BookFactRevision`，不覆写历史。`GET .../revisions` 按修订号升序读取。键限定于该具体版次的已有事实；不同版次不能合并。`source_verified` 要有非空来源说明或当前所有者可用的 `evidence_id`；`human_confirmed` 要有非空核对说明；无依据事实仍可保存为 `unverified`，不得用于成片。修订冲突返回 409 `revision_conflict`，无权访问按 404。旧事实迁移为 revision 1；`BookEdition.facts[].revision` 是新增响应字段，旧客户端可忽略。脚本保存其使用的 `fact_revision_refs` 快照；事实后续变化不改已审核脚本，旧脚本如依赖已撤回的事实，重新审核前不得渲染。
3. **创作与版本**：复用既有受众、平台、时长、原创角度、禁用表述与事实出处；只新增可选 `tone_instruction`。`ScriptVersion.generation_result_id` 区分模型草稿和人工改稿；`fact_revision_refs` 固定核对过的事实版本。`based_on_version_id` 仍表明改稿继承，已审核版本不可原地改写。历史项目缺 `tone_instruction` 用 null，历史脚本的生成来源按本地确定性生成迁移或留 null，不臆造外部模型调用。新增响应字段可由 v0.2 客户端忽略。
4. **本人录音与许可**：`POST /audio-assets` 接收文件字节、SHA-256、来源和明确权利声明；服务端核对摘要、MIME/实际媒体、大小和 ffprobe 时长，保存不暴露的私有引用，返回 `AudioAsset`。首条试用优先 `self_recorded_voice`；`licensed_voice` 只有商业使用范围、证明说明及有效期齐备且仍有效才可 `allowed`。权限不明、过期或跨所有者引用返回 422 `asset_rights_blocked`（跨所有者按 404），不得开始渲染。`RenderCreate.audio_asset_id=null` 表示静音，非空只能引用本服务当前允许的资产；客户端绝对路径、网络 URL 或媒体字节均不能作渲染引用。已有无音轨渲染按 null 迁移。当前 `AssetRights` 原字段保留，服务端仍须复核，不接受客户端单独声明 `allowed` 作为录音许可。
5. **模型结果与费用**：每个实际或确定性调用有 `GenerationResult`，其 `invocation_key` 在同一逻辑调用重试时稳定；结构化脚本关联 `script_version_id`，失败填 `reason_code`，输入/输出只存摘要，不存密钥或完整提示词。`ModelCostRecord` 以 `generation_result_id` 唯一，实际费用按账单或供应商返回的最小货币单位记录；未知费用用 `amount_minor=null + missing_reason`，确定性假实现为 0，绝不把估算写成已产生费用。失败调用也可能有实际费用。相同 key 重放不产生第二条结果或费用，新增调用才可新计费。通过两个 owner 范围内的 GET 读取；`run.generation_completed`、`run.generation_failed`、`run.cost_recorded` SSE 事件携带结果/费用 ID，沿用单调 `sequence`。供应商错误用 `provider_unavailable`、`provider_timeout`、`invalid_output` 等稳定原因码；缺事实依据或禁用表述阻止审核通过。CI 用确定性无密钥假实现；外部模型及 TTS 许可另行核实后启用。

所有原 v0.2 路径继续存在。新增写请求的 owner、版本、许可、事实和幂等校验由服务端负责；错误正文保持 `ApiError(code,message,request_id,details)`。v0.3 新端点已随 W3 集成；旧 v0.2 服务端不提供这些路径，前后端需同版本部署。发布包仍只提供人工发布与挂商品核对卡，不代表平台已确认可推广。

## G2 增量与兼容影响

1. **重开工作区**：新增 `GET /books`、`GET /creative-projects` 的游标分页与 `GET /creative-projects/{project_id}/workspace`。列表只返回当前所有者的记录，按 `(created_at, id)` 倒序稳定排序；`limit` 1–100，默认 20。`cursor` 是与所有者、过滤条件绑定的不透明字符串；换查询条件需丢弃旧游标。最后一页 `next_cursor=null`，空列表 `items=[]`。工作区详情返回精确图书版本、拟挂商品、证据、全部脚本版本、运行、渲染及最新发布包；无发布包用 `null`，不能填假数据。
2. **拟挂商品资格**：新增 `PATCH /promotion-candidates/{candidate_id}/eligibility`。这只是创作者在自己平台账号后台核对后的人工记录，不是平台授权或自动验证。`manually_confirmed` 必须附非空说明及 `confirmation_basis=creator_account_backend`；服务端写入 `eligibility_checked_at`、`eligibility_checked_by_user_id` 并使 `eligibility_revision` 加 1。改回 `unverified` 时清除确认时间/人；`expected_revision` 不匹配返回 `revision_conflict`。资格撤回后，后续审核/渲染不得依赖旧确认，发布包的商品检查项在读取时按当前状态重新计算。
3. **取消与重试**：新增 run/render 的 `/cancel`、`/retry`，写操作要求 `Idempotency-Key`。队列中或待审核的 run 可直接 `cancelled`；执行中的 run 保持 `running` 并设置 `cancel_requested_at`，产生 `run.cancel_requested`，worker 确认停止后才变 `cancelled`。render 同理。只有 `failed` 可重试，保留同一 ID、原始不可变输入和历史，`attempt` 增 1 后回到 `queued`；已成功或已取消的任务不可重试。相同 key + 相同请求只产生一次状态迁移；同 key 不同请求返回 `idempotency_key_conflict`。worker 在不可逆副作用前后检查取消，取消后不能再把任务标为成功。
4. **事件**：`RunEvent.type` 增加 `run.cancel_requested`、`run.retry_requested`，可带 `attempt`。事件 `sequence` 继续按 run 单调递增；SSE 的 `id` 仍为十进制 `sequence`，`Last-Event-ID` 只回放更大序号。重试可复用既有脚本/渲染结果，但不得产生第二个有效产物或重复记费。
5. **错误码**：`invalid_cursor`（422）、`revision_conflict`（409）、`invalid_state_transition`（409）、`idempotency_key_conflict`（409）、`review_resume_unavailable`（409）。错误正文沿用 `ApiError` 的 `code/message/request_id/details`；未授权资源仍按原 owner 隔离返回 404。

v0.2 保留全部 v0.1 路径和请求字段。旧客户端忽略新增响应字段时可继续使用；但旧服务端尚未提供新路径，也不会返回 v0.2 要求的 `Run.attempt/cancel_requested_at`、`RenderJob.attempt/cancel_requested_at/updated_at` 与 `PromotionCandidate.eligibility_revision/eligibility_checked_by_user_id`。W2 实现须迁移历史记录，按初始 `attempt=1`、`eligibility_revision=1`、`cancel_requested_at=null` 补齐；前端切换新能力前需与后端同版本部署。

| 当前状态 | 可迁移至 |
| --- | --- |
| `queued` | `running`, `cancelled` |
| `running` | `waiting_review`, `succeeded`, `failed`, `cancelled` |
| `waiting_review` | `running`, `cancelled` |
| `failed` | `queued`, `cancelled`（重试同一 run 并保留事件） |
| `succeeded`, `cancelled` | 终态 |

API 请求和事件均携带稳定 ID，写操作支持 `Idempotency-Key`。标准错误包含 `code`、`message`、`request_id`，可选 `details`。客户端不可假定任务同步完成；`POST /runs` 和 `POST /renders` 返回 202。契约变更由集成负责人统一修改、同步样例并通知三位 Agent。
