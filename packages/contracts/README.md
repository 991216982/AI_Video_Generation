# 共享契约 v0.2（G2 冻结稿）

`openapi.yaml` 是 W2 的 HTTP 源契约；`events.schema.json` 定义按 `GET /runs/{run_id}/events` 发送的 SSE `data` JSON。`examples/` 只含虚构的人工输入，用于开发和端到端测试，不代表真实商品、平台授权或已核实的书籍事实。契约字段使用 snake_case、UTC RFC 3339 时间戳、UUID 字符串；金额（若后续引入）使用最小货币单位整数。当前运行代码仍是 v0.1；W2-A/B/C 实现此 v0.2 契约后才将 API 视为 v0.2 可用。

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
