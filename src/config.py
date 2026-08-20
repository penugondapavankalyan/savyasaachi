"""
Central application settings.

Loads environment variables from a .env file (if present) at import time
using python-dotenv.  Falls back to real OS environment variables, so AWS
Lambda — which injects secrets as environment variables — works without any
.env file on disk.

Usage anywhere in the codebase:
    from src.config import settings
    url = settings.SUPABASE_URL

Design rules:
  - All secrets live here and ONLY here.
  - No other module calls os.environ directly.
  - Required keys raise a clear error on startup if missing.
  - Optional keys have sensible defaults.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# ──────────────────────────────────────────────────────────────────────────────
# Load .env
# ──────────────────────────────────────────────────────────────────────────────
# Walk up from this file's location until we find a .env file (or hit the
# filesystem root).  This handles running from any working directory.
_here = Path(__file__).resolve().parent          # src/
_root = _here.parent                              # project root

_env_path = _root / ".env"
if _env_path.exists():
    load_dotenv(dotenv_path=_env_path, override=False)
    # override=False means real env vars (e.g. Lambda) take precedence over .env


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _require(key: str) -> str:
    """Return the value of *key* or raise a clear error if it is missing."""
    value = os.environ.get(key)
    if not value:
        raise RuntimeError(
            f"Required environment variable '{key}' is not set. "
            f"Add it to your .env file or set it in the environment."
        )
    return value


def _optional(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


# ──────────────────────────────────────────────────────────────────────────────
# Settings class
# ──────────────────────────────────────────────────────────────────────────────

class Settings:
    """
    All application settings in one place.

    Required keys are validated lazily (on first access) so that the module
    can be imported without error even when not all keys are needed — useful
    during testing partial subsystems.
    """

    # ---- Supabase ----
    @property
    def SUPABASE_URL(self) -> str:
        return _require("SUPABASE_URL")

    @property
    def SUPABASE_SERVICE_ROLE_KEY(self) -> str:
        return _require("SUPABASE_SERVICE_ROLE_KEY")

    # ---- Upstash Redis ----
    @property
    def UPSTASH_REDIS_REST_URL(self) -> str:
        return _require("UPSTASH_REDIS_REST_URL")

    @property
    def UPSTASH_REDIS_REST_TOKEN(self) -> str:
        return _require("UPSTASH_REDIS_REST_TOKEN")

    # ---- Telegram ----
    @property
    def TELEGRAM_BOT_TOKEN(self) -> str:
        return _require("TELEGRAM_BOT_TOKEN")

    # ---- LLM provider selector ----
    @property
    def LLM_PROVIDER(self) -> str:
        """'ollama' (groq removed)"""
        return _optional("LLM_PROVIDER", "ollama")

    # ── Groq settings — commented out (not used) ─────────────────────────────
    # @property
    # def LLM_MODEL(self) -> str:
    #     """Primary Groq model. Fallback chain tries this first."""
    #     return _optional("LLM_MODEL", "qwen/qwen3.6-27b")
    #
    # @property
    # def LLM_FALLBACK_MODELS(self) -> list[str]:
    #     """Comma-separated ordered fallback Groq model list."""
    #     raw = _optional(
    #         "LLM_FALLBACK_MODELS",
    #         "llama-3.3-70b-versatile,openai/gpt-oss-20b,openai/gpt-oss-120b",
    #     )
    #     return [m.strip() for m in raw.split(",") if m.strip()]
    #
    # @property
    # def GROQ_API_KEY(self) -> str:
    #     """Required when LLM_PROVIDER=groq."""
    #     if self.LLM_PROVIDER == "groq":
    #         return _require("GROQ_API_KEY")
    #     return _optional("GROQ_API_KEY")

    # ── Ollama settings (used when LLM_PROVIDER=ollama) ──────────────────────
    # Ollama cloud API: https://ollama.com/v1/chat/completions (OpenAI-compatible)
    # Local Ollama API: http://localhost:11434/v1/chat/completions
    # Both use the same OpenAI-compatible format — only base_url and api_key differ.

    @property
    def OLLAMA_MODEL(self) -> str:
        """Ollama model name e.g. gemma4:31b-cloud, llama3.2, qwen2.5:7b"""
        if self.LLM_PROVIDER == "ollama":
            return _require("OLLAMA_MODEL")
        return _optional("OLLAMA_MODEL")

    @property
    def OLLAMA_BASE_URL(self) -> str:
        """
        Base URL for the Ollama OpenAI-compatible API.
        Cloud: https://ollama.com/v1
        Local: http://localhost:11434/v1  (default)
        """
        return _optional("OLLAMA_BASE_URL", "http://localhost:11434/v1")

    @property
    def OLLAMA_API_KEY(self) -> str:
        """
        Required for Ollama cloud (https://ollama.com/v1).
        Get from: https://ollama.com/settings/keys
        Not required for local Ollama (use any non-empty string).
        """
        if self.LLM_PROVIDER == "ollama" and "ollama.com" in self.OLLAMA_BASE_URL:
            return _require("OLLAMA_API_KEY")
        return _optional("OLLAMA_API_KEY", "ollama")  # local doesn't need a real key


    # ---- LLM inference settings ----
    @property
    def LLM_TEMPERATURE(self) -> float:
        """
        Sampling temperature for the LLM (0.0 = deterministic, 1.0 = creative).
        Low values are recommended for a billing agent to ensure consistent tool calls.
        Defaults to 0.1 if LLM_TEMPERATURE is not set or invalid.
        """
        raw = _optional("LLM_TEMPERATURE", "")
        if raw:
            try:
                val = float(raw)
                if 0.0 <= val <= 2.0:
                    return val
            except ValueError:
                pass
        return 0.1  # safe default for a structured billing agent

    # ---- Lambda / runtime ----
    @property
    def LAMBDA_ENV(self) -> str:
        return _optional("LAMBDA_ENV", "dev")

    @property
    def LOCAL_MODE(self) -> bool:
        """
        True when running via run_local.py (not on Lambda).
        Set LOCAL_MODE=true in .env to enable local document preview.
        When True, send_document() saves files to LOCAL_DOCS_OUTPUT_DIR
        and opens them with the OS default viewer instead of posting to Telegram.
        """
        return _optional("LOCAL_MODE", "false").lower() in ("true", "1", "yes")

    @property
    def LOCAL_DOCS_OUTPUT_DIR(self) -> str:
        """
        Directory where generated PDFs/PPTXs are saved when LOCAL_MODE=true.
        Defaults to a 'local_output' folder in the project root.
        """
        return _optional("LOCAL_DOCS_OUTPUT_DIR", str(_root / "local_output"))

    @property
    def LAMBDA_FUNCTION_URL(self) -> str:
        return _optional("LAMBDA_FUNCTION_URL")

    # ---- Tuning ----
    @property
    def MAX_HISTORY_MESSAGES(self) -> int:
        return int(_optional("MAX_HISTORY_MESSAGES", "10"))

    @property
    def DRAFT_BILL_TTL_HOURS(self) -> int:
        return int(_optional("DRAFT_BILL_TTL_HOURS", "4"))

    def __repr__(self) -> str:
        return (
            f"Settings("
            f"LLM_PROVIDER={self.LLM_PROVIDER!r}, "
            f"LLM_MODEL={self.LLM_MODEL!r}, "
            f"LAMBDA_ENV={self.LAMBDA_ENV!r})"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Module-level singleton — import this everywhere
# ──────────────────────────────────────────────────────────────────────────────
settings = Settings()
