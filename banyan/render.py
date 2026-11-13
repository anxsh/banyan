"""Single-page interactive HTML viewer renderer."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from banyan.model import DEFAULT_TITLE, normalize_tree

_TEMPLATE_PATH = Path(__file__).with_name("viewer.html")


def render_html(
    data: dict[str, Any],
    out_path: str | Path,
    title: str | None = None,
    json_url: str = "tree.json",
    embed: bool = False,
) -> None:
    """Render `viewer.html` wired to fetch sibling `json_url` (or embed JSON inline)."""
    tree = normalize_tree(data)
    if title:
        tree["meta"]["title"] = title
    page_title = html.escape(tree["meta"].get("title", DEFAULT_TITLE))
    json_url_literal = json.dumps(json_url, ensure_ascii=False)
    embedded_literal = (
        json.dumps(tree, ensure_ascii=False).replace("</", "<\\/")
        if embed
        else "null"
    )
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    content = (
        template.replace("__PAGE_TITLE__", page_title)
        .replace("__JSON_URL__", json_url_literal)
        .replace("__EMBEDDED_JSON__", embedded_literal)
    )
    Path(out_path).write_text(content, encoding="utf-8")
