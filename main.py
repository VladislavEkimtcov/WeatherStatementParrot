#!/usr/bin/env python3
"""WeatherStatementParrot — full-terminal TUI for NOAA weather analysis."""

from __future__ import annotations

import curses
import json
import os
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
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
EXTRA_PROMPT = os.getenv("EXTRA_PROMPT", "")

STATEMENT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "statement.json")
PROMPT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "PROCESS_PROMPT.md")

DEFAULT_INTERVAL_MINUTES = 60
INTERVAL_STEP_MINUTES = 15
MIN_INTERVAL_MINUTES = 15

APP_TITLE = "WeatherStatementParrot"


# ── Helpers ──────────────────────────────────────────────────────────────────

def load_prompt_template() -> str:
    """Read the PROCESS_PROMPT.md template from disk."""
    with open(PROMPT_FILE, "r", encoding="utf-8") as fh:
        return fh.read()


def extract_statement(html: str) -> str:
    """Pull the raw weather statement out of the NOAA HTML page.

    The actual forecast text lives inside a <pre> tag (class
    'glossaryProduct') on the NOAA product page.  Fall back to the
    first <pre> if the class isn't present, or to the full body text.
    """
    soup = BeautifulSoup(html, "html.parser")
    pre = soup.find("pre", class_="glossaryProduct")
    if pre is None:
        pre = soup.find("pre")
    if pre is not None:
        return pre.get_text()
    # Absolute fallback — strip all tags.
    return soup.get_text()


def fetch_statement() -> dict:
    """GET the NOAA endpoint and return a statement dict.

    Returns a dict matching the statement.json schema:
        {"timestamp": str, "result": int, "raw": str}
    """
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
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
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

    return {"timestamp": ts, "result": status, "raw": raw}


def save_statement(stmt: dict) -> None:
    """Persist statement dict to statement.json."""
    with open(STATEMENT_FILE, "w", encoding="utf-8") as fh:
        json.dump(stmt, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def build_prompt(template: str, raw_statement: str) -> str:
    """Replace the {{STATEMENT}} placeholder with actual weather text.

    If EXTRA_PROMPT is set, it is injected right before the
    ``--- / WEATHER STATEMENT:`` divider so the LLM sees the extra
    instruction in context before the raw data.
    """
    if EXTRA_PROMPT:
        # Insert the extra prompt right before the horizontal rule that
        # precedes the WEATHER STATEMENT block.
        divider = "\n---\n\nWEATHER STATEMENT:"
        if divider in template:
            template = template.replace(
                divider,
                f"\n{EXTRA_PROMPT}\n{divider}",
            )
    return template.replace("{{STATEMENT}}", raw_statement)


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

    return text


def query_llm(prompt: str) -> tuple[str, dict]:
    """Send the prompt to the OpenAI-compatible endpoint.

    Returns ``(reply_text, stats)`` where *stats* is a dict with keys
    ``tokens`` (completion token count) and ``tok_per_sec``.
    """
    client = OpenAI(base_url=OPENAI_ENDPOINT, api_key=OPENAI_API_KEY)
    t0 = time.monotonic()
    response = client.chat.completions.create(
        model=OPENAI_MODEL_ID,
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

    stats = {"tokens": tokens, "tok_per_sec": tok_per_sec, "elapsed": round(elapsed, 1)}
    return _clean_llm_response(raw), stats


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
        if OPENAI_MODEL_ID:
            parts.append(OPENAI_MODEL_ID)
        if llm_stats.get("tokens"):
            parts.append(f"{llm_stats['tokens']} tokens")
        if llm_stats.get("tok_per_sec"):
            parts.append(f"{llm_stats['tok_per_sec']:.1f} tok/s")
        if llm_stats.get("elapsed"):
            parts.append(f"{llm_stats['elapsed']}s")
        stats_line = " " + "  ·  ".join(parts)
    try:
        stdscr.addnstr(max_y - 2, 0, stats_line.ljust(max_x), max_x,
                       PAIR_STATS | curses.A_DIM)
    except curses.error:
        pass

    # ── Bottom bar ───────────────────────────────────────────────────
    mins, secs = divmod(remaining_sec, 60)
    hours, mins = divmod(mins, 60)
    if hours:
        timer_str = f" Next refresh in {hours:d}:{mins:02d}:{secs:02d}"
    else:
        timer_str = f" Next refresh in {mins:02d}:{secs:02d}"
    help_str = "↑/↓ interval  r refresh  q quit "
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
        # Highlight **bold** markdown markers with A_BOLD
        if not is_error and line.strip().startswith("**"):
            attr = PAIR_OK | curses.A_BOLD
        try:
            stdscr.addnstr(1 + i, 0, line, body_width, attr)
        except curses.error:
            pass

    stdscr.refresh()
    return max_scroll


def _run_cycle(template: str) -> tuple[list[str], int, str, dict]:
    """Execute one fetch → LLM cycle.

    Returns ``(lines, status, timestamp, llm_stats)``.
    """
    stmt = fetch_statement()
    save_statement(stmt)
    status = stmt["result"]
    ts = stmt["timestamp"]

    if status != 200:
        msg = f"HTTP {status}\n\n{stmt['raw']}" if status else f"Request failed\n\n{stmt['raw']}"
        return msg.split("\n"), status, ts, {}

    prompt = build_prompt(template, stmt["raw"])
    try:
        reply, stats = query_llm(prompt)
    except Exception as exc:
        return [f"LLM error: {exc}"], 0, ts, {}

    return reply.split("\n"), status, ts, stats


def main(stdscr) -> None:
    """Curses main loop."""
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

    template = load_prompt_template()

    interval_min = DEFAULT_INTERVAL_MINUTES
    remaining_sec = 0  # triggers immediate fetch on first tick
    scroll = 0

    body_lines: list[str] = ["Fetching weather statement…"]
    status_code: int | None = None
    last_ts = ""
    llm_stats: dict = {}
    needs_fetch = True  # first run

    while True:
        # ── Fetch if needed ──────────────────────────────────────────
        if needs_fetch:
            stdscr.erase()
            max_y, max_x = stdscr.getmaxyx()
            stdscr.addnstr(max_y // 2, max(0, max_x // 2 - 12), "Fetching weather data…", max_x)
            stdscr.refresh()

            raw_lines, status_code, last_ts, llm_stats = _run_cycle(template)
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
        elif key == curses.KEY_UP:
            interval_min += INTERVAL_STEP_MINUTES
            remaining_sec += INTERVAL_STEP_MINUTES * 60
        elif key == curses.KEY_DOWN:
            interval_min = max(MIN_INTERVAL_MINUTES, interval_min - INTERVAL_STEP_MINUTES)
            remaining_sec = min(remaining_sec, interval_min * 60)
        elif key == ord("r") or key == ord("R"):
            needs_fetch = True
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
