"""OneNote HTML <-> text. Standard library only: no beautifulsoup4, no lxml.

Reading: Graph returns a full HTML document whose body is a pile of absolutely
positioned <div>s with inline styles, <img> tags pointing at authenticated
resource URLs, and <object> attachments. We want the words.

Writing: Graph creates a page from an HTML document, taking the page title from
<title> in <head> -- not from any POST parameter.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

# Part 1 ids are exactly 16 hex characters.
MARKER_RE = re.compile(r"job-tracker-id:\s*([0-9a-f]{16})")
MARKER_PREFIX = "job-tracker-id:"

_DROP = {"script", "style", "head", "title", "meta", "link"}
_BREAK = {"p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "li", "ul", "ol", "table"}

_WS_IN_LINE = re.compile(r"[ \t]+")
# Every block tag emits a newline on both open and close, and OneNote nests them
# deeply, so blank runs are an artifact of the markup rather than of the note.
# Line structure is the data; blank-line structure is not.
_BLANK_RUN = re.compile(r"\n{2,}")


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._skip_depth = 0
        self._cells: list[str] | None = None   # accumulating a <tr>
        self._cell: list[str] | None = None

    # -- helpers
    def _emit(self, s: str) -> None:
        if self._cell is not None:
            self._cell.append(s)
        else:
            self._out.append(s)

    def _newline(self) -> None:
        if self._cell is None:
            self._out.append("\n")

    def handle_starttag(self, tag, attrs):
        if tag in _DROP:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        a = dict(attrs)

        if tag == "img":
            # NEVER fetch the src. It is an authenticated Graph URL to a binary
            # blob; downloading it is out of scope and burns throttle budget.
            self._emit("[image]")
            return
        if tag == "object":
            name = a.get("data-attachment") or a.get("name") or "file"
            self._emit(f"[attachment: {name}]")
            return
        if tag == "tr":
            self._cells = []
            return
        if tag in ("td", "th"):
            self._cell = []
            return
        if tag == "li":
            self._newline()
            self._emit("- ")
            return
        if tag in _BREAK:
            self._newline()

    def handle_endtag(self, tag):
        if tag in _DROP:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag in ("td", "th"):
            if self._cell is not None and self._cells is not None:
                self._cells.append("".join(self._cell).strip())
            self._cell = None
            return
        if tag == "tr":
            if self._cells:
                # Application logs are tables far more often than prose, and the
                # column alignment is the signal. Keep it.
                self._out.append("\n" + " | ".join(self._cells))
            self._cells = None
            return
        if tag in _BREAK:
            self._newline()

    def handle_data(self, data):
        if self._skip_depth:
            return
        self._emit(data)

    def text(self) -> str:
        raw = "".join(self._out)
        lines = [_WS_IN_LINE.sub(" ", ln).strip() for ln in raw.split("\n")]
        return _BLANK_RUN.sub("\n", "\n".join(lines)).strip()


def html_to_text(doc: str) -> str:
    """Extract readable text. Never performs I/O."""
    p = _TextExtractor()
    p.feed(doc or "")
    p.close()
    return p.text()


def build_page_html(title: str, body_lines: list[str], created: str | None = None,
                    marker: str | None = None) -> str:
    """Build the document Graph turns into a page.

    Every interpolated value is escaped. body_lines originate in job-alert
    emails, which are attacker-influenced text; this is Part 2's injection
    boundary and there is exactly one of it.
    """
    esc_title = html.escape(title or "", quote=True)
    parts = [
        "<!DOCTYPE html>", "<html>", "  <head>",
        f"    <title>{esc_title}</title>",
    ]
    if created:
        parts.append(f'    <meta name="created" content="{html.escape(created, quote=True)}" />')
    parts += ["  </head>", "  <body>"]

    for line in body_lines or []:
        parts.append(f"    <p>{html.escape(str(line), quote=True)}</p>")

    if marker:
        # Visible body text, deliberately NOT a <meta> tag or data- attribute:
        # OneNote's sanitizer makes no round-trip guarantee for those, and a
        # marker that silently vanishes turns idempotency into duplicate pages.
        parts.append(
            f"    <p><i>{MARKER_PREFIX} {html.escape(marker, quote=True)}</i></p>"
        )

    parts += ["  </body>", "</html>"]
    return "\n".join(parts)


def find_marker(text: str) -> str | None:
    """The job id this tool stamped into a page it created, if any."""
    m = MARKER_RE.search(text or "")
    return m.group(1) if m else None
