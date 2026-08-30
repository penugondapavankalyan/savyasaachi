"""
KiranaAgent — PydanticAI agent wrapper.

LLM_PROVIDER=ollama → Ollama OpenAI-compatible API (cloud or local).
  Cloud: OLLAMA_BASE_URL=https://ollama.com/v1  + OLLAMA_API_KEY
  Local: OLLAMA_BASE_URL=http://localhost:11434/v1  (no key needed)
"""

from __future__ import annotations

import logging
import re
from typing import Any

from openai import AsyncOpenAI

from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
# from pydantic_ai.models.groq import GroqModel  # unused — Groq removed
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

from src.agent.config import AgentConfig, StoreContext
from src.agent.system_prompt import build_system_prompt
from src.agent.tool_registry import detect_intent, get_tools_for_state
from src.config import settings
from src.mcp import MCPInstances, get_mcp_instances

logger = logging.getLogger(__name__)
# Separate audit logger — logs tool call activity to stderr in run_local.py
_audit = logging.getLogger("kirana.tool_audit")

# ── Token budget per model ────────────────────────────────────────────────────
_MODEL_MAX_TOKENS: dict[str, int] = {
    "qwen/qwen3.6-27b":          2048,
    "llama-3.3-70b-versatile":   3000,   # 12K TPM — most headroom
    "openai/gpt-oss-20b":        2048,
    "openai/gpt-oss-120b":       2048,
}
_DEFAULT_MAX_TOKENS = 2048

# ── Errors that should trigger fallback to next model ────────────────────────
_FALLBACK_HTTP_CODES = {429, 503, 529}   # rate limit, unavailable, overloaded


def _is_recoverable(exc: Exception) -> bool:
    """Return True if we should try the next model instead of raising."""
    if isinstance(exc, UnexpectedModelBehavior):
        return True
    if isinstance(exc, ModelHTTPError):
        if exc.status_code in _FALLBACK_HTTP_CODES:
            return True
        if exc.status_code == 400:
            body = str(getattr(exc, "body", "") or "")
            if "tool calling" in body or "not supported" in body or "not found" in body:
                return True
    return False


# ── Resolution hint: stamp Redis with owner's choice ("khata" vs "collect") ──
# Must be async — called from KiranaAgent._execute() before tools are built.
# Pattern-matches the raw user message against "add to khata" variants so that
# collect_balance_now() can self-reject when the owner explicitly chose khata.
_KHATA_HINT_RE = re.compile(
    r"\b(add|put|save|store|record|keep)\b.{0,25}\b(khata|udhar)\b"
    r"|\b(khata|udhar)\b.{0,10}\b(add|put|save|store|record)\b",
    re.IGNORECASE,
)
_COLLECT_HINT_RE = re.compile(
    r"\b(collect|paying|pay|got it|received|collected|will pay|paying now|collect now|balance paid|paid now)\b",
    re.IGNORECASE,
)


async def _stamp_resolution_hint(tuid: int, user_message: str) -> None:
    """
    If a pending UNDERPAYMENT/OVERPAYMENT Redis key exists and the owner's
    message clearly states their resolution choice, stamp `resolution_hint`
    into the Redis payload so collect_balance_now() can validate intent.
    Silently no-ops on any error.
    """
    try:
        from src.agent.tool_registry import _get_redis  # avoid circular at module level
        redis = _get_redis()
        if not redis:
            return
        intent = await redis.get_pending_payment(tuid)
        if not intent or intent.get("intent_type") not in ("UNDERPAYMENT", "OVERPAYMENT"):
            return
        # Don't overwrite an already-set hint
        if intent.get("resolution_hint"):
            return
        msg = user_message.lower()
        if _KHATA_HINT_RE.search(msg):
            intent["resolution_hint"] = "khata"
            await redis.set_pending_payment(tuid, intent)
        elif _COLLECT_HINT_RE.search(msg):
            intent["resolution_hint"] = "collect"
            await redis.set_pending_payment(tuid, intent)
    except Exception:
        pass  # Never interrupt the main flow


class KiranaAgent:
    """
    Wraps PydanticAI Agent with a model fallback cascade.

    Public methods:
      run()            — returns the agent's text response
      run_with_trace() — returns (response, all_messages) for debugging
    """

    def __init__(self) -> None:
        self.config = AgentConfig(
            llm_provider=settings.LLM_PROVIDER,
            llm_model=settings.OLLAMA_MODEL,
            ollama_base_url=settings.OLLAMA_BASE_URL,
            max_history_messages=settings.MAX_HISTORY_MESSAGES,
            draft_bill_ttl_hours=settings.DRAFT_BILL_TTL_HOURS,
        )
        self.mcps: MCPInstances = get_mcp_instances()

        self._model_chain: list[Any] = [self._build_ollama_model()]
        self._model_names: list[str] = [settings.OLLAMA_MODEL]

    # def _build_groq_model(self, model_name: str) -> GroqModel:  # unused — Groq removed
    #     return GroqModel(
    #         model_name=model_name,
    #         api_key=self.config.groq_api_key or "",
    #     )

    def _build_ollama_model(self) -> OpenAIChatModel:
        """
        Build an OpenAI-compatible model pointed at Ollama.

        Works for both:
          - Ollama cloud:  OLLAMA_BASE_URL=https://ollama.com/v1  + OLLAMA_API_KEY=<key>
          - Local Ollama:  OLLAMA_BASE_URL=http://localhost:11434/v1  (no real key needed)
        """
        openai_client = AsyncOpenAI(
            base_url=settings.OLLAMA_BASE_URL,
            api_key=settings.OLLAMA_API_KEY,
        )
        return OpenAIChatModel(
            model_name=settings.OLLAMA_MODEL,
            provider=OpenAIProvider(openai_client=openai_client),
        )

    # ── Internal: shared agent execution ─────────────────────────────────────

    async def _execute(
        self,
        user_message: str,
        conversation_history: list[dict],
        store_context: StoreContext,
    ):
        """
        Core execution: build prompt, select tools, run model with fallback.
        Returns the raw AgentRunResult object.
        Emits audit log lines for every tool call and return.
        """
        system_prompt = build_system_prompt(store_context)
        # Pass has_active_draft so payment-mode words ("credit", "cash", "upi")
        # during an active billing session always route to BILLING, not KHATA.
        has_active_draft = bool(store_context.active_draft_bill_id)
        # Pass last assistant message so detect_intent can infer intent from
        # conversational context (e.g. follow-up product name after "which product?")
        last_assistant_msg = next(
            (m["content"] for m in reversed(conversation_history) if m.get("role") == "assistant"),
            None,
        )
        intent = detect_intent(user_message, has_active_draft=has_active_draft, last_assistant_msg=last_assistant_msg)

        # ── Write resolution_hint to Redis when owner explicitly chooses "add to khata"
        # or "collect now" on a pending UNDERPAYMENT/OVERPAYMENT.
        # This lets collect_balance_now() self-reject when the owner chose khata,
        # before any irreversible DB action happens.
        await _stamp_resolution_hint(store_context.telegram_user_id, user_message)

        # Context is passed so that context-bound wrappers can bake in
        # telegram_user_id and store_id — the LLM never sees those IDs.
        tools = get_tools_for_state(store_context.workflow_state, self.mcps, store_context, intent)
        message_history = _build_message_history(conversation_history)

        _audit.info(
            "[AGENT] state=%s  intent=%s  tools=%s  user=%r",
            store_context.workflow_state,
            intent,
            [getattr(t, "__name__", repr(t)) for t in tools],
            user_message[:80],
        )

        last_exc: Exception | None = None

        for idx, model in enumerate(self._model_chain):
            model_name = self._model_names[idx]
            max_tokens = _MODEL_MAX_TOKENS.get(model_name, _DEFAULT_MAX_TOKENS)
            # Onboarding states only need short responses (one question or one template).
            # Cap at 512 to prevent the model from batching all remaining steps into
            # a single runaway message — regardless of which model is in use.
            if store_context.workflow_state in ("UNREGISTERED", "PENDING_CATALOGUE"):
                max_tokens = min(max_tokens, 512)

            try:
                agent: Agent[None, str] = Agent(
                    model=model,
                    system_prompt=system_prompt,
                    tools=tools,
                    output_type=str,
                    model_settings=ModelSettings(
                        max_tokens=max_tokens,
                        timeout=30.0,
                        temperature=settings.LLM_TEMPERATURE,
                    ),
                )
                result = await agent.run(
                    user_message,
                    message_history=message_history,
                )

                # Emit audit lines for every tool call / return
                _log_tool_activity(result.all_messages(), model_name)

                if idx > 0:
                    logger.warning(
                        "Used fallback model %s (primary %s failed: %s)",
                        model_name,
                        self._model_names[0],
                        type(last_exc).__name__,
                    )
                return result

            except Exception as exc:
                if _is_recoverable(exc) and idx < len(self._model_chain) - 1:
                    logger.warning(
                        "Model %s failed (%s: %s), trying %s next...",
                        model_name,
                        type(exc).__name__,
                        str(exc)[:100],
                        self._model_names[idx + 1],
                    )
                    last_exc = exc
                    continue
                raise

        raise last_exc  # type: ignore[misc]

    # ── Public methods ────────────────────────────────────────────────────────

    async def run(
        self,
        user_message: str,
        conversation_history: list[dict],
        store_context: StoreContext,
    ) -> str:
        """
        Run the agent and return the text response.
        Used by handler.py (Lambda) and run_local.py.
        """
        result = await self._execute(user_message, conversation_history, store_context)
        return result.output

    async def run_with_trace(
        self,
        user_message: str,
        conversation_history: list[dict],
        store_context: StoreContext,
    ) -> tuple[str, list]:
        """
        Run the agent and return (response_text, all_messages).
        all_messages includes full tool call / return trace.
        Used by run_local.py /debug command.
        """
        result = await self._execute(user_message, conversation_history, store_context)
        return result.output, result.all_messages()


# ── Module-level helpers ──────────────────────────────────────────────────────

def _build_message_history(history: list[dict]) -> list:
    """
    Convert [{role, content}] dicts from Redis into pydantic-ai v2
    ModelMessage objects.

    Guards against non-dict items and missing keys (corrupted Redis data).
    """
    from pydantic_ai.messages import ModelRequest, UserPromptPart, TextPart

    messages = []
    for msg in history:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        content = msg.get("content", "") or ""
        if not isinstance(content, str):
            content = str(content)
        if role == "user" and content:
            messages.append(ModelRequest(parts=[UserPromptPart(content=content)]))
        elif role == "assistant" and content:
            messages.append(ModelResponse(parts=[TextPart(content=content)]))
    return messages


def _log_tool_activity(messages: list, model_name: str) -> None:
    """
    Walk all_messages() after a run and emit one audit log line per
    tool call and one per tool return.
    Warns loudly if NO tool calls were made (likely hallucination).
    """
    tool_calls_found = False
    for msg in messages:
        if isinstance(msg, ModelResponse):
            for part in msg.parts:
                if isinstance(part, ToolCallPart):
                    tool_calls_found = True
                    _audit.info(
                        "[TOOL CALL]   model=%-30s  tool=%-25s  args=%s",
                        model_name,
                        part.tool_name,
                        str(part.args)[:300],
                    )
        else:
            for part in getattr(msg, "parts", []):
                if isinstance(part, ToolReturnPart):
                    content_repr = str(part.content)
                    _audit.info(
                        "[TOOL RETURN] tool=%-25s  len=%-5d  result=%s",
                        part.tool_name,
                        len(content_repr),
                        content_repr[:300],
                    )
    if not tool_calls_found:
        _audit.warning(
            "[NO TOOLS CALLED] model=%s — agent responded without any tool call. "
            "Check: (1) store_id/telegram_user_id in system prompt, "
            "(2) correct workflow state, (3) stale Redis history.",
            model_name,
        )


# ── Module-level singleton ────────────────────────────────────────────────────

_agent: KiranaAgent | None = None


def get_agent() -> KiranaAgent:
    """Return the module-level KiranaAgent singleton."""
    global _agent
    if _agent is None:
        _agent = KiranaAgent()
    return _agent
