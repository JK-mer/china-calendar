"""Configuration from environment, with house defaults.

The store is a plain directory on the media server, bind-mounted into the
containers (it moved off the Nextcloud share on 2026-08-02). Concurrent
writers are serialised by the per-uid locks in store.py, not by any sync
client. See the project wiki for the data model.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Local-development fallback; deployment sets PC_STORE_DIR. Must be durable:
# a run without PC_STORE_DIR writes the live store here.
DEFAULT_STORE = Path.home() / ".local/share/china-calendar/store"

USER_AGENT = (
    "china-calendar/0.1 (+research calendar tool)"
)


@dataclass
class LLMConfig:
    base_url: str = field(
        default_factory=lambda: os.environ.get(
            "PC_LLM_BASE_URL", "https://llm-gateway.example.internal/v1"
        )
    )
    api_key: str = field(default_factory=lambda: os.environ.get("PC_LLM_API_KEY", ""))
    # Two jobs, two models. Provider-side aliases rather than concrete ids:
    # pinning concrete ids risks 404s when models rotate. Small model for the
    # classifier (high volume, cheap, reasoning disabled via
    # reasoning_effort=none — validated live 2026-08-02); large model for the
    # extractor (date ranges, Chinese handling; rare).
    classifier_model: str = field(
        default_factory=lambda: os.environ.get("PC_CLASSIFIER_MODEL", "provider/small-text")
    )
    extractor_model: str = field(
        default_factory=lambda: os.environ.get("PC_EXTRACTOR_MODEL", "provider/large-text")
    )


@dataclass
class NextcloudConfig:
    """CalDAV target for the automated store → calendar sync. The app
    password comes from the environment (MCP Stack .env); when unset, sync
    is a graceful no-op."""

    base_url: str = field(
        default_factory=lambda: os.environ.get("PC_NC_BASE_URL", "https://nextcloud.example.internal")
    )
    user: str = field(default_factory=lambda: os.environ.get("PC_NC_USER", "ai-agent"))
    app_password: str = field(default_factory=lambda: os.environ.get("PC_NC_APP_PASSWORD", ""))
    calendar: str = field(
        default_factory=lambda: os.environ.get("PC_NC_CALENDAR", "china-calendar")
    )

    @property
    def configured(self) -> bool:
        return bool(self.app_password)

    @property
    def calendar_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/remote.php/dav/calendars/{self.user}/{self.calendar}/"


@dataclass
class Config:
    store_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("PC_STORE_DIR", DEFAULT_STORE))
    )
    llm: LLMConfig = field(default_factory=LLMConfig)
    nextcloud: NextcloudConfig = field(default_factory=NextcloudConfig)
    user_agent: str = USER_AGENT

    @property
    def events_dir(self) -> Path:
        return self.store_dir / "events"

    @property
    def raw_dir(self) -> Path:
        return self.store_dir / "raw"

    @property
    def ledger_dir(self) -> Path:
        return self.store_dir / "ledger"

    @property
    def sources_state_dir(self) -> Path:
        return self.store_dir / "sources"

    @property
    def index_path(self) -> Path:
        return self.store_dir / "index.json"

    @property
    def profile_path(self) -> Path:
        return self.store_dir / "profile.yaml"

    @property
    def digest_dir(self) -> Path:
        return self.store_dir / "digest"


def load_config() -> Config:
    return Config()
