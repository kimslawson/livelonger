"""A small, forgiving HTML tree built on the stdlib parser.

Good enough to pull pieces out of LiveJournal pages and to rewrite a captured
journal page into a reusable template. Not a full HTML5 parser.
"""
from __future__ import annotations

import html
from html.parser import HTMLParser

VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
    "param", "source", "track", "wbr",
}
RAW_TEXT = {"script", "style"}
# tag -> set of open tags it implicitly closes
AUTO_CLOSE = {
    "p": {"p"},
    "li": {"li"},
    "dt": {"dt", "dd"},
    "dd": {"dt", "dd"},
    "tr": {"tr", "td", "th"},
    "td": {"td", "th"},
    "th": {"td", "th"},
    "option": {"option"},
}
BLOCK = {
    "address", "article", "aside", "blockquote", "div", "dl", "fieldset", "footer",
    "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "main", "nav", "ol",
    "pre", "section", "table", "ul",
}


class Text:
    __slots__ = ("data", "parent", "raw")

    def __init__(self, data: str, raw: bool = False):
        self.data = data
        self.parent = None
        self.raw = raw  # True for script/style content, comments, doctypes

    def html(self) -> str:
        return self.data if self.raw else html.escape(self.data, quote=False)

    def text(self) -> str:
        return "" if self.raw else self.data


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: dict | None = None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children: list = []
        self.parent = None

    # -- navigation -----------------------------------------------------
    def append(self, child):
        child.parent = self
        self.children.append(child)
        return child

    def iter(self):
        """Depth-first iteration over element descendants (including self)."""
        stack = [self]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(c for c in reversed(node.children) if isinstance(c, Node))

    def find_all(self, pred):
        return [n for n in self.iter() if pred(n)]

    def find(self, pred):
        for n in self.iter():
            if pred(n):
                return n
        return None

    def by_class(self, cls: str):
        return self.find(lambda n: cls in n.classes())

    def all_by_class(self, cls: str):
        return self.find_all(lambda n: cls in n.classes())

    def by_id(self, ident: str):
        return self.find(lambda n: n.attrs.get("id") == ident)

    def classes(self) -> list[str]:
        return (self.attrs.get("class") or "").split()

    def ancestors(self):
        n = self.parent
        while n is not None:
            yield n
            n = n.parent

    def remove(self):
        if self.parent is not None:
            self.parent.children.remove(self)
            self.parent = None

    def replace_children(self, children):
        for c in self.children:
            c.parent = None
        self.children = []
        for c in children:
            self.append(c)

    # -- output ---------------------------------------------------------
    def text(self) -> str:
        out = []
        for c in self.children:
            out.append(c.text())
        return "".join(out)

    def inner_html(self) -> str:
        return "".join(c.html() for c in self.children)

    def html(self) -> str:
        if self.tag == "#root":
            return self.inner_html()
        attrs = "".join(
            f" {k}" if v is None else f' {k}="{html.escape(v, quote=True)}"'
            for k, v in self.attrs.items()
        )
        if self.tag in VOID:
            return f"<{self.tag}{attrs}>"
        return f"<{self.tag}{attrs}>{self.inner_html()}</{self.tag}>"


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")
        self.stack = [self.root]

    @property
    def top(self):
        return self.stack[-1]

    def _open(self, tag, attrs):
        closes = AUTO_CLOSE.get(tag)
        if closes:
            while len(self.stack) > 1 and self.top.tag in closes:
                self.stack.pop()
        if tag in BLOCK and self.top.tag == "p":
            self.stack.pop()
        node = Node(tag, {k: v for k, v in attrs})
        self.top.append(node)
        return node

    def handle_starttag(self, tag, attrs):
        node = self._open(tag, attrs)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self._open(tag, attrs)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return
        # stray end tag: ignore

    def handle_data(self, data):
        self.top.append(Text(data, raw=self.top.tag in RAW_TEXT))

    def handle_comment(self, data):
        self.top.append(Text(f"<!--{data}-->", raw=True))

    def handle_decl(self, decl):
        self.top.append(Text(f"<!{decl}>", raw=True))

    def handle_pi(self, data):
        self.top.append(Text(f"<?{data}>", raw=True))

    def unknown_decl(self, data):
        self.top.append(Text(f"<![{data}]>", raw=True))


def parse(markup: str) -> Node:
    b = _Builder()
    b.feed(markup)
    b.close()
    return b.root


def fragment(markup: str) -> list:
    """Parse a fragment and return its top-level nodes (detached)."""
    root = parse(markup)
    kids = list(root.children)
    for k in kids:
        k.parent = None
    return kids


def common_ancestor(a: Node, b: Node) -> Node | None:
    seen = set()
    for n in [a, *a.ancestors()]:
        seen.add(id(n))
    for n in [b, *b.ancestors()]:
        if id(n) in seen:
            return n
    return None
