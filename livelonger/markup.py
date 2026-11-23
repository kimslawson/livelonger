"""Render LiveJournal entry/comment markup to plain HTML.

Handles what the API hands back raw: automatic line breaks (unless the entry
was "preformatted"), <lj user>, <lj comm>, <lj-cut>, <lj-raw>, <lj-embed>,
<lj-poll-N>, <lj-template>, plus bare-URL autolinking, and strips scripts and
inline event handlers. Text that came from scraped web pages is already HTML
and doesn't go through here.
"""
from __future__ import annotations

import html
import re

TAG_RE = re.compile(
    r"<!--.*?-->|<(/?)([a-zA-Z][\w:.-]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*)>", re.S
)
ATTR_RE = re.compile(r"""([\w:-]+)\s*(?:=\s*("[^"]*"|'[^']*'|[^\s"'>]+))?""")
URL_RE = re.compile(r"""(?<![="'>])\b(https?://[^\s<>"']+)""")
ON_ATTR_RE = re.compile(r"""\s+on\w+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""", re.I)
JS_URL_RE = re.compile(r"""(href|src)\s*=\s*(["']?)\s*javascript:""", re.I)


def parse_attrs(s: str) -> dict:
    out = {}
    for m in ATTR_RE.finditer(s or ""):
        k, v = m.group(1).lower(), m.group(2)
        if v is None:
            out[k] = ""
        else:
            if v[:1] in "\"'":
                v = v[1:-1]
            out[k] = html.unescape(v)
    return out


def _autolink(text: str) -> str:
    def repl(m):
        url = m.group(1)
        trail = ""
        while url and url[-1] in ".,;:!?)]}'\"":
            if url[-1] == ")" and url.count("(") >= url.count(")"):
                break
            trail = url[-1] + trail
            url = url[:-1]
        return f'<a href="{url}">{url}</a>{trail}'

    return URL_RE.sub(repl, text)


def default_ljuser(name: str, kind: str = "user", title: str | None = None) -> str:
    name = name.strip().lower()
    host = name.replace("_", "-")
    label = html.escape(title or name)
    icon = "community" if kind == "comm" else "user"
    return (
        f'<span class="ljuser ll-ljuser ll-{icon}" lj:user="{html.escape(name)}">'
        f'<a href="https://{host}.livejournal.com/profile/" class="ll-userhead"></a>'
        f'<a href="https://{host}.livejournal.com/"><b>{label}</b></a></span>'
    )


def render(raw: str, *, preformatted: bool = False, summary: bool = False,
           entry_url: str = "", ljuser=default_ljuser, embed=None, poll=None,
           autolink: bool = True) -> str:
    """Convert LJ markup to HTML.

    summary=True collapses <lj-cut> sections to a "read more" link (for list
    pages); otherwise cuts are expanded and anchored as #cutidN like LJ does.
    embed(id) / poll(id) are optional callbacks returning replacement HTML.
    """
    if not raw:
        return ""
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    pos = 0
    in_raw = 0
    in_pre = 0
    in_a = 0
    skip_cut = 0  # depth of lj-cut being skipped in summary mode
    skip_script = False
    cut_n = 0

    def emit_text(t: str):
        if not t or skip_cut or skip_script:
            return
        if not in_raw and not in_pre and autolink and not in_a:
            t = _autolink(t)
        if not preformatted and not in_raw and not in_pre:
            t = t.replace("\n", "<br />\n")
        out.append(t)

    def emit(s: str):
        if not skip_cut and not skip_script:
            out.append(s)

    for m in TAG_RE.finditer(raw):
        emit_text(raw[pos:m.start()])
        pos = m.end()
        whole = m.group(0)
        if whole.startswith("<!--"):
            emit(whole)
            continue
        closing, name, attr_s = m.group(1) == "/", m.group(2).lower(), m.group(3) or ""
        selfclose = attr_s.rstrip().endswith("/")

        if skip_script:
            if closing and name == "script":
                skip_script = False
            continue
        if name == "script":
            if not closing and not selfclose:
                skip_script = True
            continue

        if name == "lj-cut":
            if closing:
                if skip_cut:
                    skip_cut -= 1
                continue
            if skip_cut:
                skip_cut += 0 if selfclose else 1
                continue
            cut_n += 1
            if summary:
                a = parse_attrs(attr_s)
                label = a.get("text") or "Read more…"
                out.append(
                    f'<b class="ll-cut">( <a href="{html.escape(entry_url)}#cutid{cut_n}">'
                    f"{html.escape(label)}</a> )</b>"
                )
                # a self-closing cut (rare, ancient) hides the rest of the entry
                skip_cut = 1
            else:
                out.append(f'<a name="cutid{cut_n}"></a>')
            continue

        if skip_cut:
            continue

        if name == "lj-raw":
            in_raw += -1 if closing else 1
            in_raw = max(in_raw, 0)
            continue
        if name == "pre":
            in_pre += -1 if closing else (0 if selfclose else 1)
            in_pre = max(in_pre, 0)
            out.append(whole)
            continue
        if name == "a":
            in_a += -1 if closing else (0 if selfclose else 1)
            in_a = max(in_a, 0)
        if name in ("lj", "lj-user") and not closing:
            a = parse_attrs(attr_s)
            if "user" in a:
                out.append(ljuser(a["user"], "user", a.get("title")))
            elif "comm" in a:
                out.append(ljuser(a["comm"], "comm", a.get("title")))
            elif "site" in a:
                out.append(html.escape(a.get("site", "")))
            continue
        if name == "lj":
            continue
        if name == "lj-embed":
            if not closing:
                a = parse_attrs(attr_s)
                eid = a.get("id")
                if eid and embed:
                    out.append(embed(eid))
                elif eid:
                    out.append(
                        f'<div class="ll-embed">[embedded media #{html.escape(eid)} — '
                        f'<a href="{html.escape(entry_url)}">view on LiveJournal</a>]</div>'
                    )
            continue
        if name.startswith("lj-poll"):
            if closing:
                continue
            pid = name[len("lj-poll-"):] if name.startswith("lj-poll-") else parse_attrs(attr_s).get("id", "")
            if poll and pid:
                out.append(poll(pid))
            else:
                out.append(
                    f'<div class="ll-poll">[poll #{html.escape(pid)} — <a href="https://www.livejournal.com/poll/?id='
                    f'{html.escape(pid)}">view on LiveJournal</a>]</div>'
                )
            continue
        if name == "lj-template":
            continue
        if name.startswith("lj-") or name.startswith("lj:"):
            # lj-like, lj-repost, lj-spoiler wrappers etc.: keep contents, drop tag
            if name == "lj-spoiler" and not closing:
                a = parse_attrs(attr_s)
                out.append(f'<details class="ll-spoiler"><summary>{html.escape(a.get("title") or "Spoiler")}</summary>')
            elif name == "lj-spoiler":
                out.append("</details>")
            continue

        # ordinary HTML tag: strip event handlers / javascript: urls
        tag = ON_ATTR_RE.sub("", whole)
        tag = JS_URL_RE.sub(r"\1=\2#", tag)
        out.append(tag)

    emit_text(raw[pos:])
    return "".join(out)


def strip_tags(s: str) -> str:
    s = re.sub(r"<(script|style)\b.*?</\1>", " ", s or "", flags=re.S | re.I)
    s = re.sub(r"<lj\s+(?:user|comm)\s*=\s*[\"']?([\w-]+)[\"']?\s*/?>", r"\1", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()
