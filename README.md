# 图书短视频创作工作台

本仓库从人工核实的图书事实与创作者原创观点出发，制作 9:16、20–45 秒的图书短视频草稿及发布包。首版由创作者在抖音或快手客户端自行发布、核对商品并手动挂车。阶段 0 基线已建立；阶段 1 的本地实现已通过虚构样例集成测试，真实商品与平台发布尚待人工验收。

产品范围与执行顺序见 [文档索引](docs/README.md) 和 [开发计划](docs/05-开发计划与多Agent执行.md)。共享接口以 `packages/contracts/openapi.yaml`、`packages/contracts/events.schema.json` 和 `packages/contracts/examples/` 为准。

## 本地启动

要求 Python 3.10+、Node.js 20.19+、npm。生成实际 MP4 另需 FFmpeg、ffprobe 和中文字体。当前本地试点用 SQLite、独立轮询 worker 和进程内 LangGraph checkpoint；无需平台账号、模型密钥或第三方素材。

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e "apps/api[dev]"
.venv\Scripts\python -m uvicorn book_video.main:app --reload --port 8000
```

另开终端：

```powershell
.venv\Scripts\python -m book_video.worker
cd apps/web
npm ci
npm run dev
```

API 健康检查为 `http://127.0.0.1:8000/healthz`；Web 默认在 `http://localhost:5173`。Web 初始为虚构演示模式；切换到“API 工作区”才会写入本地 API。Worker 的 `--once` 模式至多处理一个待办任务后退出。API 和 worker 须使用同一 `BOOK_VIDEO_DATABASE_PATH` 与 `BOOK_VIDEO_OUTPUT_DIR`；默认均在仓库内的 `.local/`。环境变量模板见 `.env.example`。

## 检查

```powershell
.venv\Scripts\python scripts/verify_stage0.py
.venv\Scripts\python scripts/smoke_stage0.py
.venv\Scripts\python -m pytest apps/api/tests
cd apps/web
npm run typecheck
npm run build
```

端到端媒体测试在未安装 FFmpeg/ffprobe 时跳过；集成验收必须安装两者并确认该测试运行。若覆盖默认 API 地址，在 `apps/web/.env.local` 中设置 `VITE_API_BASE_URL`。`examples/` 中均为虚构的人工输入，不代表真实图书、商品资格或平台数据。商用声音许可尚未核实，因此当前渲染为静音文字/自制图形视频。审核、导出和下载均不执行平台发布或挂车。
