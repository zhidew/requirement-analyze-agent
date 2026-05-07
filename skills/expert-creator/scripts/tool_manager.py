"""
Tool Manager Script

This script provides utilities for managing the system tool registry.
It allows adding, updating, and querying tools in the TOOL_REGISTRY.yaml.

Usage:
    from skills.expert_creator.scripts.tool_manager import ToolManager
    
    manager = ToolManager(base_dir)
    manager.add_tool(...)
    tools = manager.recommend_tools_for_domain(["api", "database"])
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


class ToolManager:
    """System tool registry manager."""
    
    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self.registry_path = self.base_dir / "skills" / "expert-creator" / "assets" / "TOOL_REGISTRY.yaml"
        self._data = self._load_registry()
    
    def _load_registry(self) -> Dict[str, Any]:
        """Load tool registry from YAML file."""
        if not self.registry_path.exists():
            return {"version": "1.0.0", "tools": [], "categories": [], "tool_combinations": []}
        
        with open(self.registry_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {"version": "1.0.0", "tools": [], "categories": [], "tool_combinations": []}
    
    def _save_registry(self) -> None:
        """Save tool registry to YAML file."""
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.registry_path, "w", encoding="utf-8") as f:
            yaml.dump(self._data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    
    def get_all_tools(self) -> List[Dict[str, Any]]:
        """Get all registered tools."""
        return self._data.get("tools", [])
    
    def get_tools_by_category(self, category: str) -> List[Dict[str, Any]]:
        """Get tools by category."""
        return [t for t in self.get_all_tools() if t.get("category") == category]
    
    def get_tool_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Get tool by name."""
        for tool in self.get_all_tools():
            if tool.get("name") == name:
                return tool
        return None
    
    def get_categories(self) -> List[Dict[str, Any]]:
        """Get all tool categories."""
        return self._data.get("categories", [])
    
    def get_tool_combinations(self) -> List[Dict[str, Any]]:
        """Get recommended tool combinations."""
        return self._data.get("tool_combinations", [])
    
    def add_tool(self, tool: Dict[str, Any]) -> bool:
        """
        Add a new tool to the registry.
        
        Args:
            tool: Tool definition dict with required fields:
                - name: Tool name (unique identifier)
                - category: Tool category
                - description_zh: Chinese description
                - description_en: English description
                - input_schema: Input parameter schema
                - output_schema: Output schema
                - use_cases: List of use cases
                - recommended_for: List of recommended expert IDs
                
        Returns:
            True if added successfully, False if tool already exists
        """
        # Validate required fields
        required = ["name", "category", "description_zh", "description_en"]
        for field in required:
            if field not in tool:
                raise ValueError(f"Missing required field: {field}")
        
        # Check if tool already exists
        if self.get_tool_by_name(tool["name"]):
            return False
        
        # Add tool
        if "tools" not in self._data:
            self._data["tools"] = []
        self._data["tools"].append(tool)
        self._save_registry()
        return True
    
    def update_tool(self, name: str, updates: Dict[str, Any]) -> bool:
        """
        Update an existing tool.
        
        Args:
            name: Tool name to update
            updates: Fields to update
            
        Returns:
            True if updated, False if tool not found
        """
        for i, tool in enumerate(self._data.get("tools", [])):
            if tool.get("name") == name:
                self._data["tools"][i].update(updates)
                self._save_registry()
                return True
        return False
    
    def remove_tool(self, name: str) -> bool:
        """
        Remove a tool from the registry.
        
        Args:
            name: Tool name to remove
            
        Returns:
            True if removed, False if not found
        """
        tools = self._data.get("tools", [])
        for i, tool in enumerate(tools):
            if tool.get("name") == name:
                tools.pop(i)
                self._save_registry()
                return True
        return False
    
    def recommend_tools_for_domain(self, domain_keywords: List[str]) -> List[str]:
        """
        Recommend tools based on domain keywords.
        
        Args:
            domain_keywords: List of domain-related keywords
            
        Returns:
            List of recommended tool names
        """
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
        
        # Verify tools exist in registry
        all_tool_names = {t.get("name") for t in self.get_all_tools()}
        return [t for t in recommended if t in all_tool_names]
    
    def get_tools_for_expert(self, expert_id: str) -> List[str]:
        """
        Get tools recommended for a specific expert.
        
        Args:
            expert_id: Expert capability ID
            
        Returns:
            List of tool names recommended for this expert
        """
        recommended = []
        for tool in self.get_all_tools():
            if expert_id in tool.get("recommended_for", []):
                recommended.append(tool.get("name"))
        return recommended
    
    def validate_tool_in_protocol(self, tool_name: str) -> bool:
        """
        Validate that a tool is implemented in protocol.py.
        
        Args:
            tool_name: Tool name to validate
            
        Returns:
            True if implemented, False otherwise
        """
        protocol_path = self.base_dir / "api_server" / "graphs" / "tools" / "protocol.py"
        if not protocol_path.exists():
            return False
        
        content = protocol_path.read_text(encoding="utf-8")
        return f'"{tool_name}"' in content or f"'{tool_name}'" in content
    
    def export_registry_json(self) -> str:
        """Export tool registry as JSON string."""
        return json.dumps(self._data, indent=2, ensure_ascii=False)
    
    def import_registry_json(self, json_str: str) -> None:
        """Import tool registry from JSON string."""
        self._data = json.loads(json_str)
        self._save_registry()


def list_all_tools(base_dir: Path) -> None:
    """Print all registered tools."""
    manager = ToolManager(base_dir)
    
    print("\n=== System Tool Registry ===\n")
    
    for category in manager.get_categories():
        cat_id = category.get("id")
        cat_name = category.get("name_zh", cat_id)
        print(f"\n[{cat_name}]")
        
        tools = manager.get_tools_by_category(cat_id)
        for tool in tools:
            name = tool.get("name", "")
            desc = tool.get("description_zh", "")
            print(f"  - {name}: {desc}")
    
    print("\n")


def recommend_tools(base_dir: Path, keywords: List[str]) -> List[str]:
    """Recommend tools for given domain keywords."""
    manager = ToolManager(base_dir)
    return manager.recommend_tools_for_domain(keywords)


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python tool_manager.py list              - List all tools")
        print("  python tool_manager.py recommend <kw>... - Recommend tools for keywords")
        sys.exit(1)
    
    base_dir = Path(__file__).parent.parent.parent.parent
    
    command = sys.argv[1]
    
    if command == "list":
        list_all_tools(base_dir)
    elif command == "recommend":
        keywords = sys.argv[2:]
        if not keywords:
            print("Please provide domain keywords")
            sys.exit(1)
        
        tools = recommend_tools(base_dir, keywords)
        print(f"\nRecommended tools for keywords {keywords}:")
        for tool in tools:
            print(f"  - {tool}")
    else:
        print(f"Unknown command: {command}")
        sys.exit(1)
