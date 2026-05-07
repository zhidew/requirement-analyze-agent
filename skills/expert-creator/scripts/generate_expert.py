"""
Expert Generation Script

This script provides the core logic for intelligently generating new design experts.
It reads instructions from SKILL.md and executes the generation workflow.

Usage:
    from skills.expert_creator.scripts.generate_expert import ExpertGenerator
    
    generator = ExpertGenerator(base_dir)
    expert = generator.create_expert("api-design", "API Design Expert", "Design APIs...")
"""

import json
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


class ToolRegistry:
    """System tool registry manager."""
    
    def __init__(self, registry_path: Path):
        self.registry_path = registry_path
        self._data = self._load_registry()
    
    def _load_registry(self) -> Dict[str, Any]:
        """Load tool registry from YAML file."""
        if not self.registry_path.exists():
            return {"tools": [], "categories": [], "tool_combinations": []}
        
        with open(self.registry_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {"tools": [], "categories": [], "tool_combinations": []}
    
    def get_all_tools(self) -> List[Dict[str, Any]]:
        """Get all registered tools."""
        return self._data.get("tools", [])
    
    def recommend_tools_for_domain(self, domain_keywords: List[str]) -> List[str]:
        """Recommend tools based on domain keywords."""
        recommended = set()
        
        # Keyword to tool mapping
        keyword_tool_map = {
            "database": ["query_database"],
            "db": ["query_database"],
            "sql": ["query_database"],
            "data": ["query_database", "extract_lookup_values"],
            "api": ["query_database", "query_knowledge_base", "write_file"],
            "code": ["clone_repository", "grep_search", "read_file_chunk"],
            "repo": ["clone_repository"],
            "git": ["clone_repository"],
            "structure": ["extract_structure", "list_files"],
            "knowledge": ["query_knowledge_base"],
            "business": ["query_knowledge_base"],
            "config": ["read_file_chunk", "write_file", "patch_file"],
            "security": ["grep_search", "query_knowledge_base"],
            "test": ["run_command", "read_file_chunk"],
            "ops": ["run_command", "read_file_chunk"],
            "architecture": ["clone_repository", "extract_structure", "grep_search"],
            "integration": ["clone_repository", "query_database", "query_knowledge_base"],
            "flow": ["query_knowledge_base", "write_file"],
        }
        
        # Always include basic file tools
        recommended.add("write_file")
        recommended.add("read_file_chunk")
        
        # Map keywords to tools
        for keyword in domain_keywords:
            keyword_lower = keyword.lower()
            for key, tools in keyword_tool_map.items():
                if key in keyword_lower:
                    recommended.update(tools)
        
        return list(recommended)


# Reference expert pairs used as few-shot examples for LLM generation
_REFERENCE_EXPERT_PAIRS = [
    ("api-design", "API设计"),
    ("data-design", "数据设计"),
]


class ExpertGenerator:
    """Intelligent expert generation engine."""
    
    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self.experts_dir = self.base_dir / "experts"
        self.skills_dir = self.base_dir / "skills"
        
        # Load tool registry
        registry_path = self.base_dir / "skills" / "expert-creator" / "assets" / "TOOL_REGISTRY.yaml"
        self.tool_registry = ToolRegistry(registry_path)
        
        # Load skill instructions
        self.skill_path = self.base_dir / "skills" / "expert-creator" / "SKILL.md"
        
        # Load Jinja2 templates for structure reference
        self._profile_template_path = self.base_dir / "skills" / "expert-creator" / "assets" / "templates" / "expert_profile_template.yaml.j2"
        self._skill_template_path = self.base_dir / "skills" / "expert-creator" / "assets" / "templates" / "skill_template.md.j2"
    
    def _resolve_expert_profile_path(self, expert_id: str) -> Path:
        """Resolve the path for an expert profile file."""
        return self.experts_dir / f"{expert_id}.expert.yaml"
    
    def _clean_expert_id(self, raw_id: str) -> str:
        """Clean and normalize an expert ID to Kebab-case."""
        cleaned = "".join(ch for ch in raw_id.lower() if ch.isalnum() or ch == "-")
        return cleaned.strip("-") or f"expert-{uuid.uuid4().hex[:8]}"
    
    def _analyze_domain_keywords(self, name: str, description: str) -> List[str]:
        """Extract domain keywords from name and description."""
        text = f"{name} {description}".lower()
        
        # Common domain keywords
        domain_keywords = [
            "api", "data", "database", "db", "sql", "security", "test", "ops",
            "architecture", "integration", "flow", "config", "code", "repo",
            "git", "structure", "knowledge", "business"
        ]
        
        found = []
        for keyword in domain_keywords:
            if keyword in text:
                found.append(keyword)
        
        return found
    
    def _collect_reference_samples(self) -> str:
        """Collect reference YAML and SKILL.md from built-in experts as few-shot examples."""
        sections = []
        
        for expert_id, label in _REFERENCE_EXPERT_PAIRS:
            yaml_path = self.experts_dir / f"{expert_id}.expert.yaml"
            skill_path = self.skills_dir / expert_id / "SKILL.md"
            
            yaml_content = yaml_path.read_text(encoding="utf-8") if yaml_path.exists() else ""
            skill_content = skill_path.read_text(encoding="utf-8") if skill_path.exists() else ""
            
            if yaml_content or skill_content:
                section = f"### Reference Expert: {label} ({expert_id})\n\n"
                if yaml_content:
                    section += f"**profile.yaml**:\n```yaml\n{yaml_content.strip()}\n```\n\n"
                if skill_content:
                    section += f"**skill.md**:\n```markdown\n{skill_content.strip()}\n```\n\n"
                sections.append(section)
        
        if not sections:
            return ""
        
        return "## Reference Expert Examples (follow this level of detail and structure)\n\n" + "\n---\n\n".join(sections)
    
    def _load_template_reference(self) -> str:
        """Load Jinja2 template files as structure reference."""
        parts = []
        
        for label, path in [
            ("Profile Template", self._profile_template_path),
            ("Skill Template", self._skill_template_path),
        ]:
            if path.exists():
                content = path.read_text(encoding="utf-8").strip()
                parts.append(f"**{label}** ({path.name}):\n```jinja2\n{content}\n```")
        
        if not parts:
            return ""
        
        return "## Template Structure Reference (Jinja2 templates)\n\n" + "\n\n".join(parts)
    
    def _validate_and_enrich_profile(self, profile_yaml: str, expert_id: str, tools_allowed: list) -> str:
        """Validate and enrich the generated expert profile YAML with missing important fields."""
        try:
            profile = yaml.safe_load(profile_yaml) or {}
        except Exception:
            return profile_yaml
        
        if not isinstance(profile, dict):
            return profile_yaml
        
        enriched = False
        
        # Ensure metadata.boundary_contract exists
        if "metadata" not in profile or not isinstance(profile.get("metadata"), dict):
            profile["metadata"] = {}
            enriched = True
        
        if "boundary_contract" not in profile["metadata"]:
            profile["metadata"]["boundary_contract"] = {
                "owns": [f"{expert_id} domain design artifacts"],
                "excludes": [
                    "full architecture narrative",
                    "ops runbooks and test inventory",
                ],
                "upstream_inputs": profile.get("scheduling", {}).get("dependencies", []),
            }
            enriched = True
        
        # Ensure error_handling exists
        if "error_handling" not in profile:
            profile["error_handling"] = {
                "on_missing_required_input": "fail",
                "on_validation_failure": "fail",
                "on_partial_generation": "emit_evidence_and_fail",
            }
            enriched = True
        
        # Ensure policies has required fields
        policies = profile.get("policies", {})
        if not isinstance(policies, dict):
            policies = {}
        for key in ("asset_baseline_required", "evidence_required", "output_must_be_structured", "manual_override_forbidden", "descriptions_prefer_chinese"):
            if key not in policies:
                policies[key] = True
                enriched = True
        profile["policies"] = policies
        
        # Ensure scheduling has dependencies
        if "scheduling" not in profile:
            profile["scheduling"] = {}
        sched = profile["scheduling"]
        if "dependencies" not in sched or not sched.get("dependencies"):
            sched["dependencies"] = []
            enriched = True

        # Ensure inputs.required follows the standard runtime contract
        inputs = profile.get("inputs", {})
        if not isinstance(inputs, dict):
            inputs = {}
        if not inputs.get("required"):
            inputs["required"] = ["requirements", "existing_assets", "output_root"]
            enriched = True
        if "optional" not in inputs:
            inputs["optional"] = ["constraints", "context"]
            enriched = True
        profile["inputs"] = inputs
        
        # Ensure upstream_artifacts if dependencies exist
        deps = sched.get("dependencies", [])
        if deps and "upstream_artifacts" not in profile:
            profile["upstream_artifacts"] = {}
            for dep in deps:
                profile["upstream_artifacts"][dep] = ["-- to be filled based on actual dependency outputs --"]
            enriched = True
        
        # Ensure tools.allowed
        if "tools" not in profile:
            profile["tools"] = {}
        if not profile["tools"].get("allowed"):
            profile["tools"]["allowed"] = tools_allowed
            enriched = True

        # Ensure outputs include both expected artifacts and execution evidence
        outputs = profile.get("outputs", {})
        if not isinstance(outputs, dict):
            outputs = {}
        if not outputs.get("expected"):
            outputs["expected"] = [f"{expert_id}-design.md"]
            enriched = True
        if not outputs.get("evidence"):
            outputs["evidence"] = [f"{expert_id}.json"]
            enriched = True
        profile["outputs"] = outputs
        
        if enriched:
            return yaml.safe_dump(profile, allow_unicode=True, sort_keys=False)
        return profile_yaml
    
    def _validate_skill_quality(self, skill_content: str, expert_id: str) -> str:
        """Validate and enrich the generated SKILL.md if it lacks important sections."""
        issues = []
        
        required_sections = [
            ("ReAct 执行策略", "ReAct Strategy"),
            ("ReAct 规则", "ReAct Guardrails"),
            ("最终生成策略", "Final Generation"),
            ("注意事项", "Notes"),
        ]
        
        content_lower = skill_content.lower()
        for zh_label, en_label in required_sections:
            if zh_label.lower() not in content_lower and en_label.lower() not in content_lower:
                issues.append(zh_label)
        
        # Check for at least 3 phases in workflow
        phase_count = skill_content.lower().count("phase")
        if phase_count < 3:
            issues.append(f"insufficient phases (found {phase_count}, need >= 3)")
        
        if issues:
            print(f"[ExpertGenerator] SKILL.md quality issues: {issues}")
            # Append missing sections with hints
            hint = "\n\n<!-- AUTO-GENERATED QUALITY HINTS -->\n"
            hint += "<!-- The following sections are recommended but were missing from the LLM output. "
            hint += "Consider enriching this skill guide manually. -->\n"
            for issue in issues:
                hint += f"<!-- - Missing: {issue} -->\n"
            skill_content += hint
        
        return skill_content
    
    def _generate_with_llm(
        self,
        name: str,
        description: str,
        *,
        name_zh: str = "",
        name_en: str = "",
        request_id: str = "",
    ) -> Dict[str, Any]:
        """Use LLM to generate expert content based on SKILL.md instructions."""
        request_tag = request_id or uuid.uuid4().hex[:8]
        try:
            from api_server.services.llm_service import generate_with_llm
            
            # Read skill instructions
            skill_instructions = self.skill_path.read_text(encoding="utf-8") if self.skill_path.exists() else ""
            
            # Extract the "LLM Instructions" section from SKILL.md
            llm_section_start = skill_instructions.find("## LLM Instructions")
            llm_section_end = skill_instructions.find("## Tool Registry Reference")
            if llm_section_start > 0 and llm_section_end > llm_section_start:
                llm_instructions = skill_instructions[llm_section_start:llm_section_end]
            else:
                llm_instructions = skill_instructions
            
            # Collect reference samples and template references
            reference_samples = self._collect_reference_samples()
            template_reference = self._load_template_reference()
            
            # Get tool recommendations
            domain_keywords = self._analyze_domain_keywords(name, description)
            recommended_tools = self.tool_registry.recommend_tools_for_domain(domain_keywords)
            
            # Build name info for prompt
            name_info = name
            if name_zh and name_en:
                name_info = f"{name_zh}（{name_en}）"
            
            # Build few-shot context
            few_shot_context = ""
            if reference_samples:
                few_shot_context += f"\n{reference_samples}\n"
            if template_reference:
                few_shot_context += f"\n{template_reference}\n"
            
            # Generate metadata
            metadata_prompt = f"""
{llm_instructions}

{few_shot_context}

---

Now generate expert metadata for:

**Expert Name**: {name_info}
**Description**: {description}

**Domain Keywords**: {domain_keywords}
**Recommended Tools**: {recommended_tools}

Generate a JSON object following Step 2 instructions. Return ONLY the JSON, no explanations.
"""
            
            print(f"[ExpertCreate:{request_tag}] Requesting metadata draft from LLM for '{name}'.")
            metadata_result = generate_with_llm(
                metadata_prompt,
                f"Generate metadata for expert: {name}",
                ["meta.json"]
            )
            
            meta = json.loads(metadata_result.artifacts.get("meta.json", "{}"))
            expert_id = self._clean_expert_id(meta.get("expert_id", name))
            
            # Override tools if not provided
            if not meta.get("tools_allowed"):
                meta["tools_allowed"] = recommended_tools
            
            # Generate full content
            template_name = meta.get("core_template_name", "output_template.md.j2")
            script_name = meta.get("needed_script_tool")
            tools_allowed = meta.get("tools_allowed", recommended_tools)
            
            expected_files = ["profile.yaml", "skill.md", template_name]
            if script_name:
                expected_files.append(script_name)
            
            content_prompt = f"""
{llm_instructions}

{few_shot_context}

---

Now generate complete expert files for:

**Expert ID**: {expert_id}
**Name (Chinese)**: {meta.get('name_zh', name)}
**Name (English)**: {meta.get('name_en', expert_id)}
**Description**: {meta.get('description', description)}
**Tools**: {json.dumps(tools_allowed)}

## IMPORTANT REQUIREMENTS

Study the **Reference Expert Examples** above carefully and follow the SAME level of detail and structural completeness:

1. **profile.yaml** must include ALL of these fields (not just the basic ones):
   - `name`, `name_en`, `name_zh`, `capability`, `description`, `version`, `skills`
   - `inputs.required` (usually `requirements`, `existing_assets`, `output_root`)
   - `scheduling.priority`, `scheduling.dependencies` (list relevant expert IDs or [])
   - `upstream_artifacts` (map of dependency expert IDs to their expected output files)
   - `keywords` (list of domain keywords)
   - `tools.allowed` (list of allowed tools)
   - `outputs.expected` (list of specific output file names under `artifacts/`)
   - `outputs.evidence` (list of evidence file names under `evidence/`)
   - `metadata.boundary_contract` with `owns`, `excludes`, `upstream_inputs`
   - `policies` with `asset_baseline_required`, `evidence_required`, `output_must_be_structured`, `manual_override_forbidden`, `descriptions_prefer_chinese`
   - `error_handling` with `on_missing_required_input`, `on_validation_failure`, `on_partial_generation`

2. **skill.md** must be comprehensive (follow the reference examples' style):
   - Frontmatter with name, description, keywords
   - Workflow section with numbered steps in Chinese
   - Input parameters table (required + optional)
   - Output artifacts table (required + conditional)
   - Tool usage notes table
   - References and Notes sections with domain-specific guidance
   - **ReAct 执行策略** with Research→Write→Verify→Patch→Finalize cycle
   - **ReAct 规则** with batching rules and tool constraints
   - Return format JSON example
   - **最终生成策略** with generation requirements and content list

3. **{template_name}** must be a meaningful Jinja2 template with actual structure,
   placeholders, and domain-specific sections (NOT just a placeholder comment).

Generate the following files following Step 3-6 instructions:
1. profile.yaml - Expert configuration
2. skill.md - Skill guide with ReAct workflow
3. {template_name} - Output template
{"4. " + script_name + " - Python script" if script_name else ""}

Return each file in a code block with the filename as header.
"""
            
            print(f"[ExpertCreate:{request_tag}] Requesting expert asset bundle from LLM for '{expert_id}'.")
            content_result = generate_with_llm(
                content_prompt,
                f"Generate complete expert files for: {name}",
                expected_files
            )
            
            profile_raw = self._clean_yaml(content_result.artifacts.get("profile.yaml", ""))
            skill_raw = content_result.artifacts.get("skill.md", "")
            template_raw = content_result.artifacts.get(template_name, "")
            script_raw = content_result.artifacts.get(script_name, "") if script_name else ""
            
            # Post-process: validate and enrich profile
            profile_enriched = self._validate_and_enrich_profile(profile_raw, expert_id, tools_allowed)
            
            # Post-process: validate skill quality
            skill_validated = self._validate_skill_quality(skill_raw, expert_id)
            
            return {
                "success": True,
                "meta": meta,
                "expert_id": expert_id,
                "profile": profile_enriched,
                "skill": skill_validated,
                "template": template_raw,
                "template_name": template_name,
                "script": script_raw,
                "script_name": script_name,
                "tools_recommended": tools_allowed,
            }
            
        except Exception as e:
            print(f"[ExpertCreate:{request_tag}] LLM generation failed: {e}")
            return {"success": False, "error": str(e)}
    
    def _clean_yaml(self, raw: str) -> str:
        """Clean YAML content from markdown code blocks."""
        cleaned = raw.strip()
        import re
        if cleaned.startswith("```"):
            match = re.match(r"^```(?:yaml)?\s+([\s\S]*?)\s*```$", cleaned)
            if match:
                cleaned = match.group(1).strip()
            else:
                match = re.match(r"^```(?:yaml)?\s+([\s\S]*)", cleaned)
                if match:
                    cleaned = match.group(1).strip()
                    if cleaned.endswith("```"):
                        cleaned = cleaned[:-3].strip()

        try:
            if "capability:" in cleaned:
                yaml.safe_load(cleaned)
                return cleaned
        except Exception:
            pass
        return ""

    def _ensure_profile_names(
        self,
        raw_profile: str,
        expert_id: str,
        *,
        fallback_name: str,
        name_zh: str = "",
        name_en: str = "",
    ) -> str:
        """Ensure generated expert YAML persists bilingual display names."""
        normalized_en = (name_en or fallback_name or expert_id).strip() or expert_id
        normalized_zh = (name_zh or "").strip()

        try:
            profile = yaml.safe_load(raw_profile) or {}
            if not isinstance(profile, dict):
                profile = {}
        except Exception:
            profile = {}

        profile["name"] = str(profile.get("name") or normalized_en)
        profile["name_en"] = str(profile.get("name_en") or normalized_en)
        profile["name_zh"] = str(profile.get("name_zh") or normalized_zh)
        profile["capability"] = str(profile.get("capability") or expert_id)

        return yaml.safe_dump(profile, allow_unicode=True, sort_keys=False)

    @staticmethod
    def _inject_phase(profile_yaml: str, phase: str) -> str:
        """Inject or update scheduling.phase in an expert profile YAML."""
        try:
            profile = yaml.safe_load(profile_yaml) or {}
        except Exception:
            profile = {}

        scheduling = profile.get("scheduling")
        if not isinstance(scheduling, dict):
            scheduling = {}
            profile["scheduling"] = scheduling
        scheduling["phase"] = str(phase).strip().upper()

        return yaml.safe_dump(profile, allow_unicode=True, sort_keys=False)

    def _generate_fallback_content(self, expert_id: str, name: str, description: str, *, name_zh: str = "", name_en: str = "") -> Dict[str, Any]:
        """Generate fallback content when LLM fails."""
        
        # Analyze domain and recommend tools
        domain_keywords = self._analyze_domain_keywords(name, description)
        recommended_tools = self.tool_registry.recommend_tools_for_domain(domain_keywords)
        
        # Use English name for YAML name field, fallback to provided name
        yaml_name = name_en or name
        skill_name = name_zh or yaml_name
        
        profile = f"""name: {yaml_name}
name_en: {name_en or yaml_name}
name_zh: {name_zh}
capability: {expert_id}
description: "{description}"
version: 0.1.0
skills:
  - {expert_id}
inputs:
  required:
    - requirements
    - existing_assets
    - output_root
  optional:
    - constraints
    - context
scheduling:
  priority: 50
  dependencies: []
keywords: {json.dumps(domain_keywords)}
tools:
  allowed: {json.dumps(recommended_tools)}
outputs:
  expected: ["{expert_id}-design.md"]
  evidence: ["{expert_id}.json"]
metadata:
  boundary_contract:
    owns:
      - {expert_id} domain design artifacts
    excludes:
      - full architecture narrative
      - ops runbooks and test inventory
    upstream_inputs: []
policies:
  asset_baseline_required: true
  evidence_required: true
  output_must_be_structured: true
  manual_override_forbidden: true
  descriptions_prefer_chinese: true
error_handling:
  on_missing_required_input: fail
  on_validation_failure: fail
  on_partial_generation: emit_evidence_and_fail
"""
        skill = f"""---
name: {skill_name}
description: "{description}"
keywords: {json.dumps(domain_keywords)}
---

# {skill_name}

## 工作流 (Workflow)

1. **需求分析**：读取需求基线，识别业务场景和设计需求。
2. **上下文收集**：使用读取工具从现有资产和参考文档中收集必要信息。
3. **枚举与约束提取**：提取相关的枚举值、约束条件和规范要求。
4. **设计生成**：基于收集到的证据生成领域设计产物。
5. **验证与修正**：回读已写入的内容，检查完整性和一致性，必要时修补。
6. **证据沉淀**：将设计依据写入 evidence 文件。

## 输入参数 (Inputs)

## 必需参数 (Required)

| 参数 | 类型 | 说明 |
|------|------|------|
| `requirements` | string/path | 需求文件路径 |
| `output_root` | string/path | 项目设计包的根路径 |

## 可选参数 (Optional)

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `constraints` | string/path | - | 约束策略文件路径 |
| `context` | string/path | - | 上下文信息文件路径 |

## 输出产物 (Output Artifacts)

## 必需产物 (Always Required)

| 产物路径 | 说明 |
|----------|------|
| `artifacts/{expert_id}-design.md` | 主要设计文档 |
| `evidence/{expert_id}.json` | 设计依据和决策证据 |

# Tool Usage Notes

| 工具名称 | 说明 |
|----------|------|
| `list_files` | 列出目录下的文件 |
| `read_file_chunk` | 读取文件片段 |
| `grep_search` | 搜索文件内容 |
| `extract_structure` | 提取文件结构 |
| `write_file` | 写入设计产物 |
| `patch_file` | 修补已有文件 |

# 参考资料 (References)

- 模板使用 `assets/templates/output_template.md.j2`。
- 参考项目全局规范（如有）。

# 注意事项 (Notes)

- **专家边界**：只负责本领域的核心设计，不涉及整体架构叙事、运维方案或测试计划。
- **依赖协同**：如有上游依赖，优先引用上游产物中的边界和命名结果。
- **描述语言**：产出文档中的描述性文本优先使用中文。

# ReAct 执行策略 (ReAct Strategy)

在执行过程中，按以下策略循环操作：

1. **研究 (Research)**：使用读取工具（list_files, read_file_chunk, grep_search）从需求文件中收集证据。
2. **编写 (Write)**：使用 `write_file` 生成草稿产物。
3. **验证 (Verify)**：使用 `read_file_chunk` 回读已写入的内容进行验证。
4. **修补 (Patch)**：基于验证结果或新发现，使用 `patch_file` 进行微调。
5. **完成 (Finalize)**：仅当所有预期产物正确写入并验证后，设置 done=true。

## ReAct 规则

1. 默认每次只输出一个下一步动作；只有在收集独立、低风险的读取证据时，才可使用 `actions` 返回最多 2 个只读动作。
2. 仅当收集到足够证据且已写入所有预期文件时才停止。
3. 保持 tool_input 简洁且为机器可读的 JSON 格式。
4. 每个步骤记录 evidence_note 说明该步骤的目的。
5. `actions` 只可包含 `read_file_chunk`、`extract_structure`、`grep_search`、`extract_lookup_values` 等只读工具，且不得混入 `write_file`、`patch_file`、`run_command`、`clone_repository`、`query_database` 或 `query_knowledge_base`。

## 返回格式

```json
{{
  "done": false,
  "thought": "Why this step is needed.",
  "tool_name": "grep_search | read_file_chunk | write_file | patch_file | none",
  "tool_input": {{}},
  "actions": [
    {{ "tool_name": "read_file_chunk", "tool_input": {{ "path": "baseline/original-requirements.md", "start_line": 1, "end_line": 120 }} }}
  ],
  "evidence_note": "What this step should confirm or produce."
}}
```

# 最终生成策略 (Final Generation)

当 ReAct 循环结束后，基于收集的证据生成最终产物：

## 生成要求

1. 仅反映观察结果支持的设计内容。
2. 使用 snake_case 命名。
3. 包含足够的结构供 assembler 和 validator 消费。
4. 将模板作为风格参考，而非强制内容。

## 生成内容

- **{expert_id}-design.md**: {description}
"""
        template = f"""# {yaml_name} Output Template

## 概述 (Overview)

{{% set generated_at = now() %}}

> Generated at: {{{{ generated_at }}}}

## 设计内容 (Design Content)

{{{{
# 此区域由专家根据 ReAct 工作流收集的证据自动填充
# 具体结构请根据实际领域需求调整以下章节
}}}}

## 需求摘要 (Requirements Summary)

{{{{
# 从需求基线中提取的关键需求
}}}}

## 设计决策 (Design Decisions)

| 决策点 | 选择方案 | 依据 |
|--------|----------|------|
| - | - | - |

## 详细设计 (Detailed Design)

### 1. 结构设计

{{{{
# 领域特定的结构设计内容
}}}}

### 2. 接口/数据定义

{{{{
# 相关的接口或数据定义
}}}}

### 3. 约束与验证

{{{{
# 业务规则和验证约束
}}}}

## 变更记录 (Change Log)

| 版本 | 变更说明 | 日期 |
|------|----------|------|
| 0.1.0 | 初始设计 | {{{{ generated_at }}}} |
"""
        return {
            "success": True,
            "expert_id": expert_id,
            "profile": profile,
            "skill": skill,
            "template": template,
            "template_name": "output_template.md.j2",
            "script": "",
            "script_name": None,
            "tools_recommended": recommended_tools,
        }
    
    def create_expert(
        self,
        expert_id: str,
        name: str,
        description: str = "",
        use_llm: bool = True,
        *,
        name_zh: str = "",
        name_en: str = "",
        phase: str = "",
        request_id: str = "",
    ) -> Optional[Dict[str, Any]]:
        """
        Create a new expert with intelligent generation.

        Args:
            expert_id: Initial expert ID (will be cleaned to Kebab-case)
            name: Expert display name
            description: Expert description
            use_llm: Whether to use LLM for intelligent generation
            name_zh: Chinese name for the expert
            name_en: English name for the expert
            phase: Target execution phase (e.g. "ARCHITECTURE")

        Returns:
            Expert metadata dict if successful, None otherwise
        """
        # Clean initial ID
        request_tag = request_id or uuid.uuid4().hex[:8]
        initial_id = self._clean_expert_id(expert_id)
        print(
            f"[ExpertCreate:{request_tag}] ExpertGenerator started "
            f"initial_id='{initial_id}' use_llm={'true' if use_llm else 'false'}."
        )
        
        # Generate content
        if use_llm:
            result = self._generate_with_llm(
                name,
                description,
                name_zh=name_zh,
                name_en=name_en,
                request_id=request_tag,
            )
        else:
            result = {"success": False}
        
        if not result.get("success"):
            print(f"[ExpertCreate:{request_tag}] Falling back to deterministic content generation.")
            result = self._generate_fallback_content(initial_id, name, description, name_zh=name_zh, name_en=name_en)
        
        expert_id = result.get("expert_id", initial_id)
        
        # Ensure unique ID
        profile_path = self._resolve_expert_profile_path(expert_id)
        if profile_path.exists():
            expert_id = f"{expert_id}-{uuid.uuid4().hex[:4]}"
            profile_path = self._resolve_expert_profile_path(expert_id)
            print(f"[ExpertCreate:{request_tag}] Expert id already existed. Using unique id '{expert_id}'.")
        
        # Create directory structure
        self.experts_dir.mkdir(parents=True, exist_ok=True)
        skill_dir = self.skills_dir / expert_id
        (skill_dir / "assets" / "templates").mkdir(parents=True, exist_ok=True)
        (skill_dir / "references").mkdir(parents=True, exist_ok=True)
        (skill_dir / "scripts").mkdir(parents=True, exist_ok=True)
        
        # Write files
        profile_content = result.get("profile", "")
        skill_content = result.get("skill", "")
        template_content = result.get("template", "")
        template_name = result.get("template_name", "output_template.md.j2")
        script_content = result.get("script", "")
        script_name = result.get("script_name")

        if profile_content:
            profile_content = self._ensure_profile_names(
                profile_content,
                expert_id,
                fallback_name=name,
                name_zh=name_zh,
                name_en=name_en,
            )
            # Inject scheduling.phase if provided
            if phase:
                profile_content = self._inject_phase(profile_content, phase)

        if profile_content:
            profile_path.write_text(profile_content, encoding="utf-8")
        if skill_content:
            (skill_dir / "SKILL.md").write_text(skill_content, encoding="utf-8")
        if template_content:
            (skill_dir / "assets" / "templates" / template_name).write_text(template_content, encoding="utf-8")
        if script_content and script_name:
            (skill_dir / "scripts" / script_name).write_text(script_content, encoding="utf-8")
        print(
            f"[ExpertCreate:{request_tag}] Expert files written "
            f"profile='{profile_path.name}' template='{template_name}' script='{script_name or ''}'."
        )
        
        # Return expert metadata
        return {
            "id": expert_id,
            "name": name_en or name or name_zh,
            "name_zh": name_zh or "",
            "name_en": name_en or "",
            "description": description,
            "profile_path": str(profile_path),
            "skill_path": str(skill_dir / "SKILL.md"),
            "tools_recommended": result.get("tools_recommended", []),
            "expertise": [],
        }


def create_expert(
    base_dir: Path,
    expert_id: str,
    name: str,
    description: str = "",
    use_llm: bool = True,
    *,
    name_zh: str = "",
    name_en: str = "",
    phase: str = "",
    request_id: str = "",
) -> Optional[Dict[str, Any]]:
    """
    Convenience function to create a new expert.

    Args:
    base_dir: Project base directory
    expert_id: Initial expert ID
    name: Expert display name
    description: Expert description
    use_llm: Whether to use LLM generation
    name_zh: Chinese name for the expert
    name_en: English name for the expert
    phase: Target execution phase (e.g. "ARCHITECTURE")

    Returns:
    Expert metadata dict if successful, None otherwise
    """
    generator = ExpertGenerator(base_dir)
    return generator.create_expert(
        expert_id,
        name,
        description,
        use_llm,
        name_zh=name_zh,
        name_en=name_en,
        phase=phase,
        request_id=request_id,
    )
