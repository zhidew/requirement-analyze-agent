---
name: requirement-clarification
description: 面向 BA 梳理 RR、竞品参考和人工补充信息，形成需求澄清基线、范围假设和术语表。
keywords:
  - 需求澄清
  - RR
  - Raw Requirements
  - 范围
  - 术语
  - 假设
---

# 工作流 (Workflow)

1. 阅读 RR、上传材料和竞品参考，区分事实、假设、建议和待确认信息。
2. 提炼业务目标、用户角色、核心场景、范围边界和约束。
3. 建立术语表，统一业务命名，标注歧义和别名。
4. 输出澄清基线、范围假设和术语表。
5. 回读产物，确认未把推测写成事实。

# 输出产物 (Output Artifacts)

- 目标输出文件名和写入顺序以 runtime 注入的 expected outputs 与 output plan 为准。
- 本技能重点约束内容质量：事实来源、范围边界、待确认项和术语一致性。

# Tool Usage Notes

- 优先使用 `list_files`、`read_file_chunk`、`grep_search` 收集 RR 和竞品参考证据。
- 使用 `write_file` 或 `patch_file` 写入本专家负责的 `artifacts/` 与 `evidence/` 内容。
- 对低置信度结论必须标注“假设”或“待确认”。

# 最终生成策略 (Final Generation)

1. 澄清基线必须覆盖业务目标、角色、场景、范围、约束、竞品参考摘要和待确认事项。
2. 范围与假设必须明确 in-scope、out-of-scope、约束、风险和影响。
3. 术语表必须包含术语、定义、别名、来源、歧义和推荐统一命名。
4. 所有自然语言默认使用简体中文。

