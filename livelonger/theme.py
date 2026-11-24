"""Capture a journal's own LiveJournal style (its S2 layout + theme + custom CSS).

LJ renders journal pages server-side from S2 code we can't get or run, so
instead we load the journal's front page once, keep its skeleton (header,
sidebar, stylesheets, decorative images), strip LJ's modern site chrome, ads
and scripts, and cut out the column where entries go. The site builder then
pours archived content into that shell, using the class names S2 layouts use,
so the journal's own CSS styles it.
"""
from __future__ import annotations

import hashlib
import re
import time
import urllib.parse
from pathlib import Path

from . import htmltree
from .http import FetchError, PoliteClient
from .store import Journal, journal_base_url, write_json

CONTENT_MARK = "<!--LL:CONTENT-->"
TITLE_MARK = "<!--LL:TITLE-->"
ROOT_MARK = "__LLROOT__"

CHROME_CLASSES = {
    "s-header", "s-menu-aside", "ljsale", "main-footer", "b-loginform", "s-header-sub",
    "asap-news", "cookies-banner", "js-lj-share", "w-cs", "b-message", "svgsprites",
    "appwidget", "s-do", "lj-like", "b-journalpromo", "journalpromo", "b-discoverytimes",
    "threeposts", "b-threeposts", "s-nav-rootlink", "b-ljplus", "b-xylem", "lj-recommended",
    "b-tree-loader", "rutos", "widget-ramblerpartner", "b-singlepost-standout",
    "s-footer", "lj_controlstrip", "s-adv", "ljcut-link-collapse", "print-logo",
}
CHROME_IDS = {"lj_controlstrip", "lj_controlstrip_new", "js", "hello-world", "ljtime"}
SITE_CSS_HINTS = ("schemius", "lj_base-journal", "header_v5", "medius/", "proximanova",
                  "flatbutton", "popup/", "notifications/", "components/", "widgets/",
                  "msgsystem", "recaptcha", "journalpromo", "categories/", "core/components",
                  "svg/flaticon", "lj_base.css", "ie.css", "adv/", "/ljextras")
CSS_URL_RE = re.compile(r"""url\(\s*(['"]?)([^'")]+)\1\s*\)""")
IMPORT_RE = re.compile(r"""@import\s+(?:url\()?\s*['"]?([^'")\s;]+)['"]?\s*\)?\s*;""")


def _is_chrome(n: htmltree.Node) -> bool:
    if n.tag in ("script", "noscript", "iframe", "template", "svg"):
        return True
    cls = set(n.classes())
    if cls & CHROME_CLASSES:
        return True
    if any(c.startswith(("s-header", "s-menu", "ljsale", "main-footer", "b-loginform",
                         "lj-like", "s-footer", "b-discovery", "s-nav", "print-")) for c in cls):
        return True
    return (n.attrs.get("id") or "") in CHROME_IDS


SITE_CHROME_PREFIXES = (
    "schemius", "lj_base", "header_v", "medius/", "proximanova", "flatbutton", "popup/",
    "notifications/", "components/", "widgets/", "msgsystem", "recaptcha", "journalpromo",
    "categories/", "core/", "svg/", "adv/", "ie.css", "post2017/", "entryunit", "common-post",
    "updateform", "ljcut", "lj-basestrap", "mainpage", "schemes/", "lj_settings",
)


def _site_css(href: str) -> bool:
    """Is this stylesheet LJ's own site chrome, rather than the journal's style?

    l-stat "combo" URLs (https://l-stat.livejournal.net/??a.css,b.css?v=1)
    bundle several files. A bundle counts as chrome only if *every* file in it
    is known chrome; anything else (voxhtml/, sup/s/, the journal's custom
    stylesheet...) is part of the style and is kept.
    """
    import urllib.parse
    if not href:
        return True
    sp = urllib.parse.urlsplit(href)
    host = (sp.hostname or "").lower()
    if not (host.endswith("livejournal.net") or host.startswith("l-stat") or host.startswith("stat.")):
        return False  # the journal's own host (/res/.../stylesheet) or elsewhere: style
    raw = href.split("??", 1)[1] if "??" in href else sp.path
    parts = [x.split("?")[0].lstrip("/") for x in raw.split(",") if x.strip()]
    return bool(parts) and all(p.startswith(SITE_CHROME_PREFIXES) for p in parts)


def find_content_column(tree: htmltree.Node, base: str) -> htmltree.Node | None:
    perm = re.compile(re.escape(base) + r"(\d+)\.html")
    first_anchor: dict[str, htmltree.Node] = {}
    order: list[str] = []
    for a in tree.find_all(lambda n: n.tag == "a"):
        m = perm.match(a.attrs.get("href") or "")
        if m and m.group(1) not in first_anchor:
            first_anchor[m.group(1)] = a
            order.append(m.group(1))
    if len(order) >= 2:
        # The first two entries' common ancestor is the entries column. (Using
        # the last entry instead risks a sidebar "recent entries" box dragging
        # the ancestor up to the whole layout.)
        lca = htmltree.common_ancestor(first_anchor[order[0]], first_anchor[order[1]])
        if lca is not None and lca.tag not in ("body", "html", "#root"):
            return lca
    for ident in ("alpha-inner", "alpha", "entries", "content-inner", "primary", "main",
                  "content", "container"):
        n = tree.by_id(ident)
        if n is not None:
            return n
    return None


def entry_style_hints(tree: htmltree.Node, column: htmltree.Node | None, base: str = "") -> dict:
    """Remember the class names the style uses for entries, so generated
    markup can mimic them."""
    hints = {"entry_classes": [], "body_classes": [], "subject_classes": []}
    if column is None:
        return hints
    for kid in column.children:
        if isinstance(kid, htmltree.Node) and kid.find(
                lambda n: n.tag == "a" and re.match(re.escape(base) + r"\d+\.html", n.attrs.get("href") or "")):
            cls = kid.classes()
            if cls:
                hints["entry_classes"] = cls
                break
    return hints


class ThemeCapture:
    def __init__(self, http: PoliteClient, journal: Journal, log=print):
        self.http = http
        self.j = journal
        self.base = journal_base_url(journal.user)
        self.dir = journal.p("theme")
        self.log = log
        self.assets: dict[str, str] = {}

    def _asset(self, url: str, sub: str) -> str | None:
        """Download a style asset; returns path relative to theme dir."""
        url = urllib.parse.urljoin(self.base, url.strip())
        if url.startswith("data:") or not url.startswith("http"):
            return None
        if url in self.assets:
            return self.assets[url]
        try:
            r = self.http.get(url, cache=True, allow_status=(403, 404, 410))
        except FetchError:
            return None
        if r.status != 200:
            return None
        path = urllib.parse.urlsplit(url).path
        ext = Path(path).suffix[:6] if "." in Path(path).name else ""
        if sub == "css":
            ext = ".css"
        name = hashlib.sha1(url.encode()).hexdigest()[:16] + ext
        rel = f"{sub}/{name}"
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if sub == "css":
            css = self._rewrite_css(r.text, url, depth=1)
            p.write_text(css)
        else:
            p.write_bytes(r.body)
        self.assets[url] = rel
        return rel

    def _rewrite_css(self, css: str, css_url: str, depth: int) -> str:
        """Rewrite url(...) refs to local copies. `depth` = dirs below theme/."""
        up = "../" * depth

        def imp(m):
            rel = self._asset(urllib.parse.urljoin(css_url, m.group(1)), "css")
            return f'@import url("{up}{rel}");' if rel else ""

        css = IMPORT_RE.sub(imp, css)

        def repl(m):
            ref = m.group(2).strip()
            if ref.startswith(("data:", "#")):
                return m.group(0)
            rel = self._asset(urllib.parse.urljoin(css_url, ref), "img")
            return f'url("{up}{rel}")' if rel else m.group(0)

        return CSS_URL_RE.sub(repl, css)

    def capture(self) -> bool:
        self.log(f"Capturing the LJ style of {self.j.user}…")
        r = self.http.get(self.base, cache=False)
        if r.status != 200:
            self.log(f"  front page unavailable (HTTP {r.status}); will use the classic theme")
            return False
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "original-frontpage.html").write_bytes(r.body)
        tree = htmltree.parse(r.text)

        column = find_content_column(tree, self.base)
        hints = entry_style_hints(tree, column, self.base)
        if column is None:
            self.log("  couldn't find the entries column; will use the classic theme")
            return False
        column.replace_children([htmltree.Text(CONTENT_MARK, raw=True)])

        for n in tree.find_all(_is_chrome):
            if n is not column and column not in list(n.iter()):
                n.remove()

        head = tree.find(lambda n: n.tag == "head")
        css_files = []
        for link in tree.find_all(lambda n: n.tag == "link"):
            rel = (link.attrs.get("rel") or "").lower()
            href = link.attrs.get("href") or ""
            if "stylesheet" in rel and href and not _site_css(href):
                local = self._asset(href, "css")
                if local:
                    link.attrs = {"rel": "stylesheet", "href": f"{ROOT_MARK}{local}"}
                    css_files.append(href)
                    continue
            link.remove()
        for st in tree.find_all(lambda n: n.tag == "style"):
            txt = st.inner_html()
            if any(x in txt for x in ("s-header", "html-adaptive", "asap-news", "padding: 103px")):
                st.remove()
                continue
            st.replace_children([htmltree.Text(self._rewrite_css(txt, self.base, 0)
                                               .replace('url("img/', f'url("{ROOT_MARK}img/'), raw=True)])
        for m in tree.find_all(lambda n: n.tag == "meta"):
            if (m.attrs.get("charset") is None) and m.attrs.get("name") != "viewport":
                m.remove()
        for img in tree.find_all(lambda n: n.tag == "img" and n.attrs.get("src")):
            local = self._asset(img.attrs["src"], "img")
            if local:
                img.attrs["src"] = f"{ROOT_MARK}{local}"
        for el in tree.find_all(lambda n: "style" in n.attrs and "url(" in (n.attrs.get("style") or "")):
            el.attrs["style"] = self._rewrite_css(el.attrs["style"], self.base, 0) \
                .replace('url("img/', f'url("{ROOT_MARK}img/')
        title = tree.find(lambda n: n.tag == "title")
        if title is not None:
            title.replace_children([htmltree.Text(TITLE_MARK, raw=True)])
        elif head is not None:
            t = htmltree.Node("title")
            t.append(htmltree.Text(TITLE_MARK, raw=True))
            head.append(t)
        # body classes/attrs from LJ's JS world aren't needed
        body = tree.find(lambda n: n.tag == "body")
        if body is not None:
            for k in list(body.attrs):
                if k not in ("class", "id"):
                    del body.attrs[k]
        html_el = tree.find(lambda n: n.tag == "html")
        if html_el is not None:
            for k in list(html_el.attrs):
                if k not in ("lang", "dir"):
                    del html_el.attrs[k]
        shell = tree.html()
        if CONTENT_MARK not in shell:
            self.log("  capture failed (content column lost); will use the classic theme")
            return False
        (self.dir / "shell.html").write_text(shell)
        write_json(self.dir / "meta.json", {
            "captured_at": int(time.time()), "source": self.base,
            "stylesheets": css_files, "hints": hints,
        })
        self.log(f"  captured: {len(css_files)} stylesheets, {len(self.assets) - len(css_files)} images")
        return True
