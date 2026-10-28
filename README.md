# 图书短视频创作工作台

本仓库从人工核实的图书事实与创作者原创观点出发，制作 9:16、20–45 秒的图书短视频草稿及发布包。首版由创作者在抖音或快手客户端自行发布、核对商品并手动挂车。当前仅建立阶段 0 基线，业务能力尚未实现。

产品范围与执行顺序见 [文档索引](docs/README.md) 和 [开发计划](docs/05-开发计划与多Agent执行.md)。共享接口以 `packages/contracts/openapi.yaml`、`packages/contracts/events.schema.json` 和 `packages/contracts/examples/` 为准。

## 本地启动

要求 Python 3.10+、Node.js 20.19+、npm。阶段 0 无需 PostgreSQL、Redis、对象存储、模型密钥或平台账号。

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e "apps/api[dev]"
.venv\Scripts\python -m uvicorn book_video.main:app --reload --port 8000
```

另开终端：

```powershell
.venv\Scripts\python -m book_video.worker --once
cd apps/web
npm ci
npm run dev
```

API 健康检查为 `http://127.0.0.1:8000/healthz`；Web 默认在 `http://localhost:5173`。Worker 的 `--once` 模式只验证入口和配置，不处理任务。环境变量模板见 `.env.example`。

## 检查

```powershell
python scripts/verify_stage0.py
python scripts/smoke_stage0.py
python -m pytest apps/api/tests
cd apps/web
npm run typecheck
npm run build
```

阶段 0 不接入平台数据、第三方图书素材或 TTS；`examples/` 中均为虚构的人工输入。商用声音许可尚未核实，因此阶段 1 默认静音或创作者自录音。渲染需要 FFmpeg/ffprobe，进入阶段 1 媒体验收前安装。
