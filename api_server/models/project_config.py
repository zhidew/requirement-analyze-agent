from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field, model_validator


class RepositoryType(str, Enum):
    GIT = "git"


class DatabaseType(str, Enum):
    MYSQL = "mysql"
    POSTGRESQL = "postgresql"
    OPENGAUSS = "opengauss"
    DWS = "dws"
    ORACLE = "oracle"
    SQLITE = "sqlite"


class KnowledgeBaseType(str, Enum):
    LOCAL = "local"
    REMOTE = "remote"


class RepositoryConfig(BaseModel):
    id: str
    name: str
    type: RepositoryType = RepositoryType.GIT
    url: str
    branch: str = "main"
    username: Optional[str] = None
    token: Optional[str] = None
    local_path: Optional[str] = None
    description: Optional[str] = None


class RepositoriesConfig(BaseModel):
    repositories: List[RepositoryConfig] = Field(default_factory=list)


class DatabaseConfig(BaseModel):
    id: str
    name: str
    type: DatabaseType
    host: str
    port: int
    database: str
    username: Optional[str] = None
    password: Optional[str] = None
    schema_filter: List[str] = Field(default_factory=list)
    description: Optional[str] = None


class DatabasesConfig(BaseModel):
    databases: List[DatabaseConfig] = Field(default_factory=list)


class KnowledgeBaseConfig(BaseModel):
    id: str
    name: str
    type: KnowledgeBaseType
    path: Optional[str] = None
    index_url: Optional[str] = None
    includes: List[str] = Field(default_factory=list)
    description: Optional[str] = None

    @model_validator(mode="after")
    def validate_location(self):
        if self.type == KnowledgeBaseType.LOCAL and not self.path:
            raise ValueError("`path` is required for local knowledge bases.")
        if self.type == KnowledgeBaseType.REMOTE and not self.index_url:
            raise ValueError("`index_url` is required for remote knowledge bases.")
        return self


class KnowledgeBasesConfig(BaseModel):
    knowledge_bases: List[KnowledgeBaseConfig] = Field(default_factory=list)


class ExpertConfig(BaseModel):
    id: str
    name: str
    name_zh: Optional[str] = None
    name_en: Optional[str] = None
    enabled: bool = True
    description: Optional[str] = None


class ExpertsConfig(BaseModel):
    experts: List[ExpertConfig] = Field(default_factory=list)


class ModelConfig(BaseModel):
    id: str
    name: str
    provider: str
    model_name: str
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    headers: Optional[Dict[str, str]] = None
    is_default: bool = False
    description: Optional[str] = None


class ModelConfigs(BaseModel):
    models: List[ModelConfig] = Field(default_factory=list)


class LlmConfig(BaseModel):
    llm_provider: str = "openai"
    openai_api_key: Optional[str] = None
    openai_base_url: Optional[str] = None
    openai_model_name: Optional[str] = None


class DebugConfig(BaseModel):
    llm_interaction_logging_enabled: bool = False
    llm_full_payload_logging_enabled: bool = False
