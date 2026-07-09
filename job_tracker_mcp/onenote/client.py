"""OneNoteClient: resolve, list, read, create. Speaks Graph, knows no SQL."""

from __future__ import annotations

import logging

from job_tracker_mcp.models import normalize_key_part
from job_tracker_mcp.onenote.graph import GraphClient, GraphError
from job_tracker_mcp.onenote.htmlio import build_page_html, html_to_text

log = logging.getLogger(__name__)

PAGE_SELECT = "id,title,createdDateTime,lastModifiedDateTime,links"
MAX_SECTION_GROUP_DEPTH = 3


class Ambiguous(GraphError):
    """Two things normalize to the same name. Ask, never guess."""


class NotFound(GraphError):
    pass


def _page_row(p: dict) -> dict:
    links = p.get("links") or {}
    return {
        "page_id": p["id"],
        "title": p.get("title") or "",
        "created_dt": p.get("createdDateTime") or "",
        "last_modified_dt": p.get("lastModifiedDateTime") or "",
        "web_url": (links.get("oneNoteWebUrl") or {}).get("href"),
        "client_url": (links.get("oneNoteClientUrl") or {}).get("href"),
    }


class OneNoteClient:
    def __init__(self, graph: GraphClient):
        self.g = graph

    # ------------------------------------------------------------- resolve

    def notebooks(self) -> list[dict]:
        return list(self.g.get_all("/me/onenote/notebooks",
                                   {"$select": "id,displayName,isDefault"}))

    def sections_of(self, notebook_id: str) -> list[dict]:
        """Top-level sections, then section groups to a bounded depth."""
        out = [
            {**s, "path": [s.get("displayName", "")]}
            for s in self.g.get_all(f"/me/onenote/notebooks/{notebook_id}/sections",
                                    {"$select": "id,displayName"})
        ]
        groups = list(self.g.get_all(f"/me/onenote/notebooks/{notebook_id}/sectionGroups",
                                     {"$select": "id,displayName"}))
        for grp in groups:
            out.extend(self._sections_in_group(grp, [grp.get("displayName", "")], 1))
        return out

    def _sections_in_group(self, group: dict, prefix: list[str], depth: int) -> list[dict]:
        if depth > MAX_SECTION_GROUP_DEPTH:
            log.warning("section group nesting exceeded depth %d; stopping",
                        MAX_SECTION_GROUP_DEPTH)
            return []
        gid = group["id"]
        out = [
            {**s, "path": [*prefix, s.get("displayName", "")]}
            for s in self.g.get_all(f"/me/onenote/sectionGroups/{gid}/sections",
                                    {"$select": "id,displayName"})
        ]
        for child in self.g.get_all(f"/me/onenote/sectionGroups/{gid}/sectionGroups",
                                    {"$select": "id,displayName"}):
            out.extend(self._sections_in_group(
                child, [*prefix, child.get("displayName", "")], depth + 1))
        return out

    @staticmethod
    def _pick(items: list[dict], wanted: str, kind: str) -> dict:
        """Match on the normalized displayName, in Python.

        Never $filter: OneNote's OData support is uneven, and the notebook name
        contains an apostrophe, which an OData string literal must escape by
        doubling. That is a bug waiting in ambush.
        """
        target = normalize_key_part(wanted)
        hits = [i for i in items if normalize_key_part(i.get("displayName")) == target]
        if not hits:
            names = ", ".join(sorted(repr(i.get("displayName")) for i in items)) or "none"
            raise NotFound(f"No {kind} named {wanted!r}. Available: {names}")
        if len(hits) > 1:
            ids = ", ".join(h["id"] for h in hits)
            raise Ambiguous(
                f"Ambiguous: {len(hits)} {kind}s named {wanted!r} (ids: {ids}). "
                f"Set JOB_TRACKER_ONENOTE_SECTION_ID to disambiguate."
            )
        return hits[0]

    def resolve_section(self, notebook_name: str, section_name: str) -> dict:
        nb = self._pick(self.notebooks(), notebook_name, "notebook")
        sec = self._pick(self.sections_of(nb["id"]), section_name, "section")
        return {
            "notebook_id": nb["id"], "notebook_name": nb.get("displayName"),
            "section_id": sec["id"], "section_name": sec.get("displayName"),
        }

    def validate_section(self, section_id: str) -> dict:
        return self.g.get_json(f"/me/onenote/sections/{section_id}",
                               {"$select": "id,displayName"})

    # ---------------------------------------------------------------- read

    def list_pages(self, section_id: str, max_pages: int = 200) -> tuple[list[dict], bool]:
        """Returns (pages, truncated). $top=100 is the documented maximum."""
        rows, truncated = [], False
        for p in self.g.get_all(f"/me/onenote/sections/{section_id}/pages",
                                {"$top": 100, "$select": PAGE_SELECT}):
            if len(rows) >= max_pages:
                truncated = True
                break
            rows.append(_page_row(p))
        return rows, truncated

    def page_content(self, page_id: str) -> tuple[str, str]:
        html = self.g.get_text(f"/me/onenote/pages/{page_id}/content")
        return html, html_to_text(html)

    # --------------------------------------------------------------- write

    def create_page(self, section_id: str, title: str, body_lines: list[str],
                    created: str | None = None, marker: str | None = None) -> dict:
        """POST to the resolved section id. NEVER ?sectionName=, which silently
        creates a second section on a typo or a rename."""
        html = build_page_html(title, body_lines, created=created, marker=marker)
        created_page = self.g.post_html(f"/me/onenote/sections/{section_id}/pages", html)
        row = _page_row(created_page)
        row["content_html"] = html
        row["content_text"] = html_to_text(html)
        return row
