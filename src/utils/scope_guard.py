"""
scope_guard.py — Pre-LLM off-topic message filter.

DESIGN PHILOSOPHY — block only, never match store content
─────────────────────────────────────────────────────────
Earlier versions tried to ALLOWLIST store-related keywords. That always breaks:
an owner can sell any product (Horlicks, Surf Excel, "wheat aata", local brand
names) — no keyword list can cover them. Every new product causes a false block.

Correct approach: ONLY block messages that are provably general-knowledge
questions. Everything else is forwarded to the agent. The agent's own Rule 23
(SCOPE — STRICT in system_prompt.py) is the second line of defence for anything
that genuinely slips through.

What does a kirana owner actually send that is off-topic?
  Almost exclusively WH-questions: "what is python?", "who is modi?",
  "explain photosynthesis", "define osmosis".

What does a kirana owner send that MUST pass?
  - Any instruction with a product name: "2 wheat aata", "Horlicks 3",
    "surf excel add karo", "naveen ka udhar" — these look like nothing.
  - Short replies: "yes", "no", "done", "500", "ok", "cancel".
  - Mid-sentence continuations mid-billing session.
  - Regional language phrases in any mix.

Logic (in order):
  1. /commands          → always forward.
  2. Starts with digit  → always forward (qty+product billing lines).
  3. WH-question opener → block UNLESS the message also contains a store
                          context signal (number, store keyword, or
                          known regional term). This allows:
                            "what is gst?"            → has store keyword
                            "how much did ramesh pay" → has store keyword
                            "how much is outstanding" → has store keyword
                          And blocks:
                            "what is python?"         → no store signal
                            "explain photosynthesis"  → no store signal
  4. Everything else    → always forward. A kirana owner will not type
                          essay-length off-topic prose unprompted.

Telegram /commands (/new, /start, /status …) always pass through.
"""

from __future__ import annotations

import re

# ── WH-question openers ───────────────────────────────────────────────────────
# A message that STARTS with one of these is a candidate for blocking.
# Only actually blocked if it contains NO store context signal (Rule 3 below).
_WH_OPENER_RE = re.compile(
    r"^(what|who|why|how|when|where|which|whose|whom|"
    r"explain|define|describe|"
    r"tell me|can you tell|"
    r"what'?s|how'?s|who'?s)\b",
    re.IGNORECASE,
)

# ── Store context signals ─────────────────────────────────────────────────────
# Used ONLY when a WH-question opener is detected (Rule 3).
# Deliberately narrow — just enough to rescue store-flavoured WH-questions.
# NOT used to validate non-WH messages (that was the old broken approach).
#
# Categories:
#   • Numbers / quantities  — "how much did ramesh pay 500"
#   • Core store nouns      — bill, stock, gst, khata, payment, customer …
#   • Hindi/Telugu/Tamil    — baaki, udhar, kadan, yekkuva …
_STORE_SIGNAL_RE = re.compile(
    # A quantity or rupee amount — "how much did ramesh pay 500", "how many 12"
    # Require at least 2 digits so that a lone "2" in "world war 2" or "1" in
    # "world war 1" does not falsely rescue off-topic history questions.
    r"(?<!\w)\d{2,}(?!\w)"
    r"|"
    # Core store nouns (deliberately short — no grocery product names)
    r"\b(bill|billing|invoice|receipt|stock|inventory|"
    r"payment|pay|paid|cash|upi|credit|debit|"
    r"khata|udhar|balance|ledger|outstanding|owe|owed|"
    r"product|item|catalogue|catalog|"
    r"gst|tax|cgst|sgst|igst|hsn|gstin|"
    r"store|shop|customer|customers|order|"
    r"report|analytics|summary|sales|revenue|"
    r"amount|total|price|cost|rate|mrp|due|"
    # Hindi/Hinglish
    r"baaki|baki|kitna|hisab|dena|lena|"
    # Telugu
    r"yekkuva|chupinchu|cheyyi|sarakulu|"
    r"icchaadu|icchadu|iccharu|icchamu|"
    # Tamil
    r"kadan|pesam|evvalavu|sarakku|hesabu|paarunga)\b",
    re.IGNORECASE,
)


def is_in_scope(message: str) -> bool:
    """
    Return True if *message* should be forwarded to the agent.
    Return False only if it is clearly a general-knowledge question with
    no store context signal.

    Decision order:
      1. Empty / /commands / digit-led  → always forward.
      2. WH-question opener detected    → forward only if a store signal
                                          (number, store noun, regional term)
                                          is also present; otherwise block.
      3. Everything else                → always forward.
    """
    if not message:
        return True

    stripped = message.strip()

    # Rule 1a — Telegram slash commands
    if stripped.startswith("/"):
        return True

    # Rule 1b — Digit-led messages are billing/stock instructions
    # "2 wheat aata", "500 cash", "3 Horlicks" — forward regardless.
    if stripped and stripped[0].isdigit():
        return True

    # Rule 2 — WH-question opener present
    # These are the only messages we ever consider blocking.
    # Block only if there is NO store context signal anywhere in the message.
    if _WH_OPENER_RE.match(stripped):
        return bool(_STORE_SIGNAL_RE.search(stripped))

    # Rule 3 — Everything else: forward unconditionally.
    # An owner mid-session may type any product name, person name, or
    # regional phrase that no keyword list can anticipate.
    return True


# ── Canned response ───────────────────────────────────────────────────────────
# Returned verbatim by handler.py without calling the LLM.
OFF_TOPIC_REPLY: str = (
    "I can only help with your kirana store — billing, stock, catalogue, and accounts. "
    "How can I help you today?"
)

# ── Stale-draft greeting interceptor ─────────────────────────────────────────
# When there is an active draft bill and the owner sends a bare greeting,
# the LLM is bypassed entirely and this fixed keyword-menu reply is returned.
# This prevents the model from generating a numbered list which causes the
# next message like "2 aata" to be misread as "menu option 2".
#
# Exported so both handler.py (production) and run_local.py (REPL) use the
# exact same logic from one place — no duplication.

_STALE_DRAFT_GREETING_RE = re.compile(
    r"^(hi+|hello+|hey+|good\s*(morning|afternoon|evening|night)|"
    r"namaste|namaskar|hola|sup|whats?\s*up|howdy|greetings|"
    r"hii+|helo+|hai+|ello)\W*$",
    re.IGNORECASE,
)

STALE_DRAFT_REPLY: str = (
    "You have an open bill. "
    "Say *continue* to keep adding items, *finalize* to pay, or *cancel* to discard it."
)


def is_stale_draft_greeting(message: str, has_active_draft: bool) -> bool:
    """
    Return True if the message is a bare greeting AND there is an active draft
    bill — meaning the LLM should be bypassed and STALE_DRAFT_REPLY sent instead.

    Args:
        message:          The raw user message.
        has_active_draft: True if store_context.active_draft_bill_id is set.
    """
    if not has_active_draft:
        return False
    return bool(_STALE_DRAFT_GREETING_RE.match(message.strip()))
