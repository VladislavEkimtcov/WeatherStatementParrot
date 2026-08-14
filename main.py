#!/usr/bin/env python3
"""WeatherStatementParrot — full-terminal TUI for NOAA weather analysis."""

from __future__ import annotations

import curses
import json
import re
import os
import sys
import textwrap
import time
import threading
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup, NavigableString, Tag
from dotenv import load_dotenv, set_key
from openai import OpenAI


# ── Configuration ────────────────────────────────────────────────────────────

_env_path = Path(__file__).resolve().parent / ".env"
if not _env_path.exists():
    print(
        "ERROR: .env file not found.\n"
        f"Expected at: {_env_path}\n"
        "Copy .env.example to .env and fill in your settings before running.",
        file=sys.stderr,
    )
    sys.exit(1)

load_dotenv(_env_path)

STATEMENT_ENDPOINT = os.getenv("STATEMENT_ENDPOINT", "")
OPENAI_ENDPOINT = os.getenv("OPENAI_ENDPOINT", "http://127.0.0.1:6767/v1")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL_ID = os.getenv("OPENAI_MODEL_ID", "")
FALLBACK_MODEL_1 = os.getenv("FALLBACK_MODEL_1", "")
FALLBACK_MODEL_2 = os.getenv("FALLBACK_MODEL_2", "")
EXTRA_PROMPT = os.getenv("EXTRA_PROMPT", "")

STATEMENT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "statement.json")
PROMPT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "PROCESS_PROMPT.md")

DEFAULT_INTERVAL_MINUTES = 60
INTERVAL_STEP_MINUTES = 15
MIN_INTERVAL_MINUTES = 15
REFRESH_INTERVAL_ENV_VAR = "REFRESH_INTERVAL_MINUTES"

APP_TITLE = "NOAA Weather Statement"


# ── Helpers ──────────────────────────────────────────────────────────────────

def load_prompt_template() -> str:
    """Read the PROCESS_PROMPT.md template from disk."""
    with open(PROMPT_FILE, "r", encoding="utf-8") as fh:
        return fh.read()


def _normalize_interval_minutes(value: str | int | None) -> int:
    """Return a valid refresh interval in minutes."""
    try:
        interval = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_MINUTES
    return max(MIN_INTERVAL_MINUTES, interval)


def load_refresh_interval_minutes() -> int:
    """Load the refresh interval from .env, falling back to the default."""
    return _normalize_interval_minutes(os.getenv(REFRESH_INTERVAL_ENV_VAR))


def persist_refresh_interval_minutes(interval_min: int) -> None:
    """Persist the refresh interval to .env for future runs."""
    normalized = _normalize_interval_minutes(interval_min)
    os.environ[REFRESH_INTERVAL_ENV_VAR] = str(normalized)
    try:
        set_key(
            str(_env_path),
            REFRESH_INTERVAL_ENV_VAR,
            str(normalized),
            quote_mode="never",
        )
    except Exception:
        pass


def render_symbian_html(html_content: str) -> str:
    """Minimalist HTML handling engine for monochrome Symbian mobile web.

    Renders HTML into clean, monochrome-friendly plain text with structured
    formatting for headings, paragraphs, lists, tables, links, images,
    blockquotes, and inline emphasis while stripping scripts and styles.
    """
    if not html_content or not html_content.strip():
        return ""

    soup = BeautifulSoup(html_content, "html.parser")

    # Strip non-content / non-display tags
    for tag in soup.find_all(
        ["script", "style", "noscript", "head", "meta", "link", "svg", "canvas", "iframe", "form", "input", "button", "select", "option"]
    ):
        tag.decompose()

    def _render_node(node, list_depth: int = 0) -> str:
        if isinstance(node, NavigableString):
            text = str(node)
            if node.find_parent("pre"):
                return text
            cleaned = re.sub(r"\s+", " ", text)
            return cleaned

        if not isinstance(node, Tag):
            return ""

        tag_name = node.name.lower()

        if tag_name == "br":
            return "\n"
        elif tag_name == "hr":
            return "\n----------------------------------------\n"
        elif tag_name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            inner = "".join(_render_node(c, list_depth) for c in node.children).strip()
            if not inner:
                return ""
            prefix = "# " if tag_name == "h1" else ("## " if tag_name == "h2" else "### ")
            return f"\n\n{prefix}{inner}\n\n"
        elif tag_name in ("p", "div", "section", "article", "header", "footer", "nav"):
            inner = "".join(_render_node(c, list_depth) for c in node.children).strip()
            if not inner:
                return ""
            return f"\n\n{inner}\n\n"
        elif tag_name in ("ul", "ol"):
            items = []
            is_ol = tag_name == "ol"
            item_idx = 1
            indent = "  " * list_depth
            for child in node.children:
                if isinstance(child, Tag) and child.name.lower() == "li":
                    bullet = f"{item_idx}. " if is_ol else "• "
                    item_inner = "".join(_render_node(c, list_depth + 1) for c in child.children).strip()
                    if item_inner:
                        items.append(f"{indent}{bullet}{item_inner}")
                    item_idx += 1
            if not items:
                return ""
            return "\n" + "\n".join(items) + "\n"
        elif tag_name == "a":
            href = node.get("href", "").strip()
            inner = "".join(_render_node(c, list_depth) for c in node.children).strip()
            if href and href != inner and not href.startswith("javascript:"):
                if inner:
                    return f"{inner} ({href})"
                return href
            return inner
        elif tag_name == "img":
            alt = node.get("alt", "").strip()
            if alt:
                return f"[Image: {alt}]"
            return "[Image]"
        elif tag_name == "table":
            rows = []
            for tr in node.find_all("tr", recursive=True):
                cells = []
                for cell in tr.find_all(["th", "td"], recursive=False):
                    cell_text = "".join(_render_node(c, list_depth) for c in cell.children).strip()
                    cell_text = cell_text.replace("\n", " ")
                    cells.append(cell_text)
                if cells:
                    rows.append(" | ".join(cells))
            if not rows:
                return ""
            has_headers = bool(node.find("th"))
            table_lines = []
            if len(rows) > 0:
                table_lines.append(f"| {rows[0]} |")
                if has_headers:
                    header_sep = "| " + " | ".join(["---"] * len(rows[0].split(" | "))) + " |"
                    table_lines.append(header_sep)
                for r in rows[1:]:
                    table_lines.append(f"| {r} |")
            return "\n\n" + "\n".join(table_lines) + "\n\n"
        elif tag_name == "blockquote":
            inner = "".join(_render_node(c, list_depth) for c in node.children).strip()
            if not inner:
                return ""
            quoted_lines = [f"> {line}" if line.strip() else ">" for line in inner.split("\n")]
            return "\n\n" + "\n".join(quoted_lines) + "\n\n"
        elif tag_name == "pre":
            return f"\n\n{node.get_text()}\n\n"
        elif tag_name in ("b", "strong"):
            inner = "".join(_render_node(c, list_depth) for c in node.children).strip()
            return f"**{inner}**" if inner else ""
        elif tag_name in ("i", "em"):
            inner = "".join(_render_node(c, list_depth) for c in node.children).strip()
            return f"*{inner}*" if inner else ""
        elif tag_name == "code":
            inner = "".join(_render_node(c, list_depth) for c in node.children).strip()
            return f"`{inner}`" if inner else ""
        else:
            return "".join(_render_node(c, list_depth) for c in node.children)

    body = soup.body if soup.body else soup
    rendered = _render_node(body)

    rendered = re.sub(r"\n{3,}", "\n\n", rendered)
    lines = [line.rstrip() for line in rendered.split("\n")]
    return "\n".join(lines).strip()


def extract_statement(html: str) -> str:
    """Pull the raw weather statement out of the NOAA HTML page.

    The actual forecast text lives inside a <pre> tag (class
    'glossaryProduct') on the NOAA product page. Fall back to the
    first <pre> if the class isn't present, or process through the
    minimalist monochrome Symbian smartphone HTML engine.
    """
    soup = BeautifulSoup(html, "html.parser")
    pre = soup.find("pre", class_="glossaryProduct")
    if pre is None:
        pre = soup.find("pre")
    if pre is not None and pre.get_text().strip():
        return pre.get_text()
    
    return render_symbian_html(html)


def fetch_statement() -> dict:
    """GET the NOAA endpoint and return a statement dict.

    Returns a dict matching the statement.json schema:
        {"timestamp": str, "result": int, "raw": str}
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        resp = requests.get(STATEMENT_ENDPOINT, timeout=30)
        status = resp.status_code
        if status == 200:
            raw = extract_statement(resp.text)
        else:
            raw = resp.text[:500]
    except requests.RequestException as exc:
        status = 0
        raw = str(exc)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return {"timestamp": ts, "result": status, "raw": raw}


def save_statement(stmt: dict) -> None:
    """Persist statement dict to statement.json."""
    with open(STATEMENT_FILE, "w", encoding="utf-8") as fh:
        json.dump(stmt, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def ensure_statement_file() -> None:
    """Ensure statement.json exists and adheres to the correct schema."""
    default_stmt = {"timestamp": "", "result": 0, "raw": ""}
    if os.path.exists(STATEMENT_FILE):
        try:
            with open(STATEMENT_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                raise ValueError("Statement is not a JSON object")
            
            needs_save = False
            for key, default_val in default_stmt.items():
                if key not in data:
                    data[key] = default_val
                    needs_save = True
            
            if needs_save:
                save_statement(data)
            return
        except Exception:
            pass  # Corrupt or invalid JSON; recreate below

    save_statement(default_stmt)


def build_prompt(template: str, raw_statement: str) -> str:
    """Replace the {{STATEMENT}} and {{EXTRA_PROMPT}} placeholders."""
    if EXTRA_PROMPT:
        template = template.replace("{{EXTRA_PROMPT}}", EXTRA_PROMPT)
    else:
        template = template.replace("{{EXTRA_PROMPT}}\n\n", "").replace("{{EXTRA_PROMPT}}", "")
        
    return template.replace("{{STATEMENT}}", raw_statement)


_CITE_RE = re.compile(r"\s*\[?(\d+)\]?\s*", re.IGNORECASE)


def _bold_citations(text: str) -> str:
    """Bold DeepSeek-style ``N`` inline citation markers.

    Some models (DeepSeek in particular) emit inline citation refs like
    ``1.`` pointing at a source index.  Convert these into
    Markdown bold (``**[1]**``) so the existing ``**`` renderer in
    ``_draw`` / ``_draw_chat`` highlights them inline.
    """
    return _CITE_RE.sub(lambda m: f"**[{m.group(1)}]**", text)


def _clean_llm_response(raw: str) -> str:
    """Normalise the LLM response.

    Some models (e.g. Qwen) wrap the answer in a JSON object like:
        {"message": "…", "contexts": []}
    Detect that, extract the *message* field, and convert literal
    escape sequences (``\\n``) into real newlines so the TUI can
    render them properly.
    """
    text = raw.strip()

    # ── Try to parse as JSON and pull out "message" ──────────────────
    if text.startswith("{"):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict) and "message" in obj:
                text = obj["message"]
        except (json.JSONDecodeError, TypeError):
            pass  # not valid JSON — treat as plain text

    # ── Normalise literal escape sequences ───────────────────────────
    # Models sometimes emit the two-char sequence  \n  instead of a
    # real newline (U+000A).  Same for \t.
    text = text.replace("\\n", "\n")
    text = text.replace("\\t", "\t")

    # ── Bold DeepSeek-style N citation markers ───────────
    text = _bold_citations(text)

    return text


def _strip_think_block(text: str) -> str:
    """Remove ``<think>…</think>`` blocks from *text* for display.

    The block may span multiple lines.  If the model emits an
    opening ``<think>`` without a closing tag, everything from
    ``<think>`` onward is removed.
    """
    # re.DOTALL lets '.' match newlines so multi-line blocks are handled.
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    # Unclosed <think> — strip from the tag to the end of the string.
    cleaned = re.sub(r"<think>.*", "", cleaned, flags=re.DOTALL)
    # Collapse leading blank lines left over after removal.
    return cleaned.strip()


def query_llm(prompt: str, model_id: str | None = None) -> tuple[str, dict]:
    """Send the prompt to the OpenAI-compatible endpoint.

    Returns ``(reply_text, stats)`` where *stats* is a dict with keys
    ``tokens`` (completion token count), ``tok_per_sec``, and ``model``.
    """
    used_model = model_id or OPENAI_MODEL_ID
    client = OpenAI(base_url=OPENAI_ENDPOINT, api_key=OPENAI_API_KEY)
    t0 = time.monotonic()
    response = client.chat.completions.create(
        model=used_model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=1024,
    )
    elapsed = time.monotonic() - t0
    raw = response.choices[0].message.content.strip()

    # Extract token count from the response when available.
    tokens = 0
    if response.usage and response.usage.completion_tokens:
        tokens = response.usage.completion_tokens
    tok_per_sec = tokens / elapsed if elapsed > 0 and tokens else 0.0

    stats = {
        "tokens": tokens,
        "tok_per_sec": tok_per_sec,
        "elapsed": round(elapsed, 1),
        "model": used_model,
    }
    return _clean_llm_response(raw), stats


def query_llm_with_fallback(prompt: str) -> tuple[str, dict]:
    """Try the primary model, then each fallback in order.

    Returns ``(reply_text, stats)`` from the first model that succeeds.
    Raises the last exception if every model fails.
    """
    models_to_try = [OPENAI_MODEL_ID]
    if FALLBACK_MODEL_1:
        models_to_try.append(FALLBACK_MODEL_1)
    if FALLBACK_MODEL_2:
        models_to_try.append(FALLBACK_MODEL_2)

    last_exc: Exception | None = None
    for model_id in models_to_try:
        try:
            return query_llm(prompt, model_id=model_id)
        except Exception as exc:
            last_exc = exc

    # All models failed — re-raise the last exception.
    raise last_exc  # type: ignore[misc]


def query_llm_chat(messages: list[dict[str, str]], model_id: str | None = None) -> tuple[str, dict]:
    """Send the chat history to the OpenAI-compatible endpoint.

    Returns ``(reply_text, stats)``.
    """
    used_model = model_id or OPENAI_MODEL_ID
    client = OpenAI(base_url=OPENAI_ENDPOINT, api_key=OPENAI_API_KEY)
    t0 = time.monotonic()
    response = client.chat.completions.create(
        model=used_model,
        messages=messages,  # type: ignore[arg-type]
        temperature=0.3,
        max_tokens=1024,
    )
    elapsed = time.monotonic() - t0
    raw = response.choices[0].message.content.strip()

    # Extract token count from the response when available.
    tokens = 0
    if response.usage and response.usage.completion_tokens:
        tokens = response.usage.completion_tokens
    tok_per_sec = tokens / elapsed if elapsed > 0 and tokens else 0.0

    stats = {
        "tokens": tokens,
        "tok_per_sec": tok_per_sec,
        "elapsed": round(elapsed, 1),
        "model": used_model,
    }
    return _clean_llm_response(raw), stats


def query_llm_chat_with_fallback(messages: list[dict[str, str]]) -> tuple[str, dict]:
    """Try the primary model for chat, then each fallback in order.

    Returns ``(reply_text, stats)`` from the first model that succeeds.
    """
    models_to_try = [OPENAI_MODEL_ID]
    if FALLBACK_MODEL_1:
        models_to_try.append(FALLBACK_MODEL_1)
    if FALLBACK_MODEL_2:
        models_to_try.append(FALLBACK_MODEL_2)

    last_exc: Exception | None = None
    for model_id in models_to_try:
        try:
            return query_llm_chat(messages, model_id=model_id)
        except Exception as exc:
            last_exc = exc

        # All models failed — re-raise the last exception.
    raise last_exc  # type: ignore[misc]


def _build_chat_display(messages: list[dict[str, str]], width: int) -> list[tuple[str, int]]:
    """Build wrapped display lines with alignment and attributes.
    
    Returns list of (wrapped_line_text, alignment_flag) where:
      alignment_flag: 0 = left-aligned, 1 = right-aligned, 2 = header/meta
    """
    display_lines: list[tuple[str, int]] = []
    
    # We skip the system message and first user message (the raw weather statement).
    # We start display from the assistant's initial synopsis.
    for i, msg in enumerate(messages):
        if i < 2:
            continue
        
        role = msg["role"]
        content = msg["content"]
        
        if role == "assistant":
            display_lines.append(("### PARROT", 2))
            # Format and wrap assistant reply
            cleaned_reply = _strip_think_block(content)
            for paragraph in cleaned_reply.split("\n"):
                if paragraph.strip() == "":
                    display_lines.append(("", 0))
                else:
                    wrapped = textwrap.wrap(paragraph, width=width)
                    for wl in (wrapped or [""]):
                        display_lines.append((wl, 0))
            display_lines.append(("", 0))  # blank line separator
            
        elif role == "user":
            display_lines.append(("### USER", 2))
            for paragraph in content.split("\n"):
                if paragraph.strip() == "":
                    display_lines.append(("", 1))
                else:
                    wrapped = textwrap.wrap(paragraph, width=width)
                    for wl in (wrapped or [""]):
                        display_lines.append((wl, 1))
            display_lines.append(("", 1))  # blank line separator
            
    return display_lines


def _draw_chat(stdscr, display_lines: list[tuple[str, int]], scroll: int, input_buf: str,
               cursor_pos: int, is_thinking: bool, spinner_char: str, llm_stats: dict | None,
               max_x: int, max_y: int) -> int:
    stdscr.erase()
    
    PAIR_TITLE = curses.color_pair(1)
    PAIR_BAR = curses.color_pair(3)
    PAIR_STATS = curses.color_pair(5)
    PAIR_H1 = curses.color_pair(6)
    PAIR_H2 = curses.color_pair(7)
    PAIR_H3 = curses.color_pair(8)
    PAIR_USER_MSG = curses.color_pair(10)
    
    # ── Top bar ──────────────────────────────────────────────────────
    title_left = f" {APP_TITLE} — Follow-Up Chat "
    try:
        stdscr.addnstr(0, 0, title_left.ljust(max_x), max_x, PAIR_TITLE | curses.A_BOLD)
    except curses.error:
        pass

    # ── Bottom input bar & Help bar ──────────────────────────────────
    # The layout from bottom up:
    # max_y - 1: Help bar (black on cyan)
    # max_y - 2: Input line (> prompt)
    # max_y - 3: Stats bar / status
    
    # Help bar
    help_str = " Enter send  ESC exit  PageUp/Dn scroll "
    try:
        stdscr.addnstr(max_y - 1, 0, help_str.ljust(max_x), max_x, PAIR_BAR | curses.A_BOLD)
    except curses.error:
        pass  # writing to bottom-right corner can raise on some terminals
    
    # Input line with prompt
    prompt = "> "
    input_display = prompt + input_buf
    # Ensure it doesn't overflow max_x
    if len(input_display) >= max_x:
        input_display = input_display[-(max_x - 1):]
    try:
        stdscr.addnstr(max_y - 2, 0, input_display.ljust(max_x), max_x)
    except curses.error:
        pass
    
    # Stats / status bar
    stats_line = ""
    if is_thinking:
        stats_line = f"Thinking {spinner_char}..."
    elif llm_stats:
        parts = []
        if llm_stats.get("model"):
            parts.append(llm_stats["model"])
        if llm_stats.get("tokens"):
            parts.append(f"{llm_stats['tokens']} tokens")
        if llm_stats.get("tok_per_sec"):
            parts.append(f"{llm_stats['tok_per_sec']:.1f} tok/s")
        if llm_stats.get("elapsed"):
            parts.append(f"{llm_stats['elapsed']}s")
        stats_line = " · ".join(parts)
    
    try:
        stdscr.addnstr(max_y - 3, 0, stats_line.ljust(max_x), max_x, PAIR_STATS | curses.A_DIM)
    except curses.error:
        pass
    
    # ── Body (Scrollable Chat Area) ──────────────────────────────────
    body_height = max_y - 4  # leaving top bar (1), stats bar (1), input line (1), help bar (1)
    body_width = max_x - 2
    if body_width < 1:
        body_width = 1
        
    max_scroll = max(0, len(display_lines) - body_height)
    if scroll > max_scroll:
        scroll = max_scroll
        
    for i in range(body_height):
        line_idx = scroll + i
        if line_idx >= len(display_lines):
            break
            
        line_text, alignment = display_lines[line_idx]
        
        try:
            if alignment == 2:  # Headers
                if line_text == "### USER":
                    # Right aligned header
                    header_str = "### USER"
                    x_pos = max(0, max_x - len(header_str) - 1)
                    stdscr.addnstr(1 + i, x_pos, header_str, len(header_str), PAIR_H3 | curses.A_BOLD)
                else:
                    # Left aligned header
                    stdscr.addnstr(1 + i, 0, line_text, body_width, PAIR_H2 | curses.A_BOLD)
            elif alignment == 1:  # User messages
                x_pos = max(0, max_x - len(line_text) - 1)
                stdscr.addnstr(1 + i, x_pos, line_text, len(line_text), PAIR_USER_MSG)
            else:  # Assistant messages
                # Handle inline bolding ** text ** if any
                if "**" in line_text:
                    parts = line_text.split("**")
                    x = 0
                    for idx, part in enumerate(parts):
                        part_attr = curses.A_BOLD if idx % 2 == 1 else 0
                        if x >= body_width:
                            break
                        draw_len = min(len(part), body_width - x)
                        if draw_len > 0:
                            stdscr.addnstr(1 + i, x, part[:draw_len], draw_len, part_attr)
                            x += draw_len
                else:
                    stdscr.addnstr(1 + i, 0, line_text, body_width)
        except curses.error:
            pass
                
    # Place cursor at the end of prompt + current cursor position
    # The prompt is at max_y - 2, and starts at x=2 (len("> "))
    cursor_x = min(max_x - 1, 2 + cursor_pos)
    try:
        stdscr.move(max_y - 2, cursor_x)
    except curses.error:
        pass
    
    stdscr.refresh()
    return max_scroll


def _chat_mode(stdscr, system_prompt: str, raw_statement: str, initial_synopsis: str, initial_stats: dict) -> None:
    curses.curs_set(1)  # show cursor
    stdscr.nodelay(True)  # non-blocking input
    
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": raw_statement},
        {"role": "assistant", "content": initial_synopsis}
    ]
    
    input_buf = ""
    cursor_pos = 0
    scroll = 0
    
    # Spinner frame counter
    spinner_frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    spinner_idx = 0
    
    is_thinking = False
    thinking_thread: threading.Thread | None = None
    chat_reply = ""
    chat_stats: dict = initial_stats
    chat_error: str | None = None
    
    def async_chat_worker(msgs: list[dict[str, str]]):
        nonlocal chat_reply, chat_stats, chat_error, is_thinking
        try:
            chat_reply, chat_stats = query_llm_chat_with_fallback(msgs)
            chat_error = None
        except Exception as e:
            chat_reply = ""
            chat_error = str(e)
        finally:
            is_thinking = False
            
    # Initial build of display lines
    max_y, max_x = stdscr.getmaxyx()
    display_lines = _build_chat_display(messages, max(20, max_x - 4))
    scroll = max(0, len(display_lines) - (max_y - 4))
    
    while True:
        max_y, max_x = stdscr.getmaxyx()
        
        # Advance spinner if thinking
        spinner_char = spinner_frames[spinner_idx]
        
        # Draw screen
        max_scroll = _draw_chat(
            stdscr, display_lines, scroll, input_buf, cursor_pos,
            is_thinking, spinner_char, chat_stats, max_x, max_y
        )
        
        # Handle async query completion
        if not is_thinking and thinking_thread is not None:
            thinking_thread.join()
            thinking_thread = None
            if chat_error:
                messages.append({"role": "assistant", "content": f"Error: {chat_error}"})
            else:
                messages.append({"role": "assistant", "content": chat_reply})
            display_lines = _build_chat_display(messages, max(20, max_x - 4))
            scroll = max(0, len(display_lines) - (max_y - 4))
            
        # Standard loop delay (~50ms)
        time.sleep(0.05)
        if is_thinking:
            spinner_idx = (spinner_idx + 1) % len(spinner_frames)
            
        try:
            key = stdscr.getch()
        except curses.error:
            key = -1
            
        if key == -1:
            continue
            
        if key == 27:  # ESC key
            # Check if there are other keys queued (ESC sequence) or just ESC
            stdscr.nodelay(True)
            next_key = stdscr.getch()
            if next_key == -1:
                # User hit ESC -> Exit chat mode
                break
            continue
            
        if is_thinking:
            # Block inputs during thinking (or just scroll keys)
            if key == curses.KEY_UP:
                scroll = max(0, scroll - 1)
            elif key == curses.KEY_DOWN:
                scroll = min(max_scroll, scroll + 1)
            elif key == curses.KEY_PPAGE:
                scroll = max(0, scroll - (max_y - 4))
            elif key == curses.KEY_NPAGE:
                scroll = min(max_scroll, scroll + (max_y - 4))
            continue
            
        # Keyboard Input Handlers
        if key == curses.KEY_UP:
            scroll = max(0, scroll - 1)
        elif key == curses.KEY_DOWN:
            scroll = min(max_scroll, scroll + 1)
        elif key == curses.KEY_PPAGE:
            scroll = max(0, scroll - (max_y - 4))
        elif key == curses.KEY_NPAGE:
            scroll = min(max_scroll, scroll + (max_y - 4))
            
        elif key in (10, 13, curses.KEY_ENTER):  # Enter
            if input_buf.strip():
                # Append user message
                messages.append({"role": "user", "content": input_buf.strip()})
                input_buf = ""
                cursor_pos = 0
                
                # Rebuild display
                display_lines = _build_chat_display(messages, max(20, max_x - 4))
                scroll = max(0, len(display_lines) - (max_y - 4))
                
                # Trigger async query
                is_thinking = True
                thinking_thread = threading.Thread(target=async_chat_worker, args=(messages.copy(),))
                thinking_thread.start()
            else:
                # Sending an empty message hides the chat and returns
                # to the main synopsis view (mirrors the old ESC exit).
                break
                
        elif key in (127, 8, curses.KEY_BACKSPACE):  # Backspace
            if cursor_pos > 0:
                input_buf = input_buf[:cursor_pos - 1] + input_buf[cursor_pos:]
                cursor_pos -= 1
                
        elif key == curses.KEY_DC:  # Delete
            if cursor_pos < len(input_buf):
                input_buf = input_buf[:cursor_pos] + input_buf[cursor_pos + 1:]
                
        elif key == curses.KEY_LEFT:
            cursor_pos = max(0, cursor_pos - 1)
            
        elif key == curses.KEY_RIGHT:
            cursor_pos = min(len(input_buf), cursor_pos + 1)
            
        elif key == curses.KEY_HOME:
            cursor_pos = 0
            
        elif key == curses.KEY_END:
            cursor_pos = len(input_buf)
            
        elif key == curses.KEY_RESIZE:
            # Rebuild and adjust scroll
            display_lines = _build_chat_display(messages, max(20, max_x - 4))
            scroll = max(0, len(display_lines) - (max_y - 4))
            
        elif 32 <= key <= 126:  # Printable character
            input_buf = input_buf[:cursor_pos] + chr(key) + input_buf[cursor_pos:]
            cursor_pos += 1
            
    curses.curs_set(0)  # hide cursor again before returning


# ── Word-wrap utility ────────────────────────────────────────────────────────

def wrap_text(text: str, width: int) -> list[str]:
    """Word-wrap *text* to *width* columns, preserving existing newlines."""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if paragraph.strip() == "":
            lines.append("")
        else:
            lines.extend(textwrap.wrap(paragraph, width=width) or [""])
    return lines


# ── Curses TUI ───────────────────────────────────────────────────────────────

def _draw(stdscr, body_lines: list[str], scroll: int, interval_min: int,
          remaining_sec: int, status_code: int | None, last_ts: str,
          llm_stats: dict | None = None) -> int:
    """Redraw the entire screen.  Returns the max valid scroll offset."""
    stdscr.erase()
    max_y, max_x = stdscr.getmaxyx()
    if max_y < 5 or max_x < 20:
        stdscr.addnstr(0, 0, "Terminal too small", max_x)
        stdscr.refresh()
        return 0

    # ── Colour pairs ─────────────────────────────────────────────────
    # 1 = title bar (black on cyan), 2 = error (red on black),
    # 3 = bottom bar (black on cyan), 4 = status ok (green),
    # 5 = stats bar (dim white on black)
    PAIR_TITLE = curses.color_pair(1)
    PAIR_ERROR = curses.color_pair(2)
    PAIR_BAR = curses.color_pair(3)
    PAIR_OK = curses.color_pair(4)
    PAIR_STATS = curses.color_pair(5)
    PAIR_H1 = curses.color_pair(6)
    PAIR_H2 = curses.color_pair(7)
    PAIR_H3 = curses.color_pair(8)
    PAIR_STAR = curses.color_pair(9)

    # ── Top bar ──────────────────────────────────────────────────────
    title_left = f" {APP_TITLE} "
    title_right = f" {last_ts} " if last_ts else ""
    pad = max_x - len(title_left) - len(title_right)
    if pad < 0:
        pad = 0
    top_line = title_left + " " * pad + title_right
    stdscr.addnstr(0, 0, top_line.ljust(max_x), max_x, PAIR_TITLE | curses.A_BOLD)

    # ── Stats bar (second-to-last row) ────────────────────────────────
    stats_line = ""
    if llm_stats:
        parts = []
        if llm_stats.get("model"):
            parts.append(llm_stats["model"])
        elif OPENAI_MODEL_ID:
            parts.append(OPENAI_MODEL_ID)
        if llm_stats.get("tokens"):
            parts.append(f"{llm_stats['tokens']} tokens")
        if llm_stats.get("tok_per_sec"):
            parts.append(f"{llm_stats['tok_per_sec']:.1f} tok/s")
        if llm_stats.get("elapsed"):
            parts.append(f"{llm_stats['elapsed']}s")
        stats_line = " · ".join(parts)
    try:
        stdscr.addnstr(max_y - 2, 0, stats_line.ljust(max_x), max_x,
                       PAIR_STATS | curses.A_DIM)
    except curses.error:
        pass

    # ── Bottom bar ───────────────────────────────────────────────────
    mins, secs = divmod(remaining_sec, 60)
    hours, mins = divmod(mins, 60)
    if hours:
        timer_str = f" Refresh in {hours:d}:{mins:02d}:{secs:02d}"
    else:
        timer_str = f" Refresh in {mins:02d}:{secs:02d}"
    help_str = " ←/→ adjust  r refresh  f follow-up  q quit "
    bot_pad = max_x - len(timer_str) - len(help_str)
    if bot_pad < 0:
        bot_pad = 0
    bot_line = timer_str + " " * bot_pad + help_str
    try:
        stdscr.addnstr(max_y - 1, 0, bot_line.ljust(max_x), max_x, PAIR_BAR | curses.A_BOLD)
    except curses.error:
        pass  # writing to bottom-right corner can raise on some terminals

    # ── Body ─────────────────────────────────────────────────────────
    body_height = max_y - 3  # rows between top bar and stats+bottom bars
    body_width = max_x - 1
    if body_width < 1:
        body_width = 1

    max_scroll = max(0, len(body_lines) - body_height)
    if scroll > max_scroll:
        scroll = max_scroll

    is_error = status_code is not None and status_code != 200

    for i in range(body_height):
        line_idx = scroll + i
        if line_idx >= len(body_lines):
            break
        line = body_lines[line_idx]
        attr = PAIR_ERROR | curses.A_BOLD if is_error else 0
        
        if not is_error:
            stripped = line.strip()
            if stripped.startswith("###"):
                attr = PAIR_H3 | curses.A_BOLD
                idx = line.find("###")
                line = line[:idx] + line[idx+4:] if line.startswith("### ", idx) else line[:idx] + line[idx+3:]
            elif stripped.startswith("##"):
                attr = PAIR_H2 | curses.A_BOLD
                idx = line.find("##")
                line = line[:idx] + line[idx+3:] if line.startswith("## ", idx) else line[:idx] + line[idx+2:]
            elif stripped.startswith("#"):
                attr = PAIR_H1 | curses.A_BOLD
                idx = line.find("#")
                line = line[:idx] + line[idx+2:] if line.startswith("# ", idx) else line[:idx] + line[idx+1:]
            elif stripped.startswith("*") and not stripped.startswith("**"):
                idx = line.find("*")
                if idx != -1:
                    line = line[:idx] + "•" + line[idx+1:]

        try:
            if not is_error and "**" in line:
                parts = line.split("**")
                x = 0
                for idx, part in enumerate(parts):
                    part_attr = attr | curses.A_BOLD if idx % 2 == 1 else attr
                    if x >= body_width:
                        break
                    draw_len = min(len(part), body_width - x)
                    if draw_len > 0:
                        stdscr.addnstr(1 + i, x, part[:draw_len], draw_len, part_attr)
                        x += draw_len
            else:
                stdscr.addnstr(1 + i, 0, line, body_width, attr)
        except curses.error:
            pass

    stdscr.refresh()
    return max_scroll


def _run_cycle(template: str) -> tuple[list[str], int, str, dict, str]:
    """Execute one fetch → LLM cycle.

    Returns ``(lines, status, timestamp, llm_stats, raw_statement)``.
    """
    stmt = fetch_statement()
    save_statement(stmt)
    status = stmt["result"]
    ts = stmt["timestamp"]

    if status != 200:
        msg = f"HTTP {status}\n\n{stmt['raw']}" if status else f"Request failed\n\n{stmt['raw']}"
        return msg.split("\n"), status, ts, {}, stmt.get("raw", "")

    prompt = build_prompt(template, stmt["raw"])
    try:
        reply, stats = query_llm_with_fallback(prompt)
    except Exception as exc:
        return [f"LLM error: {exc}"], 0, ts, {}, stmt.get("raw", "")

    # Strip <think> blocks for display; token stats are already computed.
    display_reply = _strip_think_block(reply)
    return display_reply.split("\n"), status, ts, stats, stmt.get("raw", "")


def main(stdscr) -> None:
    """Curses main loop."""
    ensure_statement_file()
    interval_min = load_refresh_interval_minutes()
    persist_refresh_interval_minutes(interval_min)

    # ── Init curses ──────────────────────────────────────────────────
    curses.curs_set(0)
    stdscr.nodelay(False)
    stdscr.timeout(1000)  # 1-second tick

    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(2, curses.COLOR_RED, -1)
    curses.init_pair(3, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(4, curses.COLOR_GREEN, -1)
    curses.init_pair(5, curses.COLOR_WHITE, -1)
    curses.init_pair(6, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(7, curses.COLOR_WHITE, curses.COLOR_BLUE)
    curses.init_pair(8, curses.COLOR_BLACK, curses.COLOR_GREEN)
    curses.init_pair(9, curses.COLOR_YELLOW, -1)
    curses.init_pair(10, curses.COLOR_CYAN, -1)

    template = load_prompt_template()

    remaining_sec = 0  # triggers immediate fetch on first tick
    scroll = 0

    body_lines: list[str] = ["Fetching weather statement…"]
    status_code: int | None = None
    last_ts = ""
    llm_stats: dict = {}
    raw_statement = ""
    synopsis_text = ""
    needs_fetch = True  # first run

    while True:
        # ── Fetch if needed ──────────────────────────────────────────
        if needs_fetch:
            stdscr.erase()
            max_y, max_x = stdscr.getmaxyx()
            stdscr.addnstr(max_y // 2, max(0, max_x // 2 - 12), "Fetching weather data…", max_x)
            stdscr.refresh()

            raw_lines, status_code, last_ts, llm_stats, raw_statement = _run_cycle(template)
            synopsis_text = "\n".join(raw_lines)
            # Re-wrap to current terminal width
            _, max_x = stdscr.getmaxyx()
            body_lines = []
            for ln in raw_lines:
                body_lines.extend(wrap_text(ln, max(20, max_x - 2)))
            scroll = 0
            remaining_sec = interval_min * 60
            needs_fetch = False

        # ── Draw ─────────────────────────────────────────────────────
        max_scroll = _draw(stdscr, body_lines, scroll, interval_min,
                           remaining_sec, status_code, last_ts, llm_stats)

        # ── Input ────────────────────────────────────────────────────
        try:
            key = stdscr.getch()
        except curses.error:
            key = -1

        if key == ord("q") or key == ord("Q"):
            break
        elif key == curses.KEY_RIGHT:
            interval_min += INTERVAL_STEP_MINUTES
            remaining_sec += INTERVAL_STEP_MINUTES * 60
            persist_refresh_interval_minutes(interval_min)
        elif key == curses.KEY_LEFT:
            previous_interval = interval_min
            interval_min = max(MIN_INTERVAL_MINUTES, interval_min - INTERVAL_STEP_MINUTES)
            remaining_sec = min(remaining_sec, interval_min * 60)
            if interval_min != previous_interval:
                persist_refresh_interval_minutes(interval_min)
        elif key == curses.KEY_UP:
            scroll = max(0, scroll - 1)
        elif key == curses.KEY_DOWN:
            scroll = min(max_scroll, scroll + 1)
        elif key == ord("r") or key == ord("R"):
            needs_fetch = True
            continue
        elif (key == ord("f") or key == ord("F")) and status_code == 200:
            _chat_mode(stdscr, template, raw_statement, synopsis_text, llm_stats)
            # Restore main-loop curses settings after returning from chat mode.
            stdscr.nodelay(False)
            stdscr.timeout(1000)
            scroll = 0  # Scroll back to the top of the synopsis
            continue
        elif key == curses.KEY_PPAGE:  # Page Up
            scroll = max(0, scroll - (curses.LINES - 3))
        elif key == curses.KEY_NPAGE:  # Page Down
            scroll = min(max_scroll, scroll + (curses.LINES - 3))
        elif key == curses.KEY_RESIZE:
            # Terminal resized — re-wrap body text
            _, max_x = stdscr.getmaxyx()
            raw_text = "\n".join(body_lines)
            body_lines = wrap_text(raw_text, max(20, max_x - 2))
            scroll = 0

        # ── Countdown ────────────────────────────────────────────────
        if key == -1:
            # timeout fired (~1 sec elapsed)
            remaining_sec -= 1
            if remaining_sec <= 0:
                needs_fetch = True


if __name__ == "__main__":
    curses.wrapper(main)
