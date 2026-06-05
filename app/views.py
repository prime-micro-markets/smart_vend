import json
import re
from pathlib import Path

from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape


def _chat_format(text: str) -> Markup:
    """Convert AI chat response to safe HTML: linkify URLs, bold **…**, preserve newlines."""
    escaped = str(escape(text))
    # Bold: **text**
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    # Linkify bare URLs (http/https) — skip ones already inside an href
    escaped = re.sub(
        r'(?<!=")https?://[^\s<>"\']+',
        lambda m: (
            f'<a href="{m.group()}" target="_blank" rel="noopener noreferrer">{m.group()}</a>'
        ),
        escaped,
    )
    # Newlines → <br>
    escaped = escaped.replace("\n", "<br>")
    return Markup(escaped)


templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["fromjson"] = json.loads
templates.env.filters["chat_format"] = _chat_format
