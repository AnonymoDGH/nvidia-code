"""Helpers para renderizar salida Markdown en terminal usando Rich con fallback."""

from __future__ import annotations

from typing import Optional

try:
    from rich.console import Console
    from rich.markdown import Markdown
    HAS_RICH = True
except Exception:
    HAS_RICH = False

from .markdown import render_markdown

_console: Optional["Console"] = Console() if HAS_RICH else None


def print_markdown(text: str) -> None:
    """Imprime markdown con Rich; si no está disponible, usa el renderizador existente."""
    if HAS_RICH and _console is not None:
        _console.print(Markdown(text, code_theme="monokai", hyperlinks=True))
        return

    try:
        print(render_markdown(text))
    except Exception:
        print(text)
