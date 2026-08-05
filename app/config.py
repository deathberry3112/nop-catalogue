"""Settings, resolved once, from the environment then .env.

Nothing else in the app reads os.environ for a secret — it comes through here,
so there is exactly one place that knows where secrets live and exactly one
place to change when they move to a secrets manager.

Every integration is OPTIONAL by design. No OpenAI key means tier 3 is off and
colour resolution behaves exactly as it did before it existed. No Supabase
credentials means runs are not recorded. Neither is allowed to stop a run: the
automation's job is to add products, and it must not become undeployable
because a telemetry backend is down.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_env_file(path: Path) -> None:
    """Put a KEY=value file into os.environ without overwriting what's there.

    The real environment always wins — that is what lets a container override a
    checked-out .env, and what stops a developer's local file from leaking into
    a deployed run.
    """
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and value and key not in os.environ:
            os.environ[key] = value


_load_env_file(ROOT / ".env")


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    admin_base_url: str = ""
    admin_email: str = ""
    admin_password: str = ""

    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"

    supabase_url: str = ""
    supabase_service_key: str = ""
    #: Postgres wire-protocol URL, used only by sql.py. The application never
    #: touches it — PostgREST covers everything it does — but PostgREST cannot
    #: run DDL, so schema changes need a real database connection.
    supabase_db_url: str = ""

    # --- capability probes: what is actually wired up right now -----------

    @property
    def llm_enabled(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def store_enabled(self) -> bool:
        return bool(self.supabase_url and self.supabase_service_key)


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings(
        admin_base_url=os.environ.get("ADMIN_BASE_URL", ""),
        admin_email=os.environ.get("ADMIN_EMAIL", ""),
        admin_password=os.environ.get("ADMIN_PASSWORD", ""),
        openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
        openai_model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        supabase_url=os.environ.get("SUPABASE_URL", ""),
        supabase_service_key=os.environ.get("SUPABASE_SERVICE_KEY", ""),
        supabase_db_url=os.environ.get("SUPABASE_DB_URL", ""),
    )
