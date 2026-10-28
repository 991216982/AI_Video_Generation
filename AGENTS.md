# 仓库协作规则

先读 `docs/README.md`、`docs/05-开发计划与多Agent执行.md` 和 `packages/contracts/README.md`。首版目标是本人可用的图书录入 → 原创脚本 → 人工审核 → 文字动效成片 → 发布包。用户自行在平台客户端发布和挂车。

## 路径归属

- 集成负责人独占 `docs/`、`packages/contracts/`、`infra/`、`.github/`、`scripts/`、根目录配置，以及跨模块接线和端到端测试。
- Agent A 独占 `apps/api/src/book_video/api/`、`core/`、`domain/`、`persistence/`、`connectors/manual/`、`apps/api/migrations/`；其测试放在 `apps/api/tests/agent_a/`。
- Agent B 独占 `apps/web/`，包含前端测试。
- Agent C 独占 `apps/api/src/book_video/workflows/`、`media/`、`tasks/`；其测试放在 `apps/api/tests/agent_c/`。
- `apps/api/src/book_video/main.py`、`worker.py`、`apps/api/pyproject.toml` 和 `apps/api/tests/integration/` 由集成负责人维护。

共享文件有变更需要先向集成负责人报告：说明原因、建议字段与兼容影响。实现 Agent 不自行编辑 OpenAPI、事件定义、样例、根配置、锁文件（前端的 `apps/web/package-lock.json` 由 Agent B 维护）或其他 Agent 的路径。所有人共享同一工作目录，不要重置、清理或提交其他人的修改；Git 提交由集成负责人统一完成。

## 数据与素材边界

- 只接人工输入或已经取得明确授权的数据；保留来源、范围、时间窗与缺失原因。无授权时不抓取平台、不声称竞品销量。
- 未获许可的书封、内文、商品图、视频、音乐、人物肖像和声音不得进入成片。素材权限不明视为不可用。
- 不做自动发布或自动挂车。`PublicationPackage` 仅提供人工操作核对卡；不能把拟挂商品视为平台已确认可推广。
- 不在仓库中放真实账号、token、密钥、个人数据或有版权风险的样例。测试数据使用虚构事实，明确标识为演示数据。

## 本轮检查

阶段 0：`python scripts/verify_stage0.py`、`python -m pytest apps/api/tests`、`npm run typecheck` 与 `npm run build`（在 `apps/web/`）。阶段 1：增加领域/API、编排恢复/幂等、ffprobe 和一条端到端样例；有失败先报告原因和影响。
