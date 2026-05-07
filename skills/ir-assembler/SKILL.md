---
name: ir-assembler
description: 聚合各 BA 专家产物，形成面向 IT/SE 的 IR（IT Requirements）交付包、追踪矩阵、验收标准和待确认问题。
keywords:
  - IR
  - IT Requirements
  - 需求规格
  - 聚合
  - 追踪
  - 验收标准
---

# 工作流 (Workflow)

1. 阅读 RR、竞品参考、人工澄清和所有上游专家产物。
2. 对齐术语、范围、规则、单据、流程、集成和约束，标出冲突和缺口。
3. 聚合形成 IR 主文档，不新增无证据支撑的需求。
4. 建立追踪矩阵，关联 RR、竞品参考、专家产物、IR 条款和验收标准。
5. 输出验收标准和待确认问题清单。
6. 回读产物，确认 IR 能支撑后续 SE 进入 IT 设计。

# Tool Usage Notes

- 先广泛读取上游产物，再写 IR；不要只依赖摘要。
- 对冲突、缺失、低置信度信息必须保留为待确认项。
- `it-requirements.md` 是聚合产物，不替代各专家的细节来源。

# 最终生成策略 (Final Generation)

1. IR 主文档必须覆盖业务目标、范围、角色、术语、规则、单据、流程、集成、非功能约束、假设和待确认项。
2. 追踪矩阵 JSON 字段要稳定，至少包含 source、ir_clause、artifact_refs、coverage_status、confidence。
3. 验收标准必须具体、可验证、可追踪，覆盖正常、异常、权限、边界和集成场景。
4. 待确认问题必须包含问题、责任方、阻塞程度、影响范围和建议处理动作。

