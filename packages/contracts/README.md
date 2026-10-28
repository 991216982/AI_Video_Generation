# 共享契约 v0.1

`openapi.yaml` 是阶段 1 的 HTTP 源契约；`events.schema.json` 定义按 `GET /runs/{run_id}/events` 发送的 SSE `data` JSON。`examples/` 只含虚构的人工输入，用于开发和端到端测试，不代表真实商品、平台授权或已核实的书籍事实。契约字段使用 snake_case、UTC RFC 3339 时间戳、UUID 字符串；金额（若后续引入）使用最小货币单位整数。

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

| 当前状态 | 可迁移至 |
| --- | --- |
| `queued` | `running`, `cancelled` |
| `running` | `waiting_review`, `succeeded`, `failed`, `cancelled` |
| `waiting_review` | `running`, `cancelled` |
| `failed` | `queued`, `cancelled`（重试同一 run 并保留事件） |
| `succeeded`, `cancelled` | 终态 |

API 请求和事件均携带稳定 ID，写操作支持 `Idempotency-Key`。标准错误包含 `code`、`message`、`request_id`，可选 `details`。客户端不可假定任务同步完成；`POST /runs` 和 `POST /renders` 返回 202。契约变更由集成负责人统一修改、同步样例并通知三位 Agent。
