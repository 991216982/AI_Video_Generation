# 共享契约 v0.3（G3 冻结稿）

`openapi.yaml` 是 W3 的 HTTP 源契约；`events.schema.json` 定义按 `GET /runs/{run_id}/events` 发送的 SSE `data` JSON。`examples/` 只含虚构输入和元数据，不含真实录音、商品或模型输出。契约字段使用 snake_case、UTC RFC 3339 时间戳、UUID 字符串；费用使用最小货币单位整数。当前运行代码为 v0.3；W3 已在虚构样例上完成本地集成验收，真实商品、本人录音许可与平台发布挂车仍待账号持有人手动验收。

G3 起点：`main` 的 `c6f3e3eb215453ed9093145041d5f1d10a0b4151`。W2 复核所述 `8e17dc6` 到该提交只新增 G3 启动计划及文档索引。契约检查：`python scripts/verify_stage0.py`。

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
