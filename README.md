# Requirement Analyze Agent

Requirement Analyze Agent 是基于 `it-design-agent` 框架改造的 BA 需求分析智能体平台。输入从面向 SE 的 IT 设计需求，调整为 RR（Raw Requirements）和可选竞品参考；专家协作产物最终聚合为 IR（IT Requirements），供后续 SE/IT 设计承接。

## 改造范围

已直接复用的框架能力：

- FastAPI + LangGraph 编排框架、SSE 事件流、项目/版本管理。
- React/Vite 管理端、项目配置、专家中心、阶段编排、人工确认与重试机制。
- 工具协议与安全边界：文件读取、搜索、结构提取、知识库/数据库/仓库查询、产物写入和验证。
- 本地元数据、checkpoint、产物治理、交互记录和日志机制。

已按 BA/RR/IR 改造的内容：

- `config/phases.yaml`：阶段改为需求澄清、规则分析、单据作业、流程控制、集成需求、IR 交付。
- `experts/*.expert.yaml`：替换为 BA 专家，包括需求澄清、规则管理、单据作业、流程控制、集成需求、IR 聚合和 IR 验证。
- `skills/*/SKILL.md` 与模板：替换为 RR 分析和 IR 输出方法。
- 后端默认专家映射、聚合专家 ID、上游产物依赖、主题归属、输出预算、产物归属推断。
- Planner 和需求澄清话术：从 IT Design Pipeline 调整为 BA Requirement Analysis Pipeline。

仍建议后续继续改造：

- `api_server/tests/` 仍大量覆盖原 IT 设计场景，当前只保证 Python 编译和新 YAML 解析通过；完整自动化测试需要按 BA 场景重写。
- 前端仍保留原管理端布局，只做了轻量文案调整；可继续细化上传区域，明确 RR 与竞品参考材料。
- Expert Creator 的默认模板已调整为分析阶段，但生成新专家时仍建议人工校正 BA 领域边界。

## 内置专家

| Expert ID | 中文名 | 阶段 | 主要输出 |
|---|---|---|---|
| `requirement-clarification` | 需求澄清专家 | `ANALYSIS` | `requirement-clarification.md`, `scope-and-assumptions.md`, `glossary.md` |
| `rules-management` | 规则管理专家 | `RULES` | `business-rules.md`, `decision-tables.md`, `rule-parameters.yaml` |
| `document-operation` | 单据作业专家 | `OPERATIONS` | `document-operations.md`, `field-requirements.yaml`, `operation-permissions.md` |
| `process-control` | 流程控制专家 | `PROCESS` | `process-requirements.md`, `state-transition.md`, `exception-handling.md` |
| `integration-requirements` | 集成需求专家 | `INTEGRATION` | `integration-requirements.md`, `external-system-matrix.yaml`, `data-exchange-events.md` |
| `ir-assembler` | IR聚合专家 | `DELIVERY` | `it-requirements.md`, `requirement-traceability.json`, `acceptance-criteria.md`, `open-questions.md` |
| `validator` | IR验证专家 | `DELIVERY` | `validation-report.md` |
| `expert-creator` | 专家构建器 | 系统专家 | 创建 expert profile、SKILL、模板和脚本，不参与普通项目编排 |

## 快速启动

前置要求：

- Python 3.11+
- Node.js 18+

后端：

```bash
cd requirement-analyze-agent
python -m venv venv
venv\Scripts\activate
pip install -r api_server\requirements.txt
copy .env.example .env
cd api_server
python main.py
```

前端：

```bash
cd requirement-analyze-agent\admin-ui
npm install
npm run dev
```

Windows 也可以直接运行：

```bash
start-all.bat
```

默认地址：

- 前端：[http://localhost:5173](http://localhost:5173)
- 后端：[http://localhost:8000](http://localhost:8000)
- Swagger：[http://localhost:8000/docs](http://localhost:8000/docs)

## 运行逻辑

1. 后端启动时扫描 `experts/*.expert.yaml` 和对应 `skills/<expert-id>/SKILL.md`。
2. 用户创建项目，输入 RR，并可上传竞品参考、存量需求、流程图、样例单据等材料。
3. `requirement_clarifier` 先处理过短或关键边界不清的 RR，必要时请求人工补充。
4. Planner 从项目启用专家中推荐参与专家，并让用户确认专家选择。
5. Supervisor 按 `config/phases.yaml` 和专家依赖推进任务。
6. 各 BA 专家生成结构化需求分析产物。
7. `ir-assembler` 聚合所有上游产物生成 IR 交付包。
8. `validator` 校验 IR 的完整性、一致性、可追踪性和可验收性。

## 环境变量

后端读取仓库根目录 `.env`：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LLM_PROVIDER` | `openai` | 当前实现归一化为 OpenAI-compatible provider |
| `OPENAI_API_KEY` | 空 | 系统级 LLM API key |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible base URL |
| `OPENAI_MODEL_NAME` | `gpt-4o` | 系统级默认模型 |
| `REQUIREMENT_ANALYZE_AGENT_METADATA_KEY` | 自动生成 | 元数据敏感字段加密密钥 |
| `LLM_MIN_CALL_INTERVAL_SECONDS` | `0` | LLM 请求开始间隔 |
| `LLM_REQUEST_TIMEOUT_SECONDS` | `600` | 单次同步 LLM 调用超时；`0` 表示不限制 |
| `USE_DYNAMIC_SUBAGENT` | `true` | 启用动态专家执行路径 |
| `USE_MARKDOWN_UPSERT_TOOL` | `true` | 启用 Markdown 增量章节写入工具 |
| `ORCHESTRATOR_MAX_PARALLEL` | `2` | 最大并行调度任务数 |
| `ORCHESTRATOR_STALE_TIMEOUT_SECONDS` | `180` | 运行态卡住检测阈值 |

兼容说明：后端仍兼容读取旧的 `IT_DESIGN_AGENT_METADATA_KEY`，但新项目建议使用 `REQUIREMENT_ANALYZE_AGENT_METADATA_KEY`。

## 目录结构

```text
requirement-analyze-agent/
|-- api_server/        # FastAPI 后端、LangGraph 节点、工具和服务
|-- admin-ui/          # React/Vite 管理前端
|-- config/            # 阶段定义和专家到阶段的绑定关系
|-- experts/           # BA 专家 YAML profile
|-- skills/            # BA 专家 SKILL、模板、参考资料和脚本
|-- projects/          # 运行时项目、上传材料、产物和本地元数据
|-- .env.example
|-- start-all.bat
|-- start-backend.bat
|-- start-frontend.bat
`-- README.md
```
