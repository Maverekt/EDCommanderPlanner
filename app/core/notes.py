"""Commander notes: one Markdown file per note under data/notes/<fid>/ (DESIGN §13.1).

The folder is read on every request, so a note edited in a text editor shows on the next load.
Deleting moves the file to deleted/ beside it rather than erasing it.
"""

from __future__ import annotations

import html
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from markupsafe import Markup

ID_RE = re.compile(r"^\d{8}-\d{6}(-\d+)?$")
FID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
FIELDS = ("title", "pinned", "created", "updated")
MAX_TITLE = 200


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def notes_dir(data_dir: Path, fid: str) -> Path:
    if not FID_RE.match(fid or ""):
        raise ValueError("bad commander id")
    return data_dir / "notes" / fid


def _path(data_dir: Path, fid: str, note_id: str) -> Path:
    if not ID_RE.match(note_id or ""):
        raise LookupError(note_id)
    return notes_dir(data_dir, fid) / f"{note_id}.md"


def _clean_title(title) -> str:
    return " ".join(str(title or "").split())[:MAX_TITLE] or "Untitled note"


def parse(text: str) -> tuple[dict, str]:
    """(front matter, body). A file without front matter is all body."""
    text = text.replace("\r\n", "\n")
    meta: dict = {}
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            for line in text[4:end].split("\n"):
                key, sep, value = line.partition(":")
                if sep and key.strip() in FIELDS:
                    meta[key.strip()] = value.strip()
            text = text[end + 5:]
    meta["pinned"] = str(meta.get("pinned", "")).lower() == "true"
    return meta, text


def dump(meta: dict, body: str) -> str:
    head = [f"title: {_clean_title(meta.get('title'))}", f"pinned: {'true' if meta.get('pinned') else 'false'}",
            f"created: {meta.get('created') or _now()}", f"updated: {meta.get('updated') or _now()}"]
    return "---\n" + "\n".join(head) + "\n---\n" + body.replace("\r\n", "\n")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _read(path: Path) -> dict:
    meta, body = parse(path.read_text(encoding="utf-8"))
    stamp = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"id": path.stem, "title": _clean_title(meta.get("title")), "pinned": meta["pinned"],
            "created": meta.get("created") or stamp, "updated": meta.get("updated") or stamp, "body": body}


def list_notes(data_dir: Path, fid: str, query: str = "") -> list[dict]:
    """Pinned first, then newest update first. A query matches title or body, case-insensitively."""
    folder = notes_dir(data_dir, fid)
    if not folder.is_dir():
        return []
    notes = [_read(p) for p in folder.glob("*.md") if ID_RE.match(p.stem)]
    words = query.lower().split()
    if words:
        notes = [n for n in notes if all(w in (n["title"] + "\n" + n["body"]).lower() for w in words)]
    notes.sort(key=lambda n: n["updated"], reverse=True)
    notes.sort(key=lambda n: not n["pinned"])
    return notes


def get(data_dir: Path, fid: str, note_id: str) -> dict:
    path = _path(data_dir, fid, note_id)
    if not path.is_file():
        raise LookupError(note_id)
    return _read(path)


def new_id(data_dir: Path, fid: str) -> str:
    folder = notes_dir(data_dir, fid)
    base = datetime.now().strftime("%Y%m%d-%H%M%S")
    note_id, n = base, 1
    while (folder / f"{note_id}.md").exists():
        n += 1
        note_id = f"{base}-{n}"
    return note_id


def create(data_dir: Path, fid: str, title: str = "", body: str = "", pinned: bool = False) -> str:
    note_id = new_id(data_dir, fid)
    now = _now()
    _write(_path(data_dir, fid, note_id), dump({"title": title, "pinned": pinned, "created": now, "updated": now}, body))
    return note_id


def raw(data_dir: Path, fid: str) -> dict[str, str]:
    """Every live note's file text by id, as stored (backups, §12)."""
    folder = notes_dir(data_dir, fid)
    if not folder.is_dir():
        return {}
    return {p.stem: p.read_text(encoding="utf-8").replace("\r\n", "\n")
            for p in sorted(folder.glob("*.md")) if ID_RE.match(p.stem)}


def write_raw(data_dir: Path, fid: str, note_id: str, text: str) -> None:
    """Write a note's file text as given; an existing note of that id is moved to deleted/ first."""
    path = _path(data_dir, fid, note_id)
    if path.is_file():
        delete(data_dir, fid, note_id)
    _write(path, text.replace("\x00", ""))


def update(data_dir: Path, fid: str, note_id: str, changes: dict) -> dict:
    note = get(data_dir, fid, note_id)
    for key in ("title", "body"):
        if key in changes and changes[key] is not None:
            note[key] = str(changes[key])
    if "pinned" in changes:
        note["pinned"] = changes["pinned"] in (True, "true", "on", "1", 1)
    note["title"] = _clean_title(note["title"])
    note["updated"] = _now()
    _write(_path(data_dir, fid, note_id), dump(note, note["body"]))
    return note


def delete(data_dir: Path, fid: str, note_id: str) -> None:
    path = _path(data_dir, fid, note_id)
    if not path.is_file():
        raise LookupError(note_id)
    bin_dir = path.parent / "deleted"
    bin_dir.mkdir(exist_ok=True)
    target, n = bin_dir / path.name, 1
    while target.exists():                     # an earlier copy of the same note stays too
        n += 1
        target = bin_dir / f"{path.stem}.{n}.md"
    os.replace(path, target)


# -- Markdown: a safe subset; everything is escaped first, so no HTML from a note gets through ----

_CODE = re.compile(r"(`[^`\n]+`)")
# A link target may leave out https:// (example.com/page, www.example.com) and may hold one level
# of brackets, as wiki addresses do. Only http(s) links are ever made.
_TARGET = r"[^\s()]+(?:\([^\s()]*\)[^\s()]*)*"
_LINK = re.compile(r"!?\[([^\]\n]+)\]\(\s*(" + _TARGET + r")\s*\)")
_BARE = re.compile(r"(?<![\"'=>/\w])((?:https?://|www\.)[^\s<]*[^\s<.,;:!?)])")
_DOMAIN = re.compile(r"^(?:www\.)?[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?:[/?#]|$)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_STRIKE = re.compile(r"~~(.+?)~~")
_ITALIC = re.compile(r"(?<![\w*])[*_](?![\s*_])(.+?)(?<![\s*_])[*_](?![\w*])")
_ITEM = re.compile(r"^(\s*)([-*]|\d+[.)])\s+(.*)$")
_TASK = re.compile(r"^\[( |x|X)\]\s+(.*)$")


def link_target(url: str) -> str | None:
    """The href for a link written in a note: http(s) as given, a bare domain as https, anything else none."""
    url = html.unescape(url)
    if re.match(r"(?i)https?://", url):
        return url
    if _DOMAIN.match(url):
        return "https://" + url
    return None


def _inline(text: str) -> str:
    out = []
    for part in _CODE.split(text):
        if part.startswith("`") and part.endswith("`") and len(part) > 1:
            out.append(f"<code>{html.escape(part[1:-1])}</code>")
            continue
        links: list[str] = []

        def keep(m, text_group, url_group):
            # Finished links are set aside so emphasis never touches their attributes or URLs.
            href = link_target(m.group(url_group))
            if href is None:
                return m.group(0)
            links.append(f'<a href="{html.escape(href)}" target="_blank" rel="noopener noreferrer">'
                         f"{m.group(text_group)}</a>")
            return f"\x00{len(links) - 1}\x00"

        s = html.escape(part.replace("\x00", ""))
        s = _LINK.sub(lambda m: keep(m, 1, 2), s)
        s = _BARE.sub(lambda m: keep(m, 1, 1), s)
        s = _BOLD.sub(r"<strong>\1</strong>", s)
        s = _STRIKE.sub(r"<del>\1</del>", s)
        s = _ITALIC.sub(r"<em>\1</em>", s)
        out.append(re.sub(r"\x00(\d+)\x00", lambda m: links[int(m.group(1))], s))
    return "".join(out)


def render(body: str, tasks: bool = False) -> Markup:
    """The body as HTML. With `tasks`, to-do boxes can be ticked: each carries its index (`toggle_task`).

    Lists nest by indentation, and a numbered list and a bulleted one side by side stay separate.
    """
    lines = body.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    para: list[str] = []
    quote: list[str] = []
    lists: list[tuple[str, int]] = []          # open lists, outermost first: (tag, indent)
    task_no = 0

    def close_lists(indent: int = -1):
        while lists and lists[-1][1] > indent:
            out.append(f"</li></{lists.pop()[0]}>")

    def flush_text():
        if para:
            out.append("<p>" + "<br>".join(_inline(x) for x in para) + "</p>")
            para.clear()
        if quote:
            out.append("<blockquote>" + "<br>".join(_inline(x) for x in quote) + "</blockquote>")
            quote.clear()

    def flush():
        flush_text()
        close_lists()

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            flush()
            code = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            out.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
        elif not stripped:
            flush_text()
            following = next((x for x in lines[i + 1:] if x.strip()), "")
            if not _ITEM.match(following):        # a blank line between items keeps one list
                close_lists()
        elif m := re.match(r"^(#{1,6})\s+(.*)$", stripped):
            flush()
            level = min(len(m.group(1)) + 2, 6)               # # → h3: the page owns h1 and h2
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
        elif stripped.startswith(">"):
            if para or lists:
                flush()
            quote.append(stripped.lstrip(">").strip())
        elif m := _ITEM.match(line):
            flush_text()
            indent = len(m.group(1).expandtabs(4))
            tag = "ul" if m.group(2) in "-*" else "ol"
            close_lists(indent)
            if lists and lists[-1][1] == indent and lists[-1][0] != tag:
                out.append(f"</li></{lists.pop()[0]}>")         # a bulleted list after a numbered one
            if lists and lists[-1][1] == indent:
                out.append("</li>")
            else:
                out.append(f"<{tag}>")
                lists.append((tag, indent))
            text = m.group(3)
            if t := _TASK.match(text):
                done = t.group(1).lower() == "x"
                box = f'data-task="{task_no}"' if tasks else "disabled"
                out.append(f'<li class="task{" done" if done else ""}"><input type="checkbox" {box}'
                           f'{" checked" if done else ""}> {_inline(t.group(2))}')
                task_no += 1
            else:
                out.append(f"<li>{_inline(text)}")
        elif stripped in ("---", "***"):
            flush()
            out.append("<hr>")
        else:
            if lists or quote:
                flush()
            para.append(stripped)
        i += 1
    flush()
    return Markup("".join(out))


def toggle_task(data_dir: Path, fid: str, note_id: str, index: int, done: bool) -> dict:
    """Tick or untick the note's `index`-th to-do box (counted as `render` counts them)."""
    note = get(data_dir, fid, note_id)
    lines = note["body"].replace("\r\n", "\n").split("\n")
    n, fenced = 0, False
    for k, line in enumerate(lines):
        if line.strip().startswith("```"):
            fenced = not fenced
            continue
        m = None if fenced else _ITEM.match(line)
        if m and _TASK.match(m.group(3)):
            if n == index:
                at = m.start(3) + 1                                 # the character inside "[ ]"
                lines[k] = line[:at] + ("x" if done else " ") + line[at + 1:]
                return update(data_dir, fid, note_id, {"body": "\n".join(lines)})
            n += 1
    raise LookupError(f"no to-do {index}")


_MARKER = re.compile(r"^\s*(?:#{1,6}|[-*]|\d+[.)]|>)?\s*(?:\[[ xX]\]\s+)?")


def excerpt(body: str, length: int = 90) -> str:
    """The body as plain text for the list: block markers, task boxes and link targets dropped."""
    lines = (_MARKER.sub("", line) for line in body.replace("\r\n", "\n").split("\n") if line.strip() and
             not line.strip().startswith("```"))
    text = re.sub(r"!?\[([^\]\n]+)\]\([^)\s]*\)", r"\1", " · ".join(lines))
    text = " ".join(re.sub(r"[*_`]", "", text).split())
    return text[:length] + ("…" if len(text) > length else "")
