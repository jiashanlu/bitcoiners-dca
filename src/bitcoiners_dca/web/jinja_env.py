"""
Jinja2 environment wired to the dashboard templates directory.

Centralized here so tests can monkey-patch the loader if needed and so the
dashboard handler functions stay focused on routing logic.
"""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATE_DIR = Path(__file__).parent / "templates"


def make_jinja() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )

    def fmt_money(value, decimals: int = 2) -> str:
        try:
            return f"{float(value):,.{decimals}f}"
        except (ValueError, TypeError):
            return "—"

    def fmt_btc(value) -> str:
        try:
            return f"{float(value):.8f}"
        except (ValueError, TypeError):
            return "—"

    def fmt_pct(value, decimals: int = 2) -> str:
        try:
            return f"{float(value):+.{decimals}f}%"
        except (ValueError, TypeError):
            return "—"

    env.filters["money"] = fmt_money
    env.filters["btc"] = fmt_btc
    env.filters["pct"] = fmt_pct
    return env
