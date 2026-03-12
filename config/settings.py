"""
config/settings.py

Loads and validates configuration from config.yaml.

LLM provider is now vendor-agnostic:
  - Anthropic  (default)
  - OpenAI
  - Azure OpenAI
  - Google Gemini
  - Ollama (local)

See config/config.example.yaml for all options.
"""
import os
import yaml
from pathlib import Path
from pydantic import BaseModel, Field, model_validator
from typing import Optional, List, Literal


# ─────────────────────────────────────────────────────────────────────
# SALESFORCE CONFIG (unchanged)
# ─────────────────────────────────────────────────────────────────────

class SalesforceConfig(BaseModel):
    instance_url: str
    username: Optional[str] = None
    password: Optional[str] = None
    security_token: str = ""
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    private_key_file: Optional[str] = None
    api_version: str = "61.0"

    model_config = {"extra": "ignore"}

    @model_validator(mode="after")
    def check_at_least_one_auth_method(self):
        has_userpass = bool(self.username and self.password)
        has_oauth    = bool(self.client_id and self.client_secret)
        has_jwt      = bool(self.client_id and self.private_key_file and self.username)
        if not (has_userpass or has_oauth or has_jwt):
            raise ValueError(
                "Salesforce auth not configured. Provide one of:\n"
                "  A) username + password  (+ security_token if not IP-whitelisted)\n"
                "  B) client_id + client_secret  (OAuth Connected App)\n"
                "  C) client_id + private_key_file + username  (JWT Bearer)"
            )
        return self

    @property
    def auth_method(self) -> str:
        if self.client_id and self.private_key_file:
            return "jwt"
        if self.client_id and self.client_secret:
            return "oauth_client_credentials"
        return "username_password"

    @property
    def password_with_token(self) -> str:
        return f"{self.password or ''}{self.security_token}"


# ─────────────────────────────────────────────────────────────────────
# LLM CONFIG — vendor-agnostic
# ─────────────────────────────────────────────────────────────────────

class LLMConfig(BaseModel):
    """
    Unified LLM configuration. Set 'provider' to switch vendors.

    provider: anthropic | openai | azure | google | ollama
    """
    provider: str = "anthropic"

    # Common fields
    api_key: str = ""
    model: str = ""
    max_tokens: int = 4096
    verify_ssl: bool = False     # set True if your network has valid SSL certs

    # Azure-specific
    azure_endpoint: Optional[str] = None
    azure_api_version: str = "2024-02-01"

    # Ollama-specific
    ollama_base_url: str = "http://localhost:11434"

    model_config = {"extra": "ignore"}

    @model_validator(mode="after")
    def check_provider_config(self):
        p = self.provider.lower()
        # Skip validation if api_key is empty — this happens when Settings
        # is constructed with defaults before the yaml is loaded.
        # The real validation fires in build_client() when it's actually used.
        if not self.api_key and p != "ollama":
            return self   # defer to build_client()

        if p == "azure" and not self.azure_endpoint:
            raise ValueError(
                "Azure OpenAI requires azure_endpoint.\n"
                "  Set llm.azure_endpoint in config.yaml  or  export AZURE_OPENAI_ENDPOINT=..."
            )
        return self

    def build_client(self):
        """Build and return the correct LLMClient for this config."""
        from tools.llm_client import build_llm_client
        return build_llm_client(self.model_dump())

    @property
    def display_name(self) -> str:
        model = self.model or "(default)"
        return f"{self.provider}/{model}"


# ── Backward-compatibility alias ─────────────────────────────────────
# Old code that references AnthropicConfig still works — it just
# creates an LLMConfig with provider="anthropic" under the hood.
class AnthropicConfig(LLMConfig):
    provider: str = "anthropic"
    model: str = "claude-sonnet-4-6"


# ─────────────────────────────────────────────────────────────────────
# OTHER CONFIGS
# ─────────────────────────────────────────────────────────────────────

class LoanStage(BaseModel):
    name: str
    keywords: List[str]


class CrawlConfig(BaseModel):
    metadata_types: str | List[str] = "all"
    parallel_requests: int = 3
    requests_per_second: int = 5
    max_per_type: int = 10000
    enable_cache: bool = True
    cache_dir: str = ".cache/metadata"


class OutputConfig(BaseModel):
    output_dir: str = "output/docs"
    format: Literal["markdown", "json", "both"] = "markdown"
    include_diagrams: bool = True
    include_raw_excerpts: bool = False
    loan_stages: List[LoanStage] = []


# ─────────────────────────────────────────────────────────────────────
# SETTINGS — root config object
# ─────────────────────────────────────────────────────────────────────

class Settings(BaseModel):
    salesforce: SalesforceConfig
    llm: LLMConfig = Field(default_factory=LLMConfig)
    crawl: CrawlConfig = Field(default_factory=CrawlConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)

    model_config = {"extra": "ignore"}

    @classmethod
    def load(cls, config_path: str = "config/config.yaml") -> "Settings":
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(
                f"Config file not found: {config_path}\n"
                "Run: python main.py setup"
            )
        with open(path) as f:
            data = yaml.safe_load(f)

        # ── Normalise the LLM section ─────────────────────────────────
        # Support both old 'anthropic:' key and new 'llm:' key.
        # If both somehow exist, 'llm:' wins.
        if "llm" not in data and "anthropic" in data:
            ant = data.pop("anthropic")
            data["llm"] = {
                "provider":   "anthropic",
                "api_key":    ant.get("api_key", ""),
                "model":      ant.get("model", "claude-sonnet-4-6"),
                "max_tokens": ant.get("max_tokens", 4096),
            }
        elif "anthropic" in data:
            # llm: already present — discard the stale anthropic: section
            data.pop("anthropic")

        data.setdefault("llm", {"provider": "anthropic"})
        sf  = data.setdefault("salesforce", {})
        llm = data["llm"]
        llm.setdefault("provider", "anthropic")
        provider = llm["provider"].lower()

        # ── Salesforce env var overrides ──────────────────────────────
        if v := os.getenv("SF_INSTANCE_URL"):   sf["instance_url"]   = v
        if v := os.getenv("SF_USERNAME"):        sf["username"]       = v
        if v := os.getenv("SF_PASSWORD"):        sf["password"]       = v
        if v := os.getenv("SF_SECURITY_TOKEN"):  sf["security_token"] = v
        if v := os.getenv("SF_CLIENT_ID"):       sf["client_id"]      = v
        if v := os.getenv("SF_CLIENT_SECRET"):   sf["client_secret"]  = v

        # ── LLM env var overrides — provider-aware ────────────────────
        # Generic overrides (highest priority — always apply)
        if v := os.getenv("LLM_PROVIDER"):  llm["provider"] = v; provider = v.lower()
        if v := os.getenv("LLM_API_KEY"):   llm["api_key"]  = v
        if v := os.getenv("LLM_MODEL"):     llm["model"]    = v

        # Provider-specific env vars — only apply to the matching provider
        # so ANTHROPIC_API_KEY doesn't silently inject into an OpenAI config
        if provider == "anthropic":
            if v := os.getenv("ANTHROPIC_API_KEY"):
                llm.setdefault("api_key", v)

        elif provider == "openai":
            if v := os.getenv("OPENAI_API_KEY"):
                llm.setdefault("api_key", v)

        elif provider == "azure":
            if v := os.getenv("AZURE_OPENAI_API_KEY"):
                llm.setdefault("api_key", v)
            if v := os.getenv("AZURE_OPENAI_ENDPOINT"):
                llm.setdefault("azure_endpoint", v)
            if v := os.getenv("AZURE_OPENAI_API_VERSION"):
                llm.setdefault("azure_api_version", v)

        elif provider == "google":
            if v := os.getenv("GOOGLE_API_KEY"):
                llm.setdefault("api_key", v)

        elif provider == "ollama":
            if v := os.getenv("OLLAMA_BASE_URL"):
                llm.setdefault("ollama_base_url", v)

        return cls(**data)


_settings: Optional[Settings] = None

def get_settings(config_path: str = "config/config.yaml") -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.load(config_path)
    return _settings

def reset_settings():
    global _settings
    _settings = None
