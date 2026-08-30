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
    r"store|shop|business|customer|customers|order|"
    r"report|analytics|summary|sales|revenue|"
    r"amount|total|price|cost|rate|mrp|due|"
    # Broader day-to-day store operation words — rescue WH-questions like
    # "why is today's collection low", "when should I reorder", "how is
    # business today" that don't contain the narrower nouns above.
    r"reorder|running out|running low|collection|profit|loss|"
    r"expense|expenses|sold|selling|supplier|vendor|"
    r"low stock|out of stock|stock left|"
    r"day end|close the day|closing day|top item|best seller|"
    # Generic "how much <any product> is left/remaining" pattern — product
    # names (salt, sugar, rice, ...) are unbounded and can never be listed
    # here, so anchor on the surrounding words instead.
    r"left|remaining|available|quantity|qty|units|"
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


# ── Workflow gate interceptors ────────────────────────────────────────────────
# UNREGISTERED and PENDING_CATALOGUE gates bypass the LLM entirely for messages
# that are clearly not part of the required onboarding step.
# These are code-level guards — no model compliance required.
#
# Pass-through logic:
#   UNREGISTERED:       Short registration answers (names, "yes"/"no", "skip",
#                       phone numbers, 2-digit state codes, GSTINs, addresses,
#                       payment mode words) must always pass through to the LLM
#                       so the registration flow can continue. Everything else
#                       (billing requests, product requests, etc.) is blocked.
#
#   PENDING_CATALOGUE:  Catalogue inputs (product details, "yes"/"no",
#                       template-filled replies, "skip") must always pass through.
#                       Billing or khata requests are blocked.

# Words/patterns that are legitimate inputs during registration — always forward.
_REGISTRATION_PASSTHROUGH_RE = re.compile(
    r"^/"                                        # /commands always pass
    r"|^\d{10}$"                                 # 10-digit phone number
    r"|^\d{2}$"                                  # 2-digit state code
    r"|^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$"  # GSTIN
    r"|^(yes|no|ok|okay|correct|skip|done|hi|hello|hey|cash|upi|credit|"
    r"first|last|firstname|lastname)\W*$"        # short confirmation/nav words
    r"|^\w[\w\s\.\-,#/]*$",                      # names / addresses (catch-all
                                                 # for single-line text without
                                                 # billing keywords below)
    re.IGNORECASE,
)

# Billing/product/khata keywords that should be blocked during UNREGISTERED state.
_UNREGISTERED_BLOCK_RE = re.compile(
    r"\b(bill|billing|invoice|receipt|make bill|create bill|"
    r"add product|add item|catalogue|catalog|stock|inventory|"
    r"khata|udhar|payment|pay|credit|balance|customer|"
    r"report|analytics|sales|revenue|gst report)\b",
    re.IGNORECASE,
)

# Billing/khata/analytics keywords that should be blocked during PENDING_CATALOGUE.
_PENDING_CATALOGUE_BLOCK_RE = re.compile(
    r"\b(bill|billing|invoice|make bill|create bill|"
    r"khata|udhar|payment|pay|credit sale|balance|customer|"
    r"report|analytics|sales|revenue|gst report|"
    r"stock movement|receive stock)\b",
    re.IGNORECASE,
)

REGISTRATION_GATE_REPLY: str = (
    "Please complete your shop registration first. "
    "Let me continue from where we left off — what is your shop name?"
)

CATALOGUE_GATE_REPLY: str = (
    "You need to add at least one product to your catalogue before you can do that. "
    "Let's add your first product now!\n\n"
    "Copy the template below, fill in the real values, and send it back:\n"
    "```\n"
    "1. Name - \n"
    "2. Type - Branded / Loose\n"
    "3. Unit - PIECE / KG / G / L / ML / PACKET / DOZEN / BUNDLE\n"
    "4. Cost price (Rs.) - \n"
    "5. Selling price / MRP (Rs.) - \n"
    "6. GST rate - 5  (0 for loose, else 5 / 12 / 18 / 28 for branded)\n"
    "7. Brand - Company Name (skip if loose)\n"
    "8. Reorder level - \n"
    "9. Initial stock - \n"
    "```"
)


def is_unregistered_block(message: str) -> bool:
    """
    Return True if the message should be blocked during UNREGISTERED state —
    i.e. it contains billing/product/khata keywords that have nothing to do
    with completing registration.

    Slash commands always pass through (return False).
    """
    stripped = message.strip()
    if stripped.startswith("/"):
        return False
    return bool(_UNREGISTERED_BLOCK_RE.search(stripped))


def is_pending_catalogue_block(message: str) -> bool:
    """
    Return True if the message should be blocked during PENDING_CATALOGUE state —
    i.e. it contains billing/khata/analytics keywords that require a catalogue
    to be set up first.

    Slash commands always pass through (return False).
    """
    stripped = message.strip()
    if stripped.startswith("/"):
        return False
    return bool(_PENDING_CATALOGUE_BLOCK_RE.search(stripped))
