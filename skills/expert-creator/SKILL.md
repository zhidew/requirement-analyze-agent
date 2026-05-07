---
name: expert-creator
description: 系统级专家，用于根据新的领域需求生成完整的 expert profile、SKILL 指南、模板文件和可选脚本。
keywords:
  - 专家生成
  - expert generation
  - 系统专家
  - 模板生成
  - 工具推荐
---

# 工作流 (Workflow)

1. **需求分析**：理解用户想创建的专家能力、领域边界、输出目标和命名偏好。
2. **元数据设计**：生成专业的 `expert_id`、中英文名称、描述、推荐工具和输出清单。
3. **契约生成**：产出 `.expert.yaml`、`SKILL.md`、模板文件以及必要的脚本骨架。
4. **一致性校验**：检查 capability、目录名、frontmatter、输出产物和工具说明是否一致。
5. **注册落盘**：把新专家写入 `experts/` 和 `skills/{expert_id}/` 目录，并在需要时写入 phase 信息。
6. **完成门禁**：仅在核心文件齐备且结构合法后结束。

# 输入参数 (Inputs)

## 必需参数 (Required)

| 参数 | 类型 | 说明 |
|------|------|------|
| `expert_id` | string | 目标 capability 标识，最终会被清洗为专业的 kebab-case。 |
| `name` | string | 专家显示名称，可作为默认英文名。 |
| `description` | string | 专家职责描述，用于生成 profile、skill 和模板约束。 |

## 可选参数 (Optional)

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `name_zh` | string | - | 专家的中文名称。 |
| `name_en` | string | - | 专家的英文名称。 |
| `phase` | string | - | 若提供，则写入专家调度阶段。 |
| `expected_outputs` | array | - | 期望的核心输出文件名列表。 |
| `tool_preferences` | array | - | 建议优先考虑或排除的工具。 |

# 输出产物 (Output Artifacts)

## 必需产物 (Always Required)

| 产物路径 | 说明 |
|----------|------|
| `experts/{expert_id}.expert.yaml` | 新专家的 profile 与运行时契约。 |
| `skills/{expert_id}/SKILL.md` | 新专家的执行指南与标准契约。 |
| `skills/{expert_id}/assets/templates/*` | 至少一个模板文件，用于约束产物结构。 |

## 条件产物 (Conditional)

| 条件 | 产物路径 | 说明 |
|------|----------|------|
| 需要辅助脚本时 | `skills/{expert_id}/scripts/{script_name}` | 领域专用脚本或工具封装。 |
| 指定 `phase` 时 | `config/phases.yaml` | 更新阶段编排，使新专家进入指定 phase。 |

# Tool Usage Notes

## 运行时契约

- 仅使用当前运行时显式暴露的工具或系统已知资源，不假设任意工具一定可用。
- 生成的新专家必须遵循现有系统的 profile / skill / template 目录结构。
- 先设计能力边界与输出契约，再写文件；不要先生成模板后回填专家定义。
- 生成的新 `SKILL.md` 必须采用统一中文契约结构，同时保留必要英文标识符。

## 建议关注的工具

| 工具 | 用途 |
|------|------|
| `read_file_chunk` | 阅读参考 expert、模板和工具注册表。 |
| `list_files` | 盘点现有 experts、skills 和模板目录。 |
| `write_file` | 写入 profile、SKILL、模板和脚本文件。 |
| `patch_file` | 修补已生成文件中的命名或结构问题。 |
| `grep_search` | 搜索参考模式、常见契约或冲突命名。 |

# 参考资料 (References)

- 模板参考 `assets/templates/expert_profile_template.yaml.j2` 和 `assets/templates/skill_template.md.j2`。
- 工具注册表参考 `assets/TOOL_REGISTRY.yaml`。
- 现有内置专家应作为样板，尤其是中文化和结构契约已统一的专家。

# 注意事项 (Notes)

- **命名规范**：`expert_id` 必须是专业英文 kebab-case，不允许中文 capability。
- **中文优先**：自然语言说明默认使用中文，但工具名、文件名、字段名保留英文标识。
- **专家边界**：只负责生成专家定义，不直接替代新专家完成其业务设计。
- **一致性优先**：profile、skill、模板、脚本中的 capability、输出文件名和边界说明必须一致。

## LLM Instructions

创建新专家时，严格按照下面的顺序生成内容：

### Step 1: Parse User Intent

解析用户提供的信息，至少提取以下要素：

- 专家名称：优先同时识别 `name_zh` 和 `name_en`
- 领域描述：明确专家负责什么，不负责什么
- 期望输出：识别核心产物、可能的模板和是否需要辅助脚本
- 调度信息：若提供 phase，需写入 scheduling.phase

### Step 2: Generate Expert Metadata

先生成一个 JSON 元数据对象，字段至少包括：

```json
{
  "expert_id": "professional-kebab-case-id",
  "name_zh": "中文名称",
  "name_en": "English Name",
  "description": "中文职责描述",
  "tools_allowed": ["write_file", "read_file_chunk"],
  "needed_script_tool": null,
  "core_template_name": "output_template.md.j2"
}
```

元数据规则：

- `expert_id` 必须是专业英文 kebab-case，例如 `security-design`、`billing-flow`
- 默认保留中文说明，但 capability、文件名和工具名保持英文标识
- 工具推荐要基于领域职责，默认至少考虑读写和结构检查能力
- 若专家需要专用脚本，`needed_script_tool` 必须给出明确文件名

### Step 3: Generate Expert Profile

生成 `.expert.yaml` 时至少包含以下内容：

- `name`、`name_en`、`name_zh`、`capability`、`description`
- `skills`
- `scheduling.priority`、`scheduling.dependencies`，以及在需要时的 `scheduling.phase`
- `tools.allowed`
- `outputs.expected`
- `metadata.boundary_contract`
- `metadata.topic_ownership`
- `metadata.routing.keywords`
- `metadata.prompt_hints`
- `metadata.delivery_contract`
- `policies` 与 `error_handling`

要求：

- 边界说明必须明确 owns、excludes、upstream_inputs
- `expert.yaml` 是结构化真相源；`outputs.expected`、`upstream_artifacts`、`routing.keywords`、`topic_ownership`、`prompt_hints`、`delivery_contract` 只能在这里维护 canonical 版本
- 产物清单必须和模板、SKILL 指南保持一致
- 描述优先中文，避免空泛措辞

### Step 4: Generate Skill Guide

生成的 `SKILL.md` 必须采用统一契约结构：

1. Frontmatter：`name`、`description`、`keywords`
2. `# 工作流 (Workflow)`
3. `# 输入参数 (Inputs)`
4. `# 输出产物 (Output Artifacts)`
5. `# Tool Usage Notes`
6. `# 参考资料 (References)`
7. `# 注意事项 (Notes)`
8. `# ReAct 执行策略 (ReAct Strategy)`
9. `## ReAct 规则`
10. `## 返回格式`
11. `# 最终生成策略 (Final Generation)`
12. `## 生成要求`
13. `## 生成内容`

额外规则：

- 自然语言说明默认用中文
- 保留必要的英文工具名、文件名、协议名和格式名
- 输出产物、证据文件和边界说明必须与 profile 一致
- `SKILL.md` 不要重复维护 `outputs.expected` 或 `upstream_artifacts` 的 canonical 文件名清单；应改写为“使用 runtime 注入的上游工件 / 目标产物”
- `SKILL.md` 不要再维护与 YAML 等价的 boundary、routing keywords、delivery checklist；这里只保留方法论、执行顺序、写作风格、ReAct 策略和示例
- 如果必须展示路径示例，必须明确标注为 illustrative example，而不是专家契约真相源

### Step 5: Generate Templates

为新专家生成至少一个模板文件，并确保：

- 模板文件名与 `outputs.expected` 对应
- 模板只提供结构约束，不替代专家最终推理
- 模板内容要体现该专家的边界和期望章节

### Step 6: Generate Optional Scripts

仅当该专家确实需要辅助脚本时才生成脚本，且脚本必须：

- 放在 `skills/{expert_id}/scripts/`
- 具有清晰的用途说明和最小可运行骨架
- 不与现有运行时工具职责重复

## Tool Registry Reference

系统内可参考的工具类别包括：

- **文件读写**：`read_file_chunk`、`list_files`、`write_file`、`patch_file`
- **结构与检索**：`grep_search`、`extract_structure`、`extract_lookup_values`
- **数据与知识**：`query_database`、`query_knowledge_base`
- **仓库与执行**：`clone_repository`、`run_command`

工具推荐原则：

- 根据专家边界推荐最小必要工具集
- 优先推荐读取和结构化分析工具，避免过度授权
- 只有在专家职责确实需要时才增加脚本或命令执行能力

# ReAct 执行策略 (ReAct Strategy)

1. **研究 (Research)**：阅读参考 expert、模板和工具注册表，理解目标领域与现有范式。
2. **设计 (Design)**：先确定 capability、边界、输出物和工具契约。
3. **编写 (Write)**：依次写入 profile、SKILL、模板以及可选脚本。
4. **校验 (Verify)**：回读所有生成文件，检查命名、路径、输出清单和章节结构是否一致。
5. **修补 (Patch)**：仅修补局部命名、边界或模板问题，不重做整套结构。
6. **完成 (Finalize)**：确认新专家可被系统识别且契约完整后结束。

## ReAct 规则

1. 默认每次只输出一个下一步动作；只有在收集独立、低风险的读取证据时，才可用 `actions` 并行返回最多 2 个只读动作。
2. 仅当 `.expert.yaml`、`SKILL.md`、至少一个模板文件以及必要时的脚本都完成后才允许 `done=true`。
3. `tool_input` 必须是明确的 JSON，尤其要给出目标路径、capability 或模板名。
4. 每一步都要写清 `evidence_note`，说明本步在确认什么契约或生成什么文件。
5. 不能生成与 capability 不匹配、边界不明确或输出清单不闭环的新专家。

## 返回格式

```json
{
  "done": false,
  "thought": "为什么需要这一步",
  "tool_name": "当前要调用的工具名，若无需工具则为 none",
  "tool_input": {},
  "actions": [
    {
      "tool_name": "可并行的只读工具",
      "tool_input": {
        "path": "skills/expert-creator/assets/TOOL_REGISTRY.yaml",
        "start_line": 1,
        "end_line": 120
      }
    }
  ],
  "evidence_note": "这一步应该确认或产出什么"
}
```

# 最终生成策略 (Final Generation)

## 生成要求

1. 新专家必须具备清晰 capability、边界契约、输出清单和工具契约。
2. 新生成的 `SKILL.md` 必须采用统一中文标准契约结构。
3. profile、skill、模板和脚本要互相引用一致，不得出现命名漂移。
4. 如提供 phase，需确保阶段信息和专家能力定位一致。

## 生成内容

- **`experts/{expert_id}.expert.yaml`**：定义专家身份、调度、边界和输出契约。
- **`skills/{expert_id}/SKILL.md`**：定义执行范式、输入输出、ReAct 规则和最终生成要求。
- **`skills/{expert_id}/assets/templates/*`**：约束目标产物的结构和表达方式。
- **`skills/{expert_id}/scripts/*`**：仅在必要时生成辅助脚本。
