"""
Skill Parser - Parse SKILL.md files into structured data

Parses markdown files with YAML frontmatter into structured configuration:
- Extract frontmatter metadata (name, description, keywords)
- Extract workflow steps
- Build prompt instructions from content sections
"""

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import yaml

from .errors import SkillParseError, ConfigLoadError


class SkillParser:
    """
    Parse SKILL.md files into structured data.
    
    Supports markdown files with optional YAML frontmatter:
    ```markdown
    ---
    name: skill-name
    description: Short description for planner
    keywords: [keyword1, keyword2]
    ---
    
    # Workflow
    1. Step one
    2. Step two
    
    # Tools
    - tool1
    - tool2
    ```
    """
    
    # Pattern to match YAML frontmatter
    FRONTMATTER_PATTERN = re.compile(
        r'^---\s*\n(.*?)\n---\s*\n(.*)$',
        re.DOTALL
    )
    
    # Sections that are important for prompt building
    # Format: (section_key, possible_headers)
    IMPORTANT_SECTIONS = [
        ('workflow', ['workflow', '工作流', '工作流 (workflow)']),
        ('tools', ['tools', '工具集', '工具集 (tools)']),
        ('inputs', ['inputs', '输入参数', '输入参数 (inputs)', '输入']),
        ('outputs', ['outputs', '输出产物', '输出产物 (outputs)', '输出']),
        ('policies', ['policies', '策略']),
        ('notes', ['notes', '注意事项', '注意事项 (notes)']),
    ]
    
    def parse(self, path: Path) -> Tuple[Dict, str]:
        """
        Parse a SKILL.md file.
        
        Args:
            path: Path to the SKILL.md file
            
        Returns:
            Tuple of (frontmatter_dict, body_content)
            
        Raises:
            ConfigLoadError: If file doesn't exist
            SkillParseError: If YAML frontmatter is invalid
        """
        if not path.exists():
            raise ConfigLoadError(str(path), "File not found")
        
        try:
            content = path.read_text(encoding='utf-8')
        except Exception as e:
            raise ConfigLoadError(str(path), f"Failed to read file: {e}")
        
        match = self.FRONTMATTER_PATTERN.match(content)
        
        if not match:
            # No frontmatter, entire content is body
            return {}, content.strip()
        
        frontmatter_text, body = match.groups()
        
        try:
            frontmatter = yaml.safe_load(frontmatter_text) or {}
        except yaml.YAMLError as e:
            raise SkillParseError(str(path), f"Invalid YAML frontmatter: {e}")
        
        if not isinstance(frontmatter, dict):
            raise SkillParseError(str(path), "Frontmatter must be a YAML dictionary")
        
        return frontmatter, body.strip()
    
    def extract_workflow(self, body: str) -> List[str]:
        """
        Extract workflow steps from body content.
        
        Looks for numbered steps under a 'Workflow' or '工作流' heading.
        Supports formats like:
        - # Workflow
        - # 工作流
        - # 工作流 (Workflow)
        
        Args:
            body: The markdown body content
            
        Returns:
            List of workflow step strings
        """
        steps = []
        in_workflow = False
        
        for line in body.split('\n'):
            stripped = line.strip()
            
            # Check for workflow section header (multiple formats)
            # Matches: # Workflow, # 工作流, # 工作流 (Workflow), etc.
            lower_stripped = stripped.lower()
            if (lower_stripped.startswith('# workflow') or 
                lower_stripped.startswith('# 工作流') or
                lower_stripped == '# 工作流 (workflow)'):
                in_workflow = True
                continue
            
            # Check for next section (ends workflow)
            if in_workflow and stripped.startswith('#'):
                break
            
            # Extract numbered steps
            if in_workflow and re.match(r'^\d+[\.\)、]\s*', stripped):
                # Remove the step number prefix
                step_text = re.sub(r'^\d+[\.\)、]\s*', '', stripped)
                if step_text:
                    steps.append(step_text)
        
        return steps
    
    def extract_sections(self, body: str) -> Dict[str, str]:
        """
        Extract all markdown sections into a dictionary.
        
        Args:
            body: The markdown body content
            
        Returns:
            Dictionary mapping section titles to their content
        """
        sections = {}
        current_title = None
        current_content = []
        
        for line in body.split('\n'):
            # Match h1 and h2 headers
            header_match = re.match(r'^(#{1,2})\s+(.+)$', line)
            
            if header_match:
                # Save previous section
                if current_title:
                    sections[current_title] = '\n'.join(current_content).strip()
                
                current_title = header_match.group(2).strip()
                current_content = []
            else:
                current_content.append(line)
        
        # Save last section
        if current_title:
            sections[current_title] = '\n'.join(current_content).strip()
        
        return sections
    
    def build_prompt_instructions(
        self, 
        body: str, 
        max_length: int = 2000
    ) -> str:
        """
        Build prompt instructions from body content.
        
        Extracts important sections and formats them for LLM consumption.
        Respects max_length to avoid token budget issues.
        
        Args:
            body: The markdown body content
            max_length: Maximum character length for output
            
        Returns:
            Formatted prompt instructions string
        """
        sections = self.extract_sections(body)
        
        # Create lowercase mapping for case-insensitive lookup
        sections_lower = {k.lower(): v for k, v in sections.items()}
        
        instructions = []
        
        # Build instructions from important sections
        for section_key, possible_headers in self.IMPORTANT_SECTIONS:
            # Workflow and tool availability are injected separately by the runtime.
            # Skipping them here avoids duplicate or conflicting prompt content.
            if section_key in {'workflow', 'tools'}:
                continue

            # Try to find matching section (case-insensitive)
            content = None
            for header in possible_headers:
                content = sections_lower.get(header.lower())
                if content:
                    break
            
            if not content:
                continue
            
            # Format section header
            section_header = section_key.capitalize() if section_key.isascii() else section_key
            instructions.append(f"## {section_header}\n{content}")
        
        result = '\n\n'.join(instructions)
        
        # Truncate if necessary
        if len(result) > max_length:
            result = result[:max_length]
            # Try to end at a complete sentence or line
            last_newline = result.rfind('\n')
            if last_newline > max_length * 0.8:
                result = result[:last_newline]
            result += "\n... (content truncated due to length limit)"
        
        return result
    
    def extract_tool_list(self, body: str) -> List[str]:
        """
        Extract tool names from a Tools section.
        
        Args:
            body: The markdown body content
            
        Returns:
            List of tool names
        """
        tools = []
        sections = self.extract_sections(body)
        
        # Create lowercase mapping for case-insensitive lookup
        sections_lower = {k.lower(): v for k, v in sections.items()}
        
        # Look for tools section in various languages (case-insensitive)
        for key in ['tools', 'tool', '工具集', '工具']:
            content = sections_lower.get(key.lower())
            if content:
                # Extract bullet list items
                for line in content.split('\n'):
                    stripped = line.strip()
                    # Match bullet list items
                    if stripped.startswith('- ') or stripped.startswith('* '):
                        tool_name = stripped[2:].strip()
                        # Remove any description after colon or dash
                        tool_name = re.split(r'[\:\-]', tool_name)[0].strip()
                        if tool_name:
                            tools.append(tool_name)
                break
        
        return tools
    
    def validate_frontmatter(
        self, 
        frontmatter: Dict, 
        path: Path
    ) -> List[str]:
        """
        Validate frontmatter has required fields.
        
        Args:
            frontmatter: Parsed frontmatter dictionary
            path: Path to the file (for error messages)
            
        Returns:
            List of validation warnings (empty if all valid)
        """
        warnings = []
        
        # Recommended fields
        if 'name' not in frontmatter:
            warnings.append(f"'name' field missing in {path}")
        
        if 'description' not in frontmatter:
            warnings.append(f"'description' field missing in {path}")
        
        # Keywords should be a list
        if 'keywords' in frontmatter:
            if not isinstance(frontmatter['keywords'], list):
                warnings.append(f"'keywords' should be a list in {path}")
        
        return warnings
