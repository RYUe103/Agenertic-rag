from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_data_dir: Path = Path("data")
    agent_config_path: Path = Path("config/agents.yaml")
    app_mock_external_services: bool = True
    local_index_enabled: bool = True
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_api_style: str = "chat_completions"
    dify_base_url: str = ""
    dify_knowledge_api_key: str = ""
    dify_dataset_id: str = ""
    dify_search_method: str = "semantic_search"
    pdf_platform_mode: str = "mock"
    pdf_platform_base_url: str = ""
    pdf_platform_api_key: str = ""
    pdf_platform_command: list[str] = Field(default_factory=list)
    pdf_platform_url_template: str = ""
    ocr_enabled: bool = False
    ocr_command: list[str] = Field(default_factory=list)
    mineru_enabled: bool = False
    mineru_url: str = ""
    mineru_api_key: str = ""
    mineru_cloud_enabled: bool = False
    mineru_timeout_seconds: float = 120.0
    mineru_return_content_list: bool = False
    mineru_return_images: bool = False
    mineru_fallback_on_error: bool = True
    agent_max_tool_calls: int = 6
    agent_max_retrieval_rounds: int = 3
    agent_max_evidence_for_answer: int = 12
    request_timeout_seconds: float = 30.0
    grep_timeout_seconds: float = 5.0
    auth_enabled: bool = True
    admin_username: str = "sansuokb"
    admin_password_hash: str = ""
    auth_session_hours: int = 12
    auth_cookie_secure: bool = False
    ldap_enabled: bool = False
    ldap_server_url: str = ""
    ldap_user_dn_template: str = ""
    ldap_base_dn: str = ""
    ldap_user_filter: str = "(uid={username})"
    ldap_group_base_dn: str = ""
    ldap_group_filter: str = "(member={user_dn})"
    ldap_group_attribute: str = "cn"
    ldap_group_role_map: dict[str, str] = Field(default_factory=dict)
    ldap_bind_dn: str = ""
    ldap_bind_password: str = ""
    ldap_timeout_seconds: float = 5.0
    external_api_enabled: bool = False
    external_api_key: str = ""
    mcp_enabled: bool = True
    mcp_allowed_hosts: list[str] = Field(default_factory=lambda: ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*", "testserver"])
    mcp_allowed_origins: list[str] = Field(default_factory=list)
    ingest_worker_count: int = 3
    ingest_upload_batch_size: int = 25
    ingest_max_files_per_job: int = 2000
    ingest_max_retries: int = 3
    ask_concurrency_limit: int = 40
    ask_queue_timeout_seconds: float = 10.0
    app_public_base_url: str = ""

    @field_validator("pdf_platform_command", "ocr_command", mode="before")
    @classmethod
    def parse_command(cls, value: object) -> object:
        if isinstance(value, str):
            return json.loads(value or "[]")
        return value

    @property
    def database_path(self) -> Path:
        return self.app_data_dir / "sources.db"

    @property
    def files_dir(self) -> Path:
        return self.app_data_dir / "files"

    @property
    def control_database_path(self) -> Path:
        return self.app_data_dir / "control.db"

    @property
    def ingest_queue_database_path(self) -> Path:
        return self.app_data_dir / "ingest-queue.db"

    @property
    def delete_queue_database_path(self) -> Path:
        return self.app_data_dir / "delete-queue.db"

    @property
    def ingest_staging_dir(self) -> Path:
        return self.app_data_dir / "ingest-staging"

    def ensure_directories(self) -> None:
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.ingest_staging_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
