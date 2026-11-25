"""Application configuration loaded from TOML files and environment variables."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

# ─── Secrets from environment ─────────────────────────────────


class Settings(BaseSettings):
    """Secrets and connection strings — loaded from environment variables."""

    model_config = SettingsConfigDict(extra="ignore")

    mongodb_url: str = "mongodb://localhost:27017"
    mongodb_database: str = "congiuntura"
    agent_mongodb_url: str = ""


# ─── App config from config/app.toml ──────────────────────────


class PollingConfig(BaseModel):
    interval_minutes: int = 5


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000


class LoggingConfig(BaseModel):
    level: str = "INFO"


class ProcessingConfig(BaseModel):
    enabled: bool = True
    llm_config: str = "config/llm.toml"
    cascade_name: str = "conjuncture"
    interval_minutes: int = 2
    max_content_chars: int = 4000
    model_path: str = "config/extraction_model.py"


class AppConfig(BaseModel):
    """Application configuration loaded from config/app.toml."""

    polling: PollingConfig = PollingConfig()
    server: ServerConfig = ServerConfig()
    logging: LoggingConfig = LoggingConfig()
    processing: ProcessingConfig = ProcessingConfig()


def load_app_config(config_path: str = "config/app.toml") -> AppConfig:
    """Load application configuration from a TOML file."""
    path = Path(config_path)
    if not path.exists():
        return AppConfig()
    with open(path, "rb") as f:
        data: dict[str, Any] = tomllib.load(f)
    return AppConfig(**data)


# ─── Feeds config from config/feeds.toml ──────────────────────


class FeedConfig(BaseModel):
    label: str
    url: str


class AgencyConfig(BaseModel):
    name: str
    language: str
    country: str  # geo area attributed to the agency's releases (required)
    feeds: list[FeedConfig]


def load_feeds_config(config_path: str = "config/feeds.toml") -> dict[str, AgencyConfig]:
    """Load feeds configuration from a TOML file.

    Returns a dict mapping agency slug → AgencyConfig.
    """
    path = Path(config_path)
    with open(path, "rb") as f:
        data: dict[str, Any] = tomllib.load(f)

    agencies: dict[str, AgencyConfig] = {}
    for slug, raw in data.items():
        agencies[slug] = AgencyConfig(**raw)
    return agencies


# ─── Extraction model loader ──────────────────────────────────


def load_extraction_model(model_path: str = "config/extraction_model.py"):
    """Dynamically import the LLMExtraction Pydantic model from a Python file.

    The model is defined as a regular Python class in config/extraction_model.py.
    This allows users to edit the model (add fields, change Literal values)
    without touching application code.
    """
    import importlib.util

    path = Path(model_path)
    spec = importlib.util.spec_from_file_location("extraction_model", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load extraction model from {model_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.LLMExtraction
