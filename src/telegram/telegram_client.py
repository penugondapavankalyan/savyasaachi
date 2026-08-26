"""
Telegram HTTP client.

Thin async wrapper around the Telegram Bot API (sendMessage, sendDocument,
sendChatAction, setWebhook).
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import httpx

from src.config import settings

logger = logging.getLogger(__name__)

# Telegram's hard per-message character limit.
_TG_MAX_CHARS = 4096

# ── MarkdownV2 escaper ────────────────────────────────────────────────────────
#
# Telegram MarkdownV2 spec — characters that MUST be backslash-escaped when
# they appear as literal text (not as formatting delimiters):
#   \ _ * [ ] ( ) ~ ` > # + - = | { } . !
#
# Source: https://core.telegram.org/bots/api#markdownv2-style
#
# Strategy: split the text into "protected" segments (code spans, code blocks,
# bold, italic, underline, strikethrough, spoiler, inline links) that must not
# be modified, and "plain" segments where EVERY MarkdownV2 special character
# gets a backslash prefix.
#
# Formatting kept intact (emitted verbatim after **bold**→*bold* conversion):
#   **bold** or *bold*    →  MarkdownV2 *bold*
#   _italic_ or __u__     →  MarkdownV2 as-is
#   `inline code`         →  interior NOT escaped
#   ```code block```      →  interior NOT escaped
#   ||spoiler||           →  as-is
#   [text](url)           →  as-is
#
# Plain-text regions: ALL 18 MarkdownV2 specials are escaped, including the
# backslash itself (escaped first so we never double-escape inserted slashes).

# All 18 MarkdownV2 special characters — ORDER MATTERS:
#   backslash must be first so we don't double-escape the slashes we insert.
_MV2_PLAIN_RE = re.compile(r'[\\\_\*\[\]\(\)\~\`\>\#\+\-\=\|\{\}\.\!]')

# Segments we must NOT touch the interior of.
# Matched in order so longer/outer patterns win over inner ones.
#   1. ```...```  fenced code block
#   2. `...`      inline code span
#   3. **...**    bold (Markdown v1 — converted to *...* on output)
#   4. *...*      bold/italic (MarkdownV2)
#   5. __...__    underline (MarkdownV2)
#   6. _..._      italic (MarkdownV2)
#   7. ||...||    spoiler (MarkdownV2)
#   8. [text](url) inline link
_PROTECTED_RE = re.compile(
    r"(```[\s\S]*?```"              # fenced code block  (must come before single-backtick)
    r"|`[^`\n]+`"                   # inline code span
    r"|\*\*[^*\n]+\*\*"            # **bold** (v1 style → converted on output)
    r"|\*[^*\n]+\*"                 # *bold* (v2 style)
    r"|__[^_\n]+__"                 # __underline__
    r"|_[^_\n]+_"                   # _italic_
    r"|\|\|[^|\n]+\|\|"             # ||spoiler||
    r"|\[[^\]]+\]\([^)]+\))",       # [text](url) inline link
    re.DOTALL,
)


def _escape_plain(text: str) -> str:
    """Escape all MarkdownV2 special chars in a plain-text region."""
    return _MV2_PLAIN_RE.sub(lambda c: "\\" + c.group(), text)


def _escape_markdownv2(text: str) -> str:
    """
    Escape *text* for Telegram MarkdownV2 parse mode.

    Protected segments (code, bold, italic, underline, spoiler, links) keep
    their delimiters intact (with **bold** converted to *bold*).  Per the
    Telegram MarkdownV2 spec, reserved characters must be escaped EVERYWHERE
    except inside code spans/blocks and link URLs — so the interior text of
    bold/italic/underline/spoiler segments is escaped too (e.g. *72.9* must
    be sent as *72\\.9* or Telegram rejects the whole message with
    "can't parse entities"). Code spans/blocks and links are emitted verbatim.
    All 18 MarkdownV2 special characters in plain-text regions are
    backslash-escaped, including the backslash itself (escaped first to
    avoid double-escaping).
    """
    result: list[str] = []
    last = 0

    for m in _PROTECTED_RE.finditer(text):
        start, end = m.start(), m.end()

        # Escape the plain-text region before this protected segment
        result.append(_escape_plain(text[last:start]))

        seg = m.group()
        if seg.startswith("```") or (seg.startswith("`") and not seg.startswith("``")):
            # fenced code block / inline code span — emit verbatim
            result.append(seg)
        elif seg.startswith("**") and seg.endswith("**"):
            # **bold** (v1 style) → *bold* (v2); escape interior text
            result.append("*" + _escape_plain(seg[2:-2]) + "*")
        elif seg.startswith("__") and seg.endswith("__"):
            result.append("__" + _escape_plain(seg[2:-2]) + "__")
        elif seg.startswith("||") and seg.endswith("||"):
            result.append("||" + _escape_plain(seg[2:-2]) + "||")
        elif seg.startswith("*") and seg.endswith("*"):
            result.append("*" + _escape_plain(seg[1:-1]) + "*")
        elif seg.startswith("_") and seg.endswith("_"):
            result.append("_" + _escape_plain(seg[1:-1]) + "_")
        else:
            # [text](url) inline link — emit verbatim
            result.append(seg)

        last = end

    # Escape any remaining trailing plain text
    result.append(_escape_plain(text[last:]))

    return "".join(result)


def _split_message(text: str) -> list[str]:
    """
    Split *text* into chunks that each fit within _TG_MAX_CHARS, splitting
    only at meaningful boundaries so markdown tables stay readable.

    Strategy (in priority order):
      1. If text fits in one chunk — return as-is.
      2. Split on double-newlines (paragraph / section boundaries) and
         accumulate sections until the next one would overflow.
      3. If a single section (e.g. a long table) is itself > _TG_MAX_CHARS,
         split it at row boundaries (lines starting with '|'), re-attaching
         the two header rows (header + separator) to every continuation chunk
         so the table is readable in each message.
      4. As a last resort, split on single newlines.

    Never splits mid-line.
    """
    if len(text) <= _TG_MAX_CHARS:
        return [text]

    chunks: list[str] = []

    def _flush(parts: list[str]) -> None:
        chunk = "\n\n".join(parts).strip()
        if chunk:
            chunks.append(chunk)

    # ── Step 1: split into paragraph-level sections ───────────────────────
    sections = text.split("\n\n")

    current_parts: list[str] = []
    current_len = 0

    for section in sections:
        section = section.strip()
        if not section:
            continue

        # Each section is joined with "\n\n" (2 chars) between parts
        join_cost = 2 if current_parts else 0
        needed = join_cost + len(section)

        if current_len + needed <= _TG_MAX_CHARS:
            current_parts.append(section)
            current_len += needed
        else:
            # Flush what we have so far
            if current_parts:
                _flush(current_parts)
                current_parts = []
                current_len = 0

            if len(section) <= _TG_MAX_CHARS:
                current_parts.append(section)
                current_len = len(section)
            else:
                # Section is a long table or block — split at row boundaries
                lines = section.split("\n")

                # Detect table header rows: first '|' line + '|---|' separator line
                header_rows: list[str] = []
                body_lines: list[str] = []
                in_header = True
                for line in lines:
                    stripped = line.strip()
                    if in_header and stripped.startswith("|"):
                        if len(header_rows) < 2:
                            header_rows.append(line)
                        else:
                            in_header = False
                            body_lines.append(line)
                    else:
                        in_header = False
                        body_lines.append(line)

                header_text = "\n".join(header_rows)
                header_cost = len(header_text) + 1  # +1 for the \n after header

                row_acc: list[str] = list(header_rows)
                row_len = header_cost

                for line in body_lines:
                    line_cost = len(line) + 1  # +1 for \n
                    if row_len + line_cost > _TG_MAX_CHARS and len(row_acc) > len(header_rows):
                        chunks.append("\n".join(row_acc).strip())
                        # Start next chunk with header so table is self-contained
                        row_acc = list(header_rows) + [line]
                        row_len = header_cost + line_cost
                    else:
                        row_acc.append(line)
                        row_len += line_cost

                if row_acc:
                    chunks.append("\n".join(row_acc).strip())

    if current_parts:
        _flush(current_parts)

    return [c for c in chunks if c]


class TelegramClient:
    def __init__(self) -> None:
        self.token = settings.TELEGRAM_BOT_TOKEN
        self.base_url = f"https://api.telegram.org/bot{self.token}"

    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str = "MarkdownV2",
        reply_to_message_id: Optional[int] = None,
    ) -> dict:
        """
        Send a text message to Telegram.

        Pipeline (in order):
          1. Escape text for MarkdownV2 (protects code/bold/italic/underline/
             spoiler/links; escapes all 18 MarkdownV2 special chars in plain
             regions, backslash first to avoid double-escaping).
          2. Split into ≤4096-char chunks at paragraph / table-row boundaries.
          3. Send each chunk with parse_mode=MarkdownV2.  If Telegram rejects
             a chunk (ok=false), retry once as plain text with parse_mode key
             OMITTED entirely (sending null is rejected by the Telegram API).

        Returns the API response of the last chunk sent.
        """
        # Step 1 — escape for MarkdownV2 (try/except: on any error keep raw text)
        use_markdown = True
        try:
            escaped = _escape_markdownv2(text)
        except Exception as exc:
            logger.warning("MarkdownV2 escape failed, using raw text: %s", exc)
            escaped = text
            use_markdown = False

        # Step 2 — split into safe-sized chunks
        chunks = _split_message(escaped)

        # Keep a parallel list of plain-text chunks for fallback (strip backslash escapes)
        # so the retry always sends readable text regardless of chunk boundaries.
        plain_chunks = _split_message(text)

        last_resp: dict = {}
        async with httpx.AsyncClient(timeout=10.0) as client:
            for i, chunk in enumerate(chunks):
                payload: dict = {"chat_id": chat_id, "text": chunk}
                if use_markdown:
                    payload["parse_mode"] = parse_mode
                if reply_to_message_id and i == 0:
                    payload["reply_to_message_id"] = reply_to_message_id

                try:
                    resp = await client.post(
                        f"{self.base_url}/sendMessage", json=payload
                    )
                    last_resp = resp.json()
                except Exception as exc:
                    logger.error(
                        "sendMessage HTTP error (chunk %d/%d): %s", i + 1, len(chunks), exc
                    )
                    raise

                if not last_resp.get("ok"):
                    err_desc = last_resp.get("description", "")
                    logger.warning(
                        "sendMessage chunk %d/%d failed (%s) — retrying as plain text",
                        i + 1, len(chunks), err_desc,
                    )
                    # Retry without parse_mode (omit the key entirely — sending null is rejected)
                    plain_text = plain_chunks[i] if i < len(plain_chunks) else text
                    plain_payload: dict = {"chat_id": chat_id, "text": plain_text}
                    if reply_to_message_id and i == 0:
                        plain_payload["reply_to_message_id"] = reply_to_message_id
                    try:
                        resp2 = await client.post(
                            f"{self.base_url}/sendMessage", json=plain_payload
                        )
                        last_resp = resp2.json()
                        if not last_resp.get("ok"):
                            logger.error(
                                "sendMessage plain-text retry also failed (chunk %d/%d): %s",
                                i + 1, len(chunks), last_resp.get("description"),
                            )
                    except Exception as exc2:
                        logger.error(
                            "sendMessage plain-text retry HTTP error (chunk %d/%d): %s",
                            i + 1, len(chunks), exc2,
                        )

        return last_resp

    async def send_document(
        self,
        chat_id: int,
        file_path: str,
        caption: Optional[str] = None,
    ) -> dict:
        """
        Send a file (PDF or PPTX).

        In LOCAL_MODE (run_local.py): copies the file to LOCAL_DOCS_OUTPUT_DIR
        and opens it with the OS default viewer. No Telegram API call is made.
        In production (Lambda): uploads to Telegram's sendDocument API.
        """
        if settings.LOCAL_MODE:
            return self._send_document_local(file_path, caption)
        async with httpx.AsyncClient(timeout=60.0) as client:
            with open(file_path, "rb") as f:
                resp = await client.post(
                    f"{self.base_url}/sendDocument",
                    data={"chat_id": str(chat_id), "caption": caption or ""},
                    files={"document": f},
                )
        return resp.json()

    def _send_document_local(self, file_path: str, caption: Optional[str]) -> dict:
        """
        Local-mode document handler: copies the file to LOCAL_DOCS_OUTPUT_DIR
        and opens it with the system default application (Preview on macOS,
        Edge/Acrobat on Windows, xdg-open on Linux).
        """
        import shutil
        import subprocess
        import sys as _sys
        from pathlib import Path as _Path

        out_dir = _Path(settings.LOCAL_DOCS_OUTPUT_DIR)
        out_dir.mkdir(parents=True, exist_ok=True)

        dest = out_dir / _Path(file_path).name
        shutil.copy2(file_path, dest)

        # Print visible path to terminal so developer always knows where the file is
        print(f"\n  📄  Document saved locally: {dest}")
        if caption:
            print(f"      Caption: {caption}")
        print()

        # Try to open with OS default viewer (best-effort, never blocks the agent)
        try:
            if _sys.platform == "win32":
                import os as _os
                _os.startfile(str(dest))
            elif _sys.platform == "darwin":
                subprocess.Popen(["open", str(dest)])
            else:
                subprocess.Popen(["xdg-open", str(dest)])
        except Exception:
            pass  # viewer launch is optional — file is saved regardless

        return {"ok": True, "local_path": str(dest)}

    async def send_typing_action(self, chat_id: int) -> None:
        """Show the typing indicator."""
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                f"{self.base_url}/sendChatAction",
                json={"chat_id": chat_id, "action": "typing"},
            )

    async def set_webhook(self, webhook_url: str) -> dict:
        """Register a URL as the Telegram webhook."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{self.base_url}/setWebhook",
                json={
                    "url": webhook_url,
                    "allowed_updates": ["message", "callback_query"],
                    "drop_pending_updates": True,
                },
            )
        return resp.json()

    async def get_webhook_info(self) -> dict:
        """Return current webhook configuration."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{self.base_url}/getWebhookInfo")
        return resp.json()


# ------------------------------------------------------------------
# Module-level singleton
# ------------------------------------------------------------------

_telegram: TelegramClient | None = None


def get_telegram_client() -> TelegramClient:
    global _telegram
    if _telegram is None:
        _telegram = TelegramClient()
    return _telegram
