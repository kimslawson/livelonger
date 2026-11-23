"""Rewrite LiveJournal URLs to pages in the local mirror when we have them.

This is what makes separately-archived journals interlink: a link in your
entry to https://poeticdream.livejournal.com/12345.html?thread=678#t678 becomes
../poeticdream/12345.html#t678 if that entry is in the archive, and stays a
live LJ link otherwise.
"""
from __future__ import annotations

import html
import re
import urllib.parse

from .store import norm_user

NOT_USERS = {"www", "users", "community", "pics", "ic", "l-userpic", "l-stat", "stat", "m",
             "syndicated", "userpic", "files", "status", "xc3", "imgprx", "lj-toys"}
TAG_RE = re.compile(r"<[a-zA-Z][^>]*>", re.S)
ATTR_RE = re.compile(r"""(\s(?:href|src|background)\s*=\s*)("([^"]*)"|'([^']*)'|([^\s>"']+))""", re.I)


def parse_lj_url(url: str):
    """-> (user, path_after_journal_root, query, fragment) or None."""
    try:
        sp = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return None
    host = (sp.hostname or "").lower()
    if not host.endswith("livejournal.com"):
        return None
    path = sp.path or "/"
    sub = host[: -len("livejournal.com")].rstrip(".")
    if sub and sub not in NOT_USERS and "." not in sub:
        return norm_user(sub), path, sp.query, sp.fragment
    if sub in ("users", "community"):
        m = re.match(r"/([\w-]+)(/.*)?$", path)
        if m:
            return norm_user(m.group(1)), m.group(2) or "/", sp.query, sp.fragment
        return None
    if sub in ("", "www"):
        m = re.match(r"/(?:users|community|~)/?([\w-]+)(/.*)?$", path)
        if m:
            return norm_user(m.group(1)), m.group(2) or "/", sp.query, sp.fragment
        if path in ("/userinfo.bml", "/profile", "/profile/"):
            q = urllib.parse.parse_qs(sp.query)
            if q.get("user"):
                return norm_user(q["user"][0]), "/profile", "", sp.fragment
    return None


class Linker:
    def __init__(self, journals: dict[str, set[int]], media: dict[str, str],
                 tag_files: dict[str, dict[str, str]] | None = None, public_only=False):
        self.journals = journals          # user -> set of ditemids
        self.media = media                # absolute url -> site-relative path
        self.tag_files = tag_files or {}  # user -> tag -> file
        self.public_only = public_only

    def resolve(self, url: str) -> str | None:
        """Site-root-relative path for a URL, or None to leave it alone."""
        u = html.unescape(url).strip()
        if u.startswith("//"):
            u = "https:" + u
        hit = self.media.get(u)
        if hit is None and u.startswith("http://"):
            hit = self.media.get("https://" + u[7:])
        if hit:
            return hit
        p = parse_lj_url(u)
        if not p:
            return None
        user, path, query, frag = p
        if user not in self.journals:
            return None
        base = f"{user}/"
        m = re.match(r"/(\d+)\.html$", path)
        if m:
            d = int(m.group(1))
            if d not in self.journals[user]:
                return None
            q = urllib.parse.parse_qs(query)
            anchor = ""
            if q.get("thread"):
                anchor = f"#t{q['thread'][0]}"
            elif frag:
                anchor = "#" + frag
            return f"{base}{d}.html{anchor}"
        if path in ("", "/"):
            return f"{base}index.html"
        if re.match(r"/(profile|info)/?$", path):
            return f"{base}profile.html"
        if re.match(r"/(friends|subscribers|subscriptions)/?$", path):
            return f"{base}connections.html"
        if re.match(r"/calendar/?$", path):
            return f"{base}archive/index.html"
        m = re.match(r"/(\d{4})/?$", path)
        if m:
            return f"{base}archive/{m.group(1)}.html"
        m = re.match(r"/(\d{4})/(\d{2})(?:/(\d{2}))?/?$", path)
        if m:
            day = f"#d{m.group(3)}" if m.group(3) else ""
            return f"{base}archive/{m.group(1)}-{m.group(2)}.html{day}"
        m = re.match(r"/tag/([^/]+)/?$", path)
        if m:
            tag = urllib.parse.unquote_plus(m.group(1))
            f = self.tag_files.get(user, {}).get(tag)
            return f"{base}tags/{f}" if f else f"{base}tags/index.html"
        return None

    def rewrite(self, markup: str, depth: int) -> str:
        """Rewrite href/src attributes inside tags; depth = dirs below site root."""
        prefix = "../" * depth

        def fix_attr(m):
            val = m.group(3) if m.group(3) is not None else (m.group(4) if m.group(4) is not None else m.group(5))
            target = self.resolve(val)
            if target is None:
                return m.group(0)
            return f'{m.group(1)}"{html.escape(prefix + target, quote=True)}"'

        def fix_tag(m):
            t = m.group(0)
            if "href" not in t and "src" not in t and "background" not in t:
                return t
            return ATTR_RE.sub(fix_attr, t)

        return TAG_RE.sub(fix_tag, markup)
