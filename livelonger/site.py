"""Build a browsable static mirror from the archive.

Everything uses relative links, so the result works straight from disk
(open site/index.html) as well as from any static web server.
"""
from __future__ import annotations

import calendar
import datetime as dt
import hashlib
import html
import json
import os
import re
import shutil
from collections import defaultdict
from pathlib import Path

from . import markup
from .linker import Linker
from .media import MediaStore
from .store import Archive, Journal, journal_base_url, live_count, norm_user, slug
from .theme import CONTENT_MARK, ROOT_MARK, TITLE_MARK

PER_PAGE = 20
TAGLINE = "Archived with livelonger. Live long and prosper. 🖖"
ASSETS = Path(__file__).parent / "assets"
esc = html.escape


# ---------------------------------------------------------------------------
# formatting helpers

def parse_time(s: str) -> dt.datetime | None:
    s = (s or "").replace("T", " ").rstrip("Z")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return dt.datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    return None


def ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def lj_date(s: str, with_time=True) -> str:
    t = parse_time(s)
    if not t:
        return esc(s or "")
    mon = t.strftime("%b") + ("." if t.month != 5 else "")
    out = f"{mon} {ordinal(t.day)}, {t.year}"
    if with_time:
        hour = t.hour % 12 or 12
        out += f" at {hour}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"
    return out


def tag_file(tag: str) -> str:
    s = slug(tag)
    if s == "x" or s != tag.lower().strip():
        s = f"{s}-{hashlib.md5(tag.encode()).hexdigest()[:6]}"
    return s + ".html"


def plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


# ---------------------------------------------------------------------------

class JournalView:
    """A journal's data, loaded and indexed for rendering."""

    def __init__(self, j: Journal, public_only: bool):
        self.j = j
        self.user = j.user
        self.meta = j.meta
        self.profile = j.get("profile", {}) or {}
        self.userpics = j.get("userpics", {}) or {}
        self.friends = j.get("friends", {}) or {}
        self.public_only = public_only
        entries = j.entries()
        if public_only:
            entries = [e for e in entries if e.get("security") == "public"]
        entries.sort(key=lambda e: (e.get("eventtime") or "", e["ditemid"]))
        self.entries = entries  # oldest first
        for e in entries:  # recount: excludes deleted comments and LJ-flagged spam
            cs = j.load_comments(e["ditemid"])
            if cs:
                e["comment_count"] = live_count(cs)
        self.by_id = {e["ditemid"]: e for e in entries}
        self.title = (self.meta.get("title") or self.profile.get("journal_title")
                      or self.meta.get("fullname") or self.user)
        self.subtitle = self.meta.get("subtitle") or self.profile.get("journal_subtitle") or ""
        self.tags: dict[str, list[dict]] = defaultdict(list)
        for e in entries:
            for t in e.get("tags") or []:
                self.tags[t].append(e)
        self.tag_files = {t: tag_file(t) for t in self.tags}
        self.groups = {int(g["id"]): g.get("name", "") for g in self.friends.get("groups") or []
                       if str(g.get("id", "")).isdigit()}
        theme_dir = j.p("theme")
        self.shell = (theme_dir / "shell.html").read_text() if (theme_dir / "shell.html").exists() else None

    def security_label(self, e) -> str:
        sec = e.get("security") or "public"
        if sec == "public":
            return ""
        if sec == "private":
            return "private"
        if sec == "usemask":
            mask = int(e.get("allowmask") or 0)
            if mask == 1:
                return "friends"
            names = [self.groups.get(b, f"group {b}") for b in range(1, 31) if mask & (1 << b)]
            return "custom: " + ", ".join(names) if names else "custom"
        return "locked"


class SiteBuilder:
    def __init__(self, archive: Archive, out: Path, theme: str = "original",
                 public_only: bool = False, log=print):
        self.archive = archive
        self.out = Path(out)
        self.theme = theme
        self.public_only = public_only
        self.log = log
        self.views: dict[str, JournalView] = {}
        self.store = MediaStore(archive)

    @property
    def privacy_note(self) -> str:
        return ("public entries only" if self.public_only
                else "includes private entries — keep this copy private")

    # -- infrastructure --------------------------------------------------
    def write(self, rel: str, content: str):
        p = self.out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    def ljuser(self, name: str, kind: str = "user", title: str | None = None) -> str:
        return markup.default_ljuser(name, kind, title)

    def userpic_img(self, url: str, alt: str = "", cls: str = "ll-userpic") -> str:
        if not url:
            return ""
        return f'<img class="{cls}" src="{esc(url)}" alt="{esc(alt)}" loading="lazy">'

    def finalize(self, page: str, depth: int) -> str:
        page = self.linker.rewrite(page, depth)
        prefix = "../" * depth
        page = re.sub(r"https://l-stat\.livejournal\.net/img/(?:userinfo|userinfo_v\d+)\.(?:svg|png|gif)[^\"']*",
                      f"{prefix}assets/userhead.svg", page)
        page = re.sub(r"https://l-stat\.livejournal\.net/img/(?:community|community_v\d+)\.(?:svg|png|gif)[^\"']*",
                      f"{prefix}assets/community.svg", page)
        return page

    def page(self, v: JournalView | None, rel: str, title: str, content: str, depth: int,
             active: str = ""):
        prefix = "../" * depth
        if v is not None:
            nav_items = [("recent", "Recent Entries", "index.html"),
                         ("archive", "Archive", "archive/index.html"),
                         ("tags", "Tags", "tags/index.html"),
                         ("profile", "Profile", "profile.html"),
                         ("connections", "Connections", "connections.html"),
                         ("search", "Search", "search.html")]
            jprefix = prefix + v.user + "/"
            nav = "".join(
                f'<li class="{"current" if k == active else ""}"><a href="{jprefix}{href}">{label}</a></li>'
                for k, label, href in nav_items
            )
            nav += f'<li class="ll-home"><a href="{prefix}index.html">All journals</a></li>'
            nav_html = f'<nav class="ll-nav"><ul>{nav}</ul></nav>'
        else:
            nav_html = ""
        full_title = f"{title} — {v.title}" if v and title != v.title else title
        use_shell = v is not None and self.theme == "original" and v.shell
        if use_shell:
            body = (f'<div class="ll-in-shell">{nav_html}{content}'
                    f'<footer class="ll-footer">{TAGLINE} · {self.privacy_note}</footer></div>')
            page = v.shell.replace(CONTENT_MARK, body).replace(TITLE_MARK, esc(full_title))
            page = page.replace(ROOT_MARK, f"{prefix}{v.user}/_theme/")
            css = (f'<link rel="stylesheet" href="{prefix}assets/ll.css">'
                   f'<meta name="viewport" content="width=device-width, initial-scale=1">'
                   f'<meta charset="utf-8">')
            page = page.replace("</head>", css + "</head>", 1) if "</head>" in page else css + page
            page = page.replace("<body", '<body data-ll-theme="original"', 1)
        else:
            header = ""
            if v is not None:
                header = (f'<header class="ll-header"><h1><a href="{prefix}{v.user}/index.html">{esc(v.title)}</a></h1>'
                          + (f'<p class="ll-subtitle">{esc(v.subtitle)}</p>' if v.subtitle else "")
                          + "</header>")
            page = (
                "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
                "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
                f"<title>{esc(full_title)}</title>"
                f'<link rel="stylesheet" href="{prefix}assets/ll.css"></head>'
                f'<body class="ll-classic"><div class="ll-wrap">{header}{nav_html}'
                f'<main class="ll-main">{content}</main>'
                f'<footer class="ll-footer">{TAGLINE} · {self.privacy_note}'
                f"</footer></div></body></html>"
            )
        self.write(rel, self.finalize(page, depth))

    # -- body rendering ------------------------------------------------
    def entry_body(self, v: JournalView, e: dict, summary: bool) -> str:
        url = f"{v.user}/{e['ditemid']}.html"
        if e.get("rendered_body"):
            return e["rendered_body"]
        if e.get("body_format") == "lj":
            return markup.render(e.get("body") or "", preformatted=e.get("preformatted", False),
                                 summary=summary, entry_url=e.get("url") or url, ljuser=self.ljuser)
        return e.get("body") or ""

    def subject_html(self, e: dict) -> str:
        s = e.get("subject") or ""
        if e.get("body_format") == "lj":
            return markup.render(s, preformatted=True, autolink=False, ljuser=self.ljuser)
        return s

    def entry_block(self, v: JournalView, e: dict, depth: int, summary: bool) -> str:
        prefix = "../" * depth
        link = f"{prefix}{v.user}/{e['ditemid']}.html"
        sec = v.security_label(e)
        lock = f'<span class="ll-lock" title="{esc(sec)}">🔒 {esc(sec)}</span>' if sec else ""
        subj = self.subject_html(e)
        subj_html = (f'<h2 class="ll-subject asset-name page-header2 entry-title subject">'
                     f'<a class="subj-link" href="{link}">{subj or "<i>(no subject)</i>"}</a></h2>')
        pic = e.get("userpic_url") or v.userpics.get(e.get("userpic_keyword") or "") or v.userpics.get("__default__", "")
        poster = e.get("poster") or v.user
        icon = (f'<div class="user-icon ll-entry-icon">{self.userpic_img(pic, e.get("userpic_keyword", ""))}'
                f'{"<br>" + self.ljuser(poster) if poster != v.user else ""}</div>') if pic else ""
        meta_items = []
        for key, label in (("mood", "Current Mood"), ("music", "Current Music"), ("location", "Current Location")):
            if e.get(key):
                meta_items.append(f'<li><span class="entryMetadata-label">{label}:</span> '
                                  f'<span class="entryMetadata-content">{esc(e[key])}</span></li>')
        meta_html = f'<div class="lj-currents ll-currents"><ul>{"".join(meta_items)}</ul></div>' if meta_items else ""
        tags = e.get("tags") or []
        tags_html = ""
        if tags:
            tags_html = ('<div class="asset-tags ll-tags"><span class="ll-tags-label">Tags:</span> ' + ", ".join(
                f'<a rel="tag" href="{prefix}{v.user}/tags/{v.tag_files.get(t, tag_file(t))}">{esc(t)}</a>' for t in tags
            ) + "</div>")
        n = e.get("comment_count", 0)
        comments_link = f'<a href="{link}#comments">{plural(n, "comment")}</a>' if n else f'<a href="{link}">Link</a>'
        orig = f'<a class="ll-orig" href="{esc(e.get("url") or journal_base_url(v.user) + str(e["ditemid"]) + ".html")}">on LJ</a>'
        body = self.entry_body(v, e, summary)
        return (
            f'<article class="ll-entry post-asset asset entry" id="e{e["ditemid"]}">'
            f'<div class="asset-inner entry-inner"><div class="asset-header ll-entry-head">'
            f"{subj_html}"
            f'<div class="asset-meta asset-entry-date ll-date"><abbr class="datetime">{lj_date(e.get("eventtime"))}</abbr> {lock}</div>'
            f"</div>"
            f'<div class="asset-content entry-content"><div class="asset-body ll-body">{icon}{body}</div>'
            f"{meta_html}{tags_html}</div>"
            f'<div class="asset-meta asset-entry-links ll-entry-links">{comments_link} · {orig}</div>'
            f"</div></article>"
        )

    def comments_html(self, v: JournalView, e: dict, depth: int) -> str:
        cs = [c for c in v.j.load_comments(e["ditemid"]) if c.get("state") != "B"]  # spam
        if self.public_only:
            cs = [c for c in cs if c.get("state") not in ("S",)]
        if not cs:
            return ""
        by_parent = defaultdict(list)
        ids = {c["id"] for c in cs}
        for c in cs:
            p = c.get("parent") or 0
            by_parent[p if p in ids else 0].append(c)
        for lst in by_parent.values():
            lst.sort(key=lambda c: (c.get("date") or "", c["id"]))
        out = []
        prefix = "../" * depth
        stack = [(c, 0) for c in reversed(by_parent[0])]
        while stack:
            c, level = stack.pop()
            out.append(self.comment_html(v, e, c, level, prefix))
            for child in reversed(by_parent.get(c["id"], [])):
                stack.append((child, level + 1))
        live = sum(1 for c in cs if c.get("state") != "D")
        return (f'<section class="ll-comments" id="comments"><h3>{plural(live, "comment")}</h3>'
                + "".join(out) + "</section>")

    def comment_html(self, v, e, c, level, prefix) -> str:
        indent = min(level, 12)
        cid = c["id"]
        state = c.get("state", "A")
        if state == "D":
            inner = '<div class="ll-comment-deleted">(Deleted comment)</div>'
        else:
            poster = c.get("poster")
            if poster and not c.get("poster_is_identity") and not str(poster).startswith("ext_"):
                who = self.ljuser(poster)
            elif poster:
                disp = c.get("poster_display") or poster
                who = (f'<a href="{esc(c.get("poster_url"))}">{esc(disp)}</a>' if c.get("poster_url")
                       else esc(disp))
            else:
                who = "<i>(Anonymous)</i>"
            if c.get("body_format") == "lj":
                body = markup.render(c.get("body") or "", ljuser=self.ljuser)
                subj = markup.render(c.get("subject") or "", preformatted=True, autolink=False, ljuser=self.ljuser)
            else:
                body, subj = c.get("body") or "", c.get("subject") or ""
            badges = ""
            if state == "S":
                badges += '<span class="ll-badge">screened</span>'
            if state == "F":
                badges += '<span class="ll-badge">frozen</span>'
            if not body and not c.get("loaded", True):
                body = '<i>(this comment was collapsed on LJ and could not be loaded)</i>'
            pic = self.userpic_img(c.get("userpic_url"), c.get("userpic_keyword", ""))
            inner = (
                f'<div class="ll-comment-head">{pic}<div class="ll-comment-who">{who} {badges}<br>'
                f'<a class="ll-comment-date" href="#t{cid}">{lj_date(c.get("date"))}</a></div></div>'
                + (f'<div class="ll-comment-subject">{subj}</div>' if subj else "")
                + f'<div class="ll-comment-body">{body}</div>'
            )
        parent = c.get("parent")
        plink = f' · <a href="#t{parent}">parent</a>' if parent else ""
        return (f'<div class="ll-comment comment" id="t{cid}" style="--ll-level:{indent}">{inner}'
                f'<div class="ll-comment-links"><a href="#t{cid}">link</a>{plink}</div></div>')

    # -- pages --------------------------------------------------------
    def build_journal(self, v: JournalView):
        u = v.user
        entries_desc = list(reversed(v.entries))
        # recent pages
        pages = max(1, (len(entries_desc) + PER_PAGE - 1) // PER_PAGE)
        for p in range(pages):
            chunk = entries_desc[p * PER_PAGE:(p + 1) * PER_PAGE]
            blocks = "".join(self.entry_block(v, e, 1, summary=True) for e in chunk)
            nav = []
            if p > 0:
                nav.append(f'<a class="next" href="{"index.html" if p == 1 else f"recent-{p}.html"}">← Newer</a>')
            if p < pages - 1:
                nav.append(f'<a class="prev" href="recent-{p + 2}.html">Older →</a>')
            navh = f'<div class="ll-pager">{" · ".join(nav)} <span>page {p + 1} of {pages}</span></div>'
            rel = f"{u}/index.html" if p == 0 else f"{u}/recent-{p + 1}.html"
            self.page(v, rel, v.title, navh + (blocks or "<p>No entries archived yet.</p>") + navh, 1, "recent")

        # entry pages
        for i, e in enumerate(v.entries):
            older = v.entries[i - 1] if i > 0 else None
            newer = v.entries[i + 1] if i + 1 < len(v.entries) else None
            nav = []
            if older:
                nav.append(f'<a href="{older["ditemid"]}.html">← {esc(markup.strip_tags(older.get("subject") or "") or lj_date(older.get("eventtime"), False))}</a>')
            if newer:
                nav.append(f'<a href="{newer["ditemid"]}.html">{esc(markup.strip_tags(newer.get("subject") or "") or lj_date(newer.get("eventtime"), False))} →</a>')
            navh = f'<div class="ll-pager ll-entry-pager">{"".join(nav)}</div>'
            content = self.entry_block(v, e, 1, summary=False) + self.comments_html(v, e, 1) + navh
            title = markup.strip_tags(e.get("subject") or "") or lj_date(e.get("eventtime"), False)
            self.page(v, f"{u}/{e['ditemid']}.html", title, content, 1)

        self.build_archive(v)
        self.build_tags(v)
        self.build_profile(v)
        self.build_connections(v)
        self.build_search(v)
        self.copy_theme(v)

    def build_archive(self, v: JournalView):
        u = v.user
        by_year: dict[int, dict[int, list]] = defaultdict(lambda: defaultdict(list))
        for e in v.entries:
            t = parse_time(e.get("eventtime"))
            if t:
                by_year[t.year][t.month].append((t, e))
        years = sorted(by_year)
        rows = []
        for y in years:
            n = sum(len(x) for x in by_year[y].values())
            months = " ".join(
                f'<a href="{y}-{m:02d}.html">{calendar.month_abbr[m]}</a>' if m in by_year[y]
                else f'<span class="ll-dim">{calendar.month_abbr[m]}</span>'
                for m in range(1, 13))
            rows.append(f'<tr><th><a href="{y}.html">{y}</a></th><td>{plural(n, "entry").replace("entrys", "entries")}</td><td class="ll-months">{months}</td></tr>')
        self.page(v, f"{u}/archive/index.html", "Archive",
                  f'<h2>Archive</h2><table class="ll-archive">{"".join(rows)}</table>', 2, "archive")

        for y in years:
            cal = []
            for m in range(1, 13):
                days = defaultdict(int)
                for t, e in by_year[y].get(m, []):
                    days[t.day] += 1
                weeks = calendar.Calendar(firstweekday=6).monthdayscalendar(y, m)
                rows_html = ""
                for w in weeks:
                    cells = ""
                    for d in w:
                        if d == 0:
                            cells += "<td></td>"
                        elif days.get(d):
                            cells += f'<td class="ll-has"><a href="{y}-{m:02d}.html#d{d:02d}">{d}</a><sup>{days[d]}</sup></td>'
                        else:
                            cells += f"<td>{d}</td>"
                    rows_html += f"<tr>{cells}</tr>"
                head = "".join(f"<th>{d}</th>" for d in ("S", "M", "T", "W", "T", "F", "S"))
                title = (f'<a href="{y}-{m:02d}.html">{calendar.month_name[m]}</a>' if m in by_year[y]
                         else calendar.month_name[m])
                cal.append(f'<div class="ll-month"><h3>{title}</h3><table><tr>{head}</tr>{rows_html}</table></div>')
            yi = years.index(y)
            nav = []
            if yi > 0:
                nav.append(f'<a href="{years[yi - 1]}.html">← {years[yi - 1]}</a>')
            if yi < len(years) - 1:
                nav.append(f'<a href="{years[yi + 1]}.html">{years[yi + 1]} →</a>')
            self.page(v, f"{u}/archive/{y}.html", str(y),
                      f'<h2>{y}</h2><div class="ll-pager">{" · ".join(nav)}</div><div class="ll-calendar">{"".join(cal)}</div>',
                      2, "archive")
            for m, items in by_year[y].items():
                self.page(v, f"{u}/archive/{y}-{m:02d}.html", f"{calendar.month_name[m]} {y}",
                          f'<h2>{calendar.month_name[m]} <a href="{y}.html">{y}</a></h2>' + self.entry_list(v, [e for _, e in items], 2, anchors=True),
                          2, "archive")

    def entry_list(self, v: JournalView, entries: list[dict], depth: int, anchors=False) -> str:
        prefix = "../" * depth
        out, last_day = [], None
        for e in entries:
            t = parse_time(e.get("eventtime"))
            day = t.strftime("%Y-%m-%d") if t else ""
            if day != last_day:
                if last_day is not None:
                    out.append("</ul>")
                anchor = f' id="d{t.day:02d}"' if anchors and t else ""
                out.append(f'<h4{anchor}>{lj_date(e.get("eventtime"), False)}</h4><ul class="ll-list">')
                last_day = day
            subj = markup.strip_tags(e.get("subject") or "") or "(no subject)"
            sec = v.security_label(e)
            n = e.get("comment_count", 0)
            out.append(
                f'<li><span class="ll-time">{t.strftime("%I:%M %p").lstrip("0") if t else ""}</span> '
                f'<a href="{prefix}{v.user}/{e["ditemid"]}.html">{esc(subj)}</a>'
                + (f' <span class="ll-lock">🔒 {esc(sec)}</span>' if sec else "")
                + (f' <span class="ll-dim">({plural(n, "comment")})</span>' if n else "")
                + "</li>")
        if last_day is not None:
            out.append("</ul>")
        return "".join(out)

    def build_tags(self, v: JournalView):
        u = v.user
        if v.tags:
            mx = max(len(x) for x in v.tags.values())
            cloud = " ".join(
                f'<a href="{v.tag_files[t]}" style="font-size:{0.85 + 1.2 * (len(es) / mx):.2f}em">{esc(t)}</a>'
                f'<sup class="ll-dim">{len(es)}</sup>'
                for t, es in sorted(v.tags.items(), key=lambda kv: kv[0].lower()))
        else:
            cloud = "<p>No tags.</p>"
        self.page(v, f"{u}/tags/index.html", "Tags", f'<h2>Tags</h2><div class="ll-tagcloud">{cloud}</div>', 2, "tags")
        for t, es in v.tags.items():
            self.page(v, f"{u}/tags/{v.tag_files[t]}", f"Tag: {t}",
                      f'<h2>Entries tagged “{esc(t)}”</h2>' + self.entry_list(v, list(reversed(es)), 2), 2, "tags")

    def build_profile(self, v: JournalView):
        p, m = v.profile, v.meta
        rows = []

        def row(label, value):
            if value:
                rows.append(f"<tr><th>{label}</th><td>{value}</td></tr>")

        row("Name", esc(m.get("fullname") or p.get("name") or ""))
        row("Journal", esc(v.title) + (f"<br><i>{esc(v.subtitle)}</i>" if v.subtitle else ""))
        row("Location", esc(", ".join(x for x in (p.get("city"), p.get("country")) if x)))
        if p.get("homepage"):
            row("Website", f'<a href="{esc(p["homepage"])}">{esc(p.get("homepage_title") or p["homepage"])}</a>')
        for k, val in (p.get("im") or {}).items():
            row(esc(k.upper()), esc(val))
        row("Journal created", lj_date(p.get("created", "").replace("T", " "), False) if p.get("created") else "")
        row("Last updated", lj_date(p.get("last_updated", "").replace("T", " "), False) if p.get("last_updated") else "")
        stats = f"{len(v.entries)} archived entries"
        if p.get("posts") is not None:
            stats += f" (LJ reports {p['posts']} posted"
            stats += f", {p['comments_received']} comments received)" if p.get("comments_received") is not None else ")"
        row("Stats", esc(stats))
        src = {"api": "complete owner backup via the LiveJournal API",
               "web": "fetched from LiveJournal web pages (what was visible at the time)"}.get(m.get("source"), m.get("source", ""))
        row("Archive", esc(src))
        pic = p.get("userpic") or v.userpics.get("__default__", "")
        bio = p.get("bio_html") or ""
        interests = p.get("interests") or []
        ints = ", ".join(esc(i) for i in interests)
        pics = "".join(
            f'<figure>{self.userpic_img(url, kw)}<figcaption>{esc(kw) if kw != "__default__" else "<i>default</i>"}</figcaption></figure>'
            for kw, url in sorted(v.userpics.items(), key=lambda kv: (kv[0] != "__default__", kv[0].lower())))
        content = (
            f'<div class="ll-profile"><div class="ll-profile-top">{self.userpic_img(pic, v.user)}'
            f'<div><h2>{self.ljuser(v.user)}</h2><table class="ll-profile-table">{"".join(rows)}</table></div></div>'
            + (f'<h3>Bio</h3><div class="ll-bio">{bio}</div>' if bio else "")
            + (f'<h3>Interests ({len(interests)})</h3><p class="ll-interests">{ints}</p>' if interests else "")
            + (f'<h3>Userpics ({len(v.userpics)})</h3><div class="ll-userpics">{pics}</div>' if v.userpics else "")
            + "</div>"
        )
        self.page(v, f"{v.user}/profile.html", "Profile", content, 1, "profile")

    def build_connections(self, v: JournalView):
        fr = v.friends
        friends = fr.get("friends") or []
        friendof = {norm_user(f.get("username", "")) for f in fr.get("friendof") or []}
        names = {norm_user(f.get("username", "")) for f in friends}

        def who(f):
            name = norm_user(f.get("username", ""))
            kind = "comm" if f.get("type") in ("community", "C") else "user"
            extra = []
            if f.get("fullname") and f["fullname"] != name:
                extra.append(esc(f["fullname"]))
            if name in friendof and name in names:
                extra.append("mutual")
            if name in self.views:
                extra.append(f'<a class="ll-archived" href="../{name}/index.html">archived here</a>')
            groups = [v.groups.get(b) for b in range(1, 31) if int(f.get("groupmask") or 0) & (1 << b) and v.groups.get(b)]
            if groups:
                extra.append("groups: " + esc(", ".join(groups)))
            return f'<li>{self.ljuser(name, kind)} <span class="ll-dim">{" · ".join(extra)}</span></li>'

        people = [f for f in friends if f.get("type") not in ("community", "C", "syndicated", "Y")]
        comms = [f for f in friends if f.get("type") in ("community", "C")]
        feeds = [f for f in friends if f.get("type") in ("syndicated", "Y")]
        src_note = {"api": "", "foaf": "<p class='ll-dim'>From the public FOAF profile.</p>"}.get(fr.get("source"), "")
        parts = [f"<h2>Connections</h2>{src_note}"]
        for title, lst in (("Friends", people), ("Communities", comms), ("Feeds", feeds)):
            if lst:
                parts.append(f"<h3>{title} ({len(lst)})</h3><ul class='ll-people'>"
                             + "".join(who(f) for f in sorted(lst, key=lambda f: f.get("username", ""))) + "</ul>")
        if fr.get("friendof"):
            parts.append(f"<h3>Friend of ({len(fr['friendof'])})</h3><ul class='ll-people'>"
                         + "".join(who(f) for f in sorted(fr["friendof"], key=lambda f: f.get("username", ""))) + "</ul>")
        if fr.get("groups") and not self.public_only:
            parts.append("<h3>Friend groups</h3><ul>" + "".join(
                f"<li>{esc(g.get('name', ''))}</li>" for g in sorted(fr["groups"], key=lambda g: g.get("sortorder", 0))) + "</ul>")
        self.page(v, f"{v.user}/connections.html", "Connections", "".join(parts), 1, "connections")

    def build_search(self, v: JournalView):
        data = []
        for e in v.entries:
            body = e.get("rendered_body") or e.get("body") or ""
            text = markup.strip_tags(body)
            ctext = " ".join(
                markup.strip_tags(c.get("body") or "") + " " + (c.get("poster") or "")
                for c in v.j.load_comments(e["ditemid"])
                if c.get("state") not in ("D", "B") and not (self.public_only and c.get("state") == "S"))
            data.append([e["ditemid"], (e.get("eventtime") or "")[:10],
                         markup.strip_tags(e.get("subject") or ""), text, ctext, e.get("tags") or []])
        self.write(f"{v.user}/search-data.js",
                   "window.LL_SEARCH=" + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + ";")
        content = (
            '<h2>Search</h2><form class="ll-search" onsubmit="return false">'
            '<input id="ll-q" type="search" placeholder="Search entries…" autofocus> '
            '<label><input id="ll-c" type="checkbox"> include comments</label></form>'
            '<div id="ll-results"></div>'
            '<script src="search-data.js"></script><script src="../assets/search.js"></script>'
        )
        self.page(v, f"{v.user}/search.html", "Search", content, 1, "search")

    def copy_theme(self, v: JournalView):
        src = v.j.p("theme")
        dst = self.out / v.user / "_theme"
        for sub in ("css", "img"):
            if (src / sub).exists():
                shutil.copytree(src / sub, dst / sub, dirs_exist_ok=True)

    def build_index(self):
        cards = []
        for u, v in sorted(self.views.items()):
            pic = v.profile.get("userpic") or v.userpics.get("__default__", "")
            src = v.meta.get("source")
            badge = "complete backup" if src == "api" else "web copy"
            first = lj_date(v.entries[0].get("eventtime"), False) if v.entries else ""
            last = lj_date(v.entries[-1].get("eventtime"), False) if v.entries else ""
            cards.append(
                f'<li class="ll-card"><a href="{u}/index.html">{self.userpic_img(pic, u)}</a>'
                f'<div><h2><a href="{u}/index.html">{esc(v.title)}</a></h2>'
                f'<p>{self.ljuser(u)} · <span class="ll-badge">{badge}</span></p>'
                f'<p class="ll-dim">{len(v.entries)} entries{f" · {first} – {last}" if v.entries else ""}</p></div></li>')
        content = ("<h1>Journals</h1><ul class='ll-cards'>" + "".join(cards) + "</ul>"
                   + "<p class='ll-dim'>A local mirror made with livelonger. Links between archived "
                     "journals stay inside this mirror; everything else points at LiveJournal.</p>")
        self.page(None, "index.html", "LiveJournal archive", content, 0)

    # -- media ----------------------------------------------------------
    def link_media(self) -> dict[str, str]:
        mapping = {}
        for url, rec in self.store.index.items():
            if not rec.get("path"):
                continue
            src = self.store.dir / rec["path"]
            if not src.exists():
                continue
            dst = self.out / "media" / rec["path"]
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(src, dst)
                except OSError:
                    shutil.copy2(src, dst)
            mapping[url] = "media/" + rec["path"]
        return mapping

    # -- main -----------------------------------------------------------
    def build(self, users: list[str] | None = None):
        self.out.mkdir(parents=True, exist_ok=True)
        for j in self.archive.journals():
            self.views[j.user] = JournalView(j, self.public_only)
        if not self.views:
            self.log("Nothing archived yet.")
            return
        shutil.copytree(ASSETS, self.out / "assets", dirs_exist_ok=True)
        media = self.link_media()
        self.linker = Linker({u: set(v.by_id) for u, v in self.views.items()}, media,
                             {u: v.tag_files for u, v in self.views.items()}, self.public_only)
        for u, v in self.views.items():
            if users and u not in users:
                continue
            self.log(f"Building {u}: {len(v.entries)} entries"
                     f" ({'original LJ style' if self.theme == 'original' and v.shell else 'classic theme'})")
            self.build_journal(v)
        self.build_index()
        self.log(f"Site ready: {self.out / 'index.html'}")
