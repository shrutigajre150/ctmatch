"""Runtime settings, read from ``CTMATCH_*`` environment variables.

Plain pydantic is enough here; pydantic-settings would add a dependency for a handful of fields.
"""

from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel, Field

_ENV_PREFIX = "CTMATCH_"


class Settings(BaseModel):
    """Application settings with safe local defaults."""

    database_url: str = "postgresql://ctmatch:ctmatch@localhost:5432/ctmatch"
    ctgov_base_url: str = "https://clinicaltrials.gov/api/v2"
    ctgov_page_size: int = Field(default=1000, ge=1, le=1000)
    ctgov_min_interval_s: float = Field(default=1.3, ge=0.0)
    ctgov_timeout_s: float = Field(default=30.0, gt=0.0)
    ctgov_max_retries: int = Field(default=5, ge=0)

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> Settings:
        """Build settings from environment variables, e.g. ``CTMATCH_DATABASE_URL``."""
        env = os.environ if environ is None else environ
        values: dict[str, Any] = {}
        for name in cls.model_fields:
            key = f"{_ENV_PREFIX}{name.upper()}"
            if key in env:
                values[name] = env[key]
        return cls.model_validate(values)
