"""Slack Incoming Webhook publisher: Markdown report -> readable Block Kit.

- `# Title`        -> header block
- `## Section`     -> divider + header block
- `###`, `####`    -> bold line
- Markdown tables  -> aligned monospace table when it fits Slack's width
                      (long header words wrap onto a second line); otherwise
                      one bullet per row with labelled values
- `**bold**`, `[text](url)`, `<url>` -> Slack mrkdwn
- Long reports are split across several messages (50-block limit each).
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)
MAX_SECTION_CHARS = 2900      # Slack limit is 3000 per section text
MAX_BLOCKS_PER_MESSAGE = 48   # Slack limit is 50
MAX_TABLE_WIDTH = 72          # monospace characters before Slack wraps on desktop
MAX_HEADER_CHARS = 150
_SEPARATOR_ROW = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_EMPTY = {"", "—", "-"}


# -- inline conversion ------------------------------------------------------

def _escape(text: str) -> str:
    text = text.replace("&", "&amp;")
    return re.sub(r"<(?!https?://|mailto:)", "&lt;", text)


def _inline(text: str) -> str:
    """Markdown inline -> Slack mrkdwn."""
    text = _escape(text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"<\2|\1>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"__(.+?)__", r"*\1*", text)
    return text.replace("\\|", "|")


def _plain(text: str) -> str:
    """Markdown inline -> plain text (for code blocks and headers)."""
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"\1", text)
    text = re.sub(r"<(https?://[^>|]+)(\|[^>]*)?>", r"\1", text)
    text = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), text)
    text = re.sub(r"(?<!\w)[*_](\S(?:.*?\S)?)[*_](?!\w)", r"\1", text)
    return text.replace("`", "").replace("\\|", "|").strip()


def _width(text: str) -> int:
    width = 0
    for char in text:
        if unicodedata.combining(char) or char in "️‍":
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def _pad(text: str, width: int, right: bool = False) -> str:
    gap = " " * max(0, width - _width(text))
    return gap + text if right else text + gap


# -- tables -----------------------------------------------------------------

def _cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", line)]


def _wrap_words(text: str, width: int) -> list[str]:
    lines, current = [], ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and _width(candidate) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    return lines + ([current] if current else [])


def _is_numeric(text: str) -> bool:
    return bool(re.fullmatch(r"[-+−]?[$₹€£¥]?[\d,.]+[%xKMB]?", text.replace(" ", "")))


def _code_table(header: list[str], body: list[list[str]]) -> list[str] | None:
    """Aligned monospace rows, or None when the table is too wide."""
    columns = len(header)
    if any("http" in cell for row in body for cell in row):
        return None
    widths = []
    for i in range(columns):
        body_width = max((_width(row[i]) for row in body), default=0)
        word_width = max((_width(w) for w in header[i].split()), default=0)
        widths.append(max(body_width, word_width, 1))
    if sum(widths) + 2 * (columns - 1) > MAX_TABLE_WIDTH:
        return None
    wrapped = [_wrap_words(header[i], widths[i]) or [""] for i in range(columns)]
    height = max(len(w) for w in wrapped)
    numeric = [bool(body) and all(_is_numeric(row[i]) or row[i] in _EMPTY | {"n/a"} for row in body)
               for i in range(columns)]
    lines = []
    for level in range(height):
        # bottom-align wrapped headers so the last word sits on the rule
        parts = [(wrapped[i][level - (height - len(wrapped[i]))] if level >= height - len(wrapped[i]) else "")
                 for i in range(columns)]
        lines.append("  ".join(_pad(parts[i], widths[i], numeric[i]) for i in range(columns)).rstrip())
    lines.append("  ".join("─" * widths[i] for i in range(columns)))
    for row in body:
        lines.append("  ".join(_pad(row[i], widths[i], numeric[i]) for i in range(columns)).rstrip())
    return lines


def _list_table(header: list[str], body: list[list[str]]) -> list[str]:
    """One bullet per row: bold first cell, labelled short values, long values below."""
    out = []
    for row in body:
        first = _escape(_plain(row[0]))
        if len(header) == 2:
            out.append(f"• *{first}*: {_inline(row[1])}")
            continue
        marks, short, long = [], [], []
        for label, value in zip(header[1:], row[1:]):
            if value in _EMPTY:
                continue
            plain = _plain(value)
            if _width(plain) <= 3 and not any(ch.isalnum() for ch in plain):
                marks.append(plain)          # e.g. ✅ / ❌ used as the bullet
            elif len(plain) <= 40:
                short.append(f"{_plain(label)}: {_inline(value)}")
            else:
                long.append(f"      _{_plain(label)}:_ {_inline(value)}")
        bullet = " ".join(marks) if marks else "•"
        head = f"{bullet} *{first}*" if first else bullet
        out.append(head + ("  ·  " + "  ·  ".join(short) if short else ""))
        out.extend(long)
    return out


def _render_table(lines: list[str]) -> list[str]:
    """Return text pieces; code-block pieces are already fenced."""
    rows = [_cells(line) for line in lines if not _SEPARATOR_ROW.match(line.strip())]
    if not rows:
        return []
    columns = max(len(r) for r in rows)
    rows = [r + [""] * (columns - len(r)) for r in rows]
    header, body = rows[0], rows[1:]
    plain_header = [_plain(c) for c in header]
    plain_body = [[_plain(c) for c in row] for row in body]
    code = _code_table(plain_header, plain_body)
    if code is None:
        return ["\n".join(_list_table(header, body))]
    # split very long tables into several fenced pieces, repeating the header
    head_len = code.index(next(line for line in code if line.startswith("─"))) + 1
    head, rest = code[:head_len], code[head_len:]
    pieces, chunk = [], []
    for line in rest:
        if len("\n".join(head + chunk + [line])) + 8 > MAX_SECTION_CHARS:
            pieces.append("```\n" + "\n".join(head + chunk) + "\n```")
            chunk = []
        chunk.append(line)
    pieces.append("```\n" + "\n".join(head + chunk) + "\n```")
    return pieces


# -- document ---------------------------------------------------------------

def _header_block(text: str) -> dict[str, object]:
    text = _plain(text)
    if len(text) > MAX_HEADER_CHARS:
        text = text[: MAX_HEADER_CHARS - 1] + "…"
    return {"type": "header", "text": {"type": "plain_text", "text": text, "emoji": True}}


def markdown_to_blocks(report: str) -> tuple[str | None, list[dict[str, object]]]:
    """Convert the report; return (title, blocks). The title comes from the first `# ` line."""
    title: str | None = None
    blocks: list[dict[str, object]] = []
    buffer: list[str] = []

    def flush() -> None:
        text = "\n".join(buffer).strip()
        buffer.clear()
        while text:
            if len(text) <= MAX_SECTION_CHARS:
                piece, text = text, ""
            else:
                cut = text.rfind("\n", 0, MAX_SECTION_CHARS)
                cut = cut if cut > MAX_SECTION_CHARS // 2 else MAX_SECTION_CHARS
                piece, text = text[:cut], text[cut:].strip()
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": piece}})

    def add(piece: str) -> None:
        if len("\n".join(buffer)) + len(piece) + 1 > MAX_SECTION_CHARS:
            flush()
        buffer.append(piece)

    lines = report.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("|"):
            table = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table.append(lines[i])
                i += 1
            for piece in _render_table(table):
                add(piece)
            continue
        heading = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading:
            level, text = len(heading.group(1)), heading.group(2).strip()
            if level == 1 and title is None:
                title = _plain(text)
            elif level <= 2:
                flush()
                if blocks and blocks[-1]["type"] != "divider":
                    blocks.append({"type": "divider"})
                blocks.append(_header_block(text))
            else:
                add(f"\n*{_plain(text)}*")
        elif stripped == "---":
            flush()
            if blocks and blocks[-1]["type"] != "divider":
                blocks.append({"type": "divider"})
        elif stripped.startswith(("- ", "* ")):
            nested = len(line) - len(line.lstrip()) >= 2
            add(("      ◦ " if nested else "• ") + _inline(stripped[2:]))
        else:
            add(_inline(line))
        i += 1
    flush()
    return title, blocks


def build_slack_messages(report: str, title: str | None = None) -> list[dict[str, object]]:
    """Return one or more webhook payloads (Slack allows 50 blocks per message)."""
    found_title, blocks = markdown_to_blocks(report)
    title = title or found_title or "📊 Daily Portfolio Briefing"
    messages, chunk = [], []
    for block in blocks:
        if len(chunk) >= MAX_BLOCKS_PER_MESSAGE - 1:
            messages.append(chunk)
            chunk = []
        if not chunk and block["type"] == "divider":
            continue
        chunk.append(block)
    if chunk:
        messages.append(chunk)
    payloads = []
    total = len(messages) or 1
    for index, chunk in enumerate(messages or [[]], start=1):
        suffix = f" ({index}/{total})" if total > 1 else ""
        payloads.append({"text": f"{_plain(title)}{suffix}",
                         "blocks": [_header_block(f"{title}{suffix}"), *chunk]})
    return payloads


def build_slack_payload(report: str) -> dict[str, object]:
    """Backward-compatible: the first message only."""
    return build_slack_messages(report)[0]


def post_to_slack(webhook_url: str, report: str, timeout: float = 15.0, title: str | None = None) -> None:
    """Post a formatted report; raises RuntimeError without logging the secret URL."""
    for payload in build_slack_messages(report, title):
        request = Request(
            webhook_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                if response.status < 200 or response.status >= 300:
                    raise RuntimeError(f"Slack returned HTTP {response.status}.")
        except HTTPError as exc:
            raise RuntimeError(f"Slack returned HTTP {exc.code}.") from exc
        except URLError as exc:
            logger.error("Could not reach Slack: %s", exc.reason)
            raise RuntimeError("Could not reach Slack. Check the network and webhook configuration.") from exc
