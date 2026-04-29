"""html_sink.py — Markdown → self-contained HTML report renderer.

Part of the FinOps Engine shared utilities.

Converts the Markdown reports produced by each engine into self-contained
HTML files (inline CSS, no JS, no external assets), suitable for opening
in any browser or sharing via email / Outlook.

No third-party dependencies — uses the Python standard library only.

Public API
----------
    write_html(md_path, html_path, title=None)
        Read a Markdown file and write a self-contained HTML file.

    write_index(out_dir, reports)
        Write an ``index.html`` that links to all HTML report files.

    md_to_html(title, md) -> str
        Convert a Markdown string to a complete HTML page string.
"""
from __future__ import annotations

import html
import re
from pathlib import Path

# ---------------------------------------------------------------------------
# CSS — inline in every page; Outlook-safe (no flexbox/grid, table-based).
# ---------------------------------------------------------------------------
_CSS = """
* { box-sizing: border-box; }
body {
    font-family: Calibri, 'Segoe UI', Arial, sans-serif;
    font-size: 14px;
    line-height: 1.6;
    color: #101828;
    background: #ffffff;
    margin: 0;
    padding: 0;
}
.container {
    max-width: 960px;
    margin: 32px auto;
    padding: 0 28px;
}
h1 {
    font-size: 22px;
    color: #0f172a;
    border-bottom: 2px solid #0284c7;
    padding-bottom: 6px;
    margin-top: 32px;
    margin-bottom: 12px;
}
h2 {
    font-size: 17px;
    color: #0f172a;
    border-bottom: 1px solid #e2e8f0;
    padding-bottom: 4px;
    margin-top: 28px;
    margin-bottom: 10px;
}
h3 {
    font-size: 14px;
    color: #1e293b;
    margin-top: 20px;
    margin-bottom: 8px;
}
p { margin: 8px 0; }
a { color: #0284c7; text-decoration: none; }
a:hover { text-decoration: underline; }
code {
    font-family: Consolas, 'Courier New', monospace;
    font-size: 12px;
    background: #f1f5f9;
    padding: 1px 5px;
    border-radius: 3px;
}
pre {
    background: #f1f5f9;
    padding: 12px 16px;
    border-radius: 4px;
    overflow-x: auto;
    font-size: 12px;
}
pre code { background: none; padding: 0; }
table {
    border-collapse: collapse;
    width: 100%;
    margin: 14px 0;
    font-size: 13px;
}
thead tr { background: #0284c7; }
th {
    color: #ffffff;
    padding: 8px 10px;
    text-align: left;
    font-weight: 600;
    white-space: nowrap;
}
td {
    padding: 6px 10px;
    border-bottom: 1px solid #e2e8f0;
    vertical-align: top;
}
tr:nth-child(even) td { background: #f8fafc; }
blockquote {
    margin: 12px 0;
    padding: 10px 16px;
    background: #fffbeb;
    border-left: 4px solid #f59e0b;
    color: #78350f;
}
blockquote p { margin: 4px 0; }
ul { margin: 8px 0; padding-left: 24px; }
li { margin: 3px 0; }
hr { border: none; border-top: 1px solid #e2e8f0; margin: 20px 0; }
strong { font-weight: 600; }
em { font-style: italic; }
.footer {
    margin-top: 40px;
    padding-top: 10px;
    border-top: 1px solid #e2e8f0;
    font-size: 11px;
    color: #94a3b8;
}
.index-list { list-style: none; padding: 0; }
.index-list li {
    padding: 10px 14px;
    border: 1px solid #e2e8f0;
    border-radius: 6px;
    margin-bottom: 8px;
}
.index-list li a { font-size: 15px; font-weight: 600; }
"""

_PAGE_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>{css}</style>
</head>
<body>
<div class="container">
{body}
</div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Inline Markdown → HTML helpers
# ---------------------------------------------------------------------------

def _esc(text: str) -> str:
    """HTML-escape a plain-text string (& < > only; leave quotes alone)."""
    return html.escape(text, quote=False)


def _inline(text: str) -> str:
    """Convert inline Markdown within a line to HTML.

    Processes (in order): bold, italic, inline code, links.
    The input text is HTML-escaped first so that literal angle brackets
    and ampersands in the source are safe.
    """
    text = _esc(text)
    # Bold: **text** or __text__
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'__(.+?)__', r'<strong>\1</strong>', text)
    # Italic: *text* (not **) or _text_ (not __)
    text = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<em>\1</em>', text)
    text = re.sub(r'(?<!_)_(?!_)([^_\n]+?)(?<!_)_(?!_)', r'<em>\1</em>', text)
    # Inline code: `text`
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
    # Links: [label](url)
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)',
                  r'<a href="\2">\1</a>', text)
    return text


# ---------------------------------------------------------------------------
# Table rendering
# ---------------------------------------------------------------------------

def _is_separator_row(line: str) -> bool:
    """Return True if *line* is a GFM table separator (e.g. |---|---:|)."""
    stripped = line.strip()
    # Must consist only of |, -, :, spaces
    return bool(stripped) and bool(re.match(r'^[\|:\- ]+$', stripped))


def _parse_table_row(line: str) -> list[str]:
    """Split a pipe-delimited table row into cell strings."""
    s = line.strip()
    if s.startswith('|'):
        s = s[1:]
    if s.endswith('|'):
        s = s[:-1]
    return [c.strip() for c in s.split('|')]


def _render_table(rows: list[str]) -> str:
    """Render a list of GFM table row strings to an HTML ``<table>``."""
    # Locate separator row
    sep_idx = next(
        (i for i, r in enumerate(rows) if _is_separator_row(r)), None
    )
    if sep_idx is not None:
        header_rows = rows[:sep_idx]
        body_rows = rows[sep_idx + 1:]
        sep_cells = _parse_table_row(rows[sep_idx])
        alignments: list[str] = []
        for c in sep_cells:
            c = c.strip()
            if c.startswith(':') and c.endswith(':'):
                alignments.append('center')
            elif c.endswith(':'):
                alignments.append('right')
            else:
                alignments.append('left')
    else:
        header_rows = []
        body_rows = rows
        alignments = []

    def align_attr(i: int) -> str:
        if i < len(alignments) and alignments[i] in ('right', 'center'):
            return f' style="text-align:{alignments[i]}"'
        return ''

    out: list[str] = ['<table>']
    if header_rows:
        out.append('<thead>')
        for row in header_rows:
            cells = _parse_table_row(row)
            out.append('<tr>')
            for i, c in enumerate(cells):
                out.append(f'<th{align_attr(i)}>{_inline(c)}</th>')
            out.append('</tr>')
        out.append('</thead>')
    out.append('<tbody>')
    for row in body_rows:
        if not row.strip():
            continue
        cells = _parse_table_row(row)
        out.append('<tr>')
        for i, c in enumerate(cells):
            out.append(f'<td{align_attr(i)}>{_inline(c)}</td>')
        out.append('</tr>')
    out.append('</tbody>')
    out.append('</table>')
    return '\n'.join(out)


# ---------------------------------------------------------------------------
# Block-level Markdown → HTML
# ---------------------------------------------------------------------------

def _is_table_start(line: str, idx: int, lines: list[str]) -> bool:
    """Return True when *line* is the header row of a GFM table.

    A GFM table header row contains ``|`` and is immediately followed by a
    separator row (e.g. ``|---|---:|``).
    """
    return (
        '|' in line
        and not _is_separator_row(line)
        and idx + 1 < len(lines)
        and _is_separator_row(lines[idx + 1])
    )


def _render_blocks(lines: list[str]) -> str:
    """Convert a list of Markdown lines to an HTML fragment string."""
    out: list[str] = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]

        # --- ATX heading ---
        m = re.match(r'^(#{1,6})\s+(.*)', line)
        if m:
            lvl = len(m.group(1))
            out.append(f'<h{lvl}>{_inline(m.group(2))}</h{lvl}>')
            i += 1
            continue

        # --- Horizontal rule (---, ***, ___) ---
        if re.match(r'^(?:-{3,}|\*{3,}|_{3,})\s*$', line):
            out.append('<hr>')
            i += 1
            continue

        # --- Blockquote (consecutive > lines) ---
        if line.startswith('>'):
            bq: list[str] = []
            while i < n and lines[i].startswith('>'):
                bq.append(lines[i][1:].lstrip(' '))
                i += 1
            inner = _render_blocks(bq)
            out.append(f'<blockquote>{inner}</blockquote>')
            continue

        # --- GFM table (header row followed by separator row) ---
        if _is_table_start(line, i, lines):
            tbl: list[str] = []
            while i < n and '|' in lines[i]:
                tbl.append(lines[i])
                i += 1
            out.append(_render_table(tbl))
            continue

        # --- Unordered list (lines starting with - or * + space) ---
        if re.match(r'^[*\-]\s', line):
            items: list[str] = []
            while i < n and re.match(r'^[*\-]\s', lines[i]):
                items.append(lines[i][2:].strip())
                i += 1
            out.append('<ul>')
            for item in items:
                out.append(f'<li>{_inline(item)}</li>')
            out.append('</ul>')
            continue

        # --- Empty line ---
        if not line.strip():
            i += 1
            continue

        # --- Paragraph (collect consecutive non-special lines) ---
        _heading_re = re.compile(r'^#{1,6}\s')
        _hr_re = re.compile(r'^(?:-{3,}|\*{3,}|_{3,})\s*$')
        para: list[str] = []
        while i < n:
            ln = lines[i]
            if not ln.strip():
                break
            if _heading_re.match(ln):
                break
            if _hr_re.match(ln):
                break
            if ln.startswith('>'):
                break
            if _is_table_start(ln, i, lines):
                break
            if re.match(r'^[*\-]\s', ln):
                break
            para.append(ln)
            i += 1
        if para:
            out.append(f'<p>{_inline(" ".join(para))}</p>')

    return '\n'.join(out)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def md_to_html(title: str, md: str) -> str:
    """Convert a Markdown string to a complete, self-contained HTML page.

    Parameters
    ----------
    title:
        The page ``<title>`` and main heading shown in the browser tab.
    md:
        Markdown source text.

    Returns
    -------
    str
        A full HTML document as a string (UTF-8 safe).
    """
    body = _render_blocks(md.splitlines())
    return _PAGE_TEMPLATE.format(
        title=html.escape(title),
        css=_CSS,
        body=body,
    )


def write_html(md_path: Path, html_path: Path,
               title: str | None = None) -> None:
    """Read *md_path* and write a self-contained HTML file to *html_path*.

    Parameters
    ----------
    md_path:
        Source Markdown file.
    html_path:
        Destination HTML file (created or overwritten).
    title:
        Page title.  If *None*, extracted from the first ``# Heading``
        in the document; falls back to the stem of *md_path*.
    """
    md = md_path.read_text(encoding="utf-8")
    if title is None:
        m = re.search(r'^#\s+(.*)', md, re.MULTILINE)
        title = m.group(1).strip() if m else md_path.stem
    html_path.write_text(md_to_html(title, md), encoding="utf-8")


def write_index(out_dir: Path,
                reports: list[tuple[str, str]]) -> None:
    """Write an ``index.html`` that links to every HTML report.

    Parameters
    ----------
    out_dir:
        Directory in which to write ``index.html``.
    reports:
        Ordered list of ``(title, html_filename)`` pairs.
    """
    items = "\n".join(
        f'  <li><a href="{html.escape(fn)}">'
        f'{html.escape(t)}</a></li>'
        for t, fn in reports
    )
    body = (
        "<h1>FinOps Engine — Reports</h1>\n"
        "<ul class=\"index-list\">\n"
        f"{items}\n"
        "</ul>\n"
        '<p class="footer">Generated by the FinOps Engine.</p>'
    )
    page = _PAGE_TEMPLATE.format(
        title="FinOps Engine Reports",
        css=_CSS,
        body=body,
    )
    (out_dir / "index.html").write_text(page, encoding="utf-8")
