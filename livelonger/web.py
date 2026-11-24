"""Archive journals you *don't* own, from their public web pages.

Uses (in LiveJournal's preferred order) the FOAF profile feed, the allpics
userpic page, the year/month calendar pages to discover entries, and each
entry's page, which carries the entry and its comments as embedded JSON.

If you're logged in (your own session from `livelonger backup` or `login`, or
browser cookies), friends-locked entries you're allowed to see come along too.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import re
import time
import xml.etree.ElementTree as ET

from . import htmltree
from .http import FetchError, PoliteClient
from .store import Archive, Journal, journal_base_url, live_count, norm_user

NS = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "foaf": "http://xmlns.com/foaf/0.1/",
    "ya": "http://blogs.yandex.ru/schema/foaf/",
    "lj": "http://www.livejournal.org/rss/lj/1.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
}
RDF_RES = f"{{{NS['rdf']}}}resource"
DC_TITLE = f"{{{NS['dc']}}}title"


# ---------------------------------------------------------------------------
# small parsing helpers (also used by tests)

def site_json(page: str, name: str):
    """Pull `Site.<name> = {...};` out of an LJ page."""
    m = re.search(r"Site\." + re.escape(name) + r"\s*=\s*", page)
    if not m:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(page, m.end())
        return obj
    except json.JSONDecodeError:
        return None


def parse_foaf(xml_bytes: bytes) -> dict:
    root = ET.fromstring(xml_bytes)
    person = root.find("foaf:Person", NS)
    if person is None:
        return {}

    def t(path):
        el = person.find(path, NS)
        return (el.text or "").strip() if el is not None and el.text else ""

    def res(path):
        el = person.find(path, NS)
        return el.get(RDF_RES) if el is not None else ""

    weblog = person.find("foaf:weblog", NS)
    homepage = person.find("foaf:homepage", NS)
    hp = ""
    if homepage is not None:
        from urllib.parse import unquote
        hp = unquote(homepage.get(RDF_RES) or "")
    country = person.find("ya:country", NS)
    city = person.find("ya:city", NS)
    posts = person.find("ya:blogActivity/ya:Posts/ya:posted", NS)
    received = person.find("ya:blogActivity/ya:Comments/ya:received", NS)
    knows = []
    for k in person.findall("foaf:knows", NS):
        nick = k.find("foaf:Person/foaf:nick", NS)
        if nick is not None and nick.text:
            knows.append(norm_user(nick.text))
    ims = {}
    for key in ("aimChatID", "jabberID", "msnChatID", "yahooChatID", "icqChatID"):
        v = t(f"foaf:{key}")
        if v:
            ims[key.replace("ChatID", "").replace("ID", "")] = v
    return {
        "username": norm_user(t("foaf:nick")),
        "name": t("foaf:name"),
        "journal_title": t("lj:journaltitle"),
        "journal_subtitle": t("lj:journalsubtitle"),
        "userpic": res("foaf:img"),
        "bio_html": t("ya:bio"),
        "country": country.get(DC_TITLE) if country is not None else "",
        "city": city.get(DC_TITLE) if city is not None else "",
        "homepage": hp,
        "homepage_title": homepage.get(DC_TITLE) if homepage is not None else "",
        "created": weblog.get(f"{{{NS['lj']}}}dateCreated") if weblog is not None else "",
        "last_updated": weblog.get(f"{{{NS['lj']}}}dateLastUpdated") if weblog is not None else "",
        "posts": int(posts.text) if posts is not None and (posts.text or "").isdigit() else None,
        "comments_received": int(received.text) if received is not None and (received.text or "").isdigit() else None,
        "interests": [i.get(DC_TITLE) for i in person.findall("foaf:interest", NS) if i.get(DC_TITLE)],
        "knows": knows,
        "im": ims,
    }


_PIC_RE = re.compile(
    r"<img src='(https://l-userpic\.livejournal\.com/\d+/\d+)'[^>]*>.*?</td>\s*<td>(.*?)</td>", re.S
)


def parse_allpics(page: str) -> dict:
    pics = {}
    for m in _PIC_RE.finditer(page):
        url, cell = m.group(1), m.group(2)
        if "<u>Default</u>" in cell:
            pics["__default__"] = url
        km = re.search(r"<b>Keywords:</b>(.*?)<br", cell, re.S)
        if km:
            for kw in html.unescape(re.sub(r"<[^>]+>", "", km.group(1))).split(","):
                kw = kw.strip()
                if kw:
                    pics[kw] = url
    return pics


def entry_links(page: str, base: str) -> set[int]:
    return {int(x) for x in re.findall(re.escape(base) + r"(\d+)\.html", page)}


def calendar_links(page: str, base: str) -> tuple[set[str], set[str]]:
    """Month (YYYY/MM) and day (YYYY/MM/DD) archive links on a calendar page."""
    months, days = set(), set()
    for y, mo, d in re.findall(re.escape(base) + r"(\d{4})/(\d{2})/(?:(\d{2})/)?", page):
        if d:
            days.add(f"{y}/{mo}/{d}")
            months.add(f"{y}/{mo}")
        else:
            months.add(f"{y}/{mo}")
    return months, days


def _ts(ts) -> str:
    try:
        return dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OverflowError):
        return ""


S2_BODY_CLASSES = ("asset-body", "entry-content", "entry_text", "entrytext", "entry-text",
                   "entry_body", "post-content", "b-singlepost-bodywrapper")


_CMT_ID = re.compile(r"ljcmt(\d+)$")
_MARGIN = re.compile(r"margin-left\s*:\s*(\d+)")
_BODY_CLASSES = ("comment-text", "comment-body", "comment-content", "commenttext", "comment_body",
                 "comment-entry", "comment_text", "talk-comment-box")
_DATE_CLASSES = ("comment-datetimelink", "comment-date", "datetime", "comment-datetime", "comment-time")
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _own(el, pred):
    """First descendant of el matching pred, not looking inside nested comments."""
    stack = list(reversed([c for c in el.children if isinstance(c, htmltree.Node)]))
    while stack:
        n = stack.pop()
        if _CMT_ID.match(n.attrs.get("id") or ""):
            continue
        if pred(n):
            return n
        stack.extend(reversed([c for c in n.children if isinstance(c, htmltree.Node)]))
    return None


def parse_s2_date(text: str) -> str:
    """LJ comment dates as S2 styles print them -> 'YYYY-MM-DD HH:MM:SS'."""
    t = re.sub(r"\s+", " ", text or "").strip()

    def hm(h, mi, s, ap):
        h = int(h)
        if ap:
            h = h % 12 + (12 if ap.lower() == "pm" else 0)
        return f"{h:02d}:{int(mi):02d}:{int(s or 0):02d}"

    m = re.search(r"(\d{4})-(\d{2})-(\d{2})[ T,]*(\d{1,2}):(\d{2})(?::(\d{2}))? ?([ap]m)?", t, re.I)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)} " + hm(*m.group(4, 5, 6, 7))
    m = re.search(r"([A-Za-z]{3})[a-z]*\.? (\d{1,2})(?:st|nd|rd|th)?,? (\d{4}),?(?: at)? (\d{1,2}):(\d{2})(?::(\d{2}))? ?([ap]m)?",
                  t, re.I)
    if m and m.group(1).lower()[:3] in _MONTHS:
        return (f"{m.group(3)}-{_MONTHS[m.group(1).lower()[:3]]:02d}-{int(m.group(2)):02d} "
                + hm(*m.group(4, 5, 6, 7)))
    return ""


def parse_s2_comments(tree: htmltree.Node, ditemid: int) -> list[dict]:
    """Comments as drawn by a journal's own S2 style. Every S2 layout wraps
    each comment in an element with id="ljcmt<dtalkid>"; nesting is either
    by element or by margin-left indentation."""
    out, stack = [], []  # stack of (indent, id) for margin-based nesting
    for el in tree.find_all(lambda n: bool(_CMT_ID.match(n.attrs.get("id") or ""))):
        cid = int(_CMT_ID.match(el.attrs["id"]).group(1))
        anc = next((a for a in el.ancestors() if _CMT_ID.match(a.attrs.get("id") or "")), None)
        if anc is not None:
            parent = int(_CMT_ID.match(anc.attrs["id"]).group(1))
        else:
            mm = _MARGIN.search(el.attrs.get("style") or "")
            indent = int(mm.group(1)) if mm else 0
            while stack and stack[-1][0] >= indent:
                stack.pop()
            parent = stack[-1][1] if stack else 0
            stack.append((indent, cid))
        who = _own(el, lambda n: "data-ljuser" in n.attrs or "lj:user" in n.attrs)
        poster = (who.attrs.get("data-ljuser") or who.attrs.get("lj:user")) if who else None
        pic = _own(el, lambda n: n.tag == "img" and "userpic" in (n.attrs.get("src") or ""))
        body_el = None
        for cls in _BODY_CLASSES:
            body_el = _own(el, lambda n, cls=cls: cls in n.classes())
            if body_el is not None:
                break
        date_el = None
        for cls in _DATE_CLASSES:
            date_el = _own(el, lambda n, cls=cls: cls in n.classes())
            if date_el is not None:
                break
        subj_el = _own(el, lambda n: any(c in n.classes() for c in ("comment-subject", "comment-title")))
        classes = " ".join(el.classes()) + " " + " ".join(
            c for n in el.iter() for c in n.classes() if "screened" in c or "deleted" in c)
        text_all = el.text()
        state = "A"
        if "deleted" in classes or (body_el is None and "Deleted comment" in text_all):
            state = "D"
        elif "screened" in classes:
            state = "S"
        out.append({
            "source": "web",
            "id": cid,
            "parent": parent,
            "ditemid": ditemid,
            "poster": norm_user(poster) if poster else None,
            "poster_display": poster or "",
            "poster_url": "",
            "poster_is_identity": bool(poster and poster.startswith("ext_")),
            "subject": subj_el.inner_html().strip() if subj_el else "",
            "body": body_el.inner_html().strip() if body_el is not None else "",
            "body_format": "html",
            "date": parse_s2_date(date_el.text() if date_el else ""),
            "state": state,
            "userpic_url": pic.attrs.get("src", "") if pic else "",
            "userpic_keyword": pic.attrs.get("alt", "") if pic else "",
            "loaded": body_el is not None or state == "D",
            "collapsed": body_el is None and state != "D",
            "props": {},
        })
    return out


def site_layout(url: str, **params) -> str:
    """Ask for LJ's standard page layout (style=site), whose structure and
    embedded JSON we know, instead of the journal's own S2 style, which LJ
    uses for logged-in viewers."""
    from urllib.parse import urlencode
    q = {"style": "site", **{k: v for k, v in params.items() if v is not None}}
    return f"{url}?{urlencode(q)}"


def parse_entry_page(page: str, user: str, ditemid: int, url: str) -> dict | None:
    """Extract the entry and the comments embedded in an LJ entry page."""
    sentry = site_json(page, "entry") or {}
    spage = site_json(page, "page") or {}
    if not sentry and "aentry-post__text" not in page:
        return None
    tree = htmltree.parse(page)
    body_el = tree.by_class("aentry-post__text")
    styled = body_el is None
    if styled:
        # LJ ignored ?style=site and rendered the journal's own S2 style:
        # try the body containers common S2 layouts use
        for cls in S2_BODY_CLASSES:
            body_el = tree.by_class(cls)
            if body_el is not None:
                break
    body = ""
    if body_el is not None:
        for junk in body_el.find_all(lambda n: {"ljsale", "aentry-post__slot"} & set(n.classes())):
            junk.remove()
        body = body_el.inner_html().strip()
    title_el = tree.by_class("aentry-post__title-text")
    subject = sentry.get("title")
    if subject is None and title_el is not None:
        subject = title_el.inner_html().strip()
    subject = "" if subject is None else str(subject)  # JSON may hand us a number
    tags = []
    tags_el = tree.by_class("aentry-tags")
    if tags_el is not None:
        tags = [a.text().strip() for a in tags_el.find_all(lambda n: n.tag == "a") if a.text().strip()]
    info = {}
    for key in ("mood", "music", "location"):
        el = tree.by_class(f"aentry-head__{key}")
        if el is not None:
            txt_el = el.by_class(f"aentry-head__{key}-text") or el
            info[key] = re.sub(r"\s+", " ", txt_el.text()).strip()
    is_public = sentry.get("is_public")
    remote = site_json(page, "remote")
    entry = {
        "source": "web",
        "journal": user,
        "ditemid": ditemid,
        "itemid": ditemid // 256,
        "anum": ditemid % 256,
        "url": url,
        "poster": norm_user(sentry.get("poster") or user),
        "eventtime": _ts(sentry.get("eventtime")) if sentry.get("eventtime") else "",
        "subject": subject or "",
        "body": body,
        "body_format": "html",
        "security": "public" if is_public in (True, 1, None) else "locked",
        "allowmask": 0,
        "tags": tags,
        "mood": info.get("mood", ""),
        "music": info.get("music", ""),
        "location": info.get("location", ""),
        "userpic_keyword": "",
        "userpic_url": sentry.get("poster_userpic_url") or "",
        "preformatted": True,
        "props": {},
        "reply_count": int(spage.get("replycount") or 0),
        "fetched_logged_in_as": (remote or {}).get("username") if isinstance(remote, dict) else None,
    }
    comments = [web_comment(c, ditemid) for c in spage.get("comments") or []]
    if not comments and styled:
        comments = parse_s2_comments(tree, ditemid)
    entry["layout"] = "journal-style" if styled else "site"
    return {"entry": entry, "comments": [c for c in comments if c],
            "comments_page": spage.get("comments_page"),
            # comments exist but we can't read them from this layout
            "comments_unreadable": styled and not comments and entry["reply_count"] > 0}


def web_comment(c: dict, ditemid: int) -> dict | None:
    tid = c.get("thread") or c.get("dtalkid")
    if not tid:
        return None
    unames = c.get("username") or []
    u0 = unames[0] if unames else {}
    poster = c.get("uname") or u0.get("username")
    state = "A"
    if c.get("deleted"):
        state = "D"
    elif c.get("leafclass") and "screened" in str(c.get("leafclass")):
        state = "S"
    elif c.get("frozen"):
        state = "F"
    return {
        "source": "web",
        "id": int(tid),
        "parent": int(c.get("parent") or 0),
        "ditemid": ditemid,
        "poster": norm_user(poster) if poster and not u0.get("is_identity") else poster,
        "poster_display": str(c.get("dname") or poster or ""),
        "poster_url": u0.get("journal_url") or "",
        "poster_is_identity": bool(u0.get("is_identity")),
        "subject": str(c.get("subject") or ""),
        "body": str(c.get("article") or ""),
        "body_format": "html",
        "date": _ts(c.get("ctime_ts")).replace(" ", "T") + "Z" if c.get("ctime_ts") else (c.get("ctime") or ""),
        "state": state,
        "userpic_url": c.get("userpic") or "",
        "userpic_keyword": c.get("upictitle") or "",
        "loaded": bool(c.get("loaded", 1)) and c.get("article") is not None,
        "collapsed": bool(c.get("collapsed")),
        "props": {},
    }


def whoami(http: PoliteClient, user: str = "") -> str | None:
    """Who LiveJournal's website thinks we are (None if logged out)."""
    r = http.get(f"https://www.livejournal.com/allpics.bml?user={user or 'lj'}", cache=False)
    remote = site_json(r.text, "remote") if r.status == 200 else None
    if isinstance(remote, dict) and remote.get("username"):
        return norm_user(remote["username"])
    return None


# ---------------------------------------------------------------------------

class WebFetcher:
    def __init__(self, archive: Archive, http: PoliteClient, user: str, log=print,
                 refresh: bool = False, as_user: str | None = None):
        self.archive = archive
        self.http = http
        self.user = norm_user(user)
        self.base = journal_base_url(self.user)
        self.j: Journal = archive.journal(self.user)
        self.log = log
        self.refresh = refresh
        self.as_user = as_user
        self.reparse = False  # re-read cached pages of already-saved entries

    def get(self, url, cache=True):
        return self.http.get(url, cache=cache and not self.refresh)

    # ------------------------------------------------------------------
    def profile(self) -> dict:
        self.log(f"Fetching profile for {self.user}…")
        r = self.http.get(self.base + "data/foaf.rdf", cache=False)
        if r.status != 200:
            raise FetchError(f"no such journal or not public: {self.user} (HTTP {r.status})", r.status)
        self.j.raw("foaf.rdf", r.body)
        prof = parse_foaf(r.body)
        old = self.j.get("profile", {}) or {}
        old.update(prof)
        self.j.put("profile", old)
        meta = self.j.meta
        self.j.update_meta(
            username=self.user,
            source=meta.get("source") if meta.get("source") == "api" else "web",
            title=prof.get("journal_title"), subtitle=prof.get("journal_subtitle"),
            fullname=prof.get("name"), url=self.base, web_archived_at=int(time.time()),
        )
        if meta.get("source") != "api":
            fr = self.j.get("friends", {}) or {}
            if fr.get("source") != "api":
                self.j.put("friends", {"source": "foaf", "friends": [{"username": u} for u in prof.get("knows", [])],
                                       "friendof": [], "groups": []})
        try:
            r = self.http.get(f"https://www.livejournal.com/allpics.bml?user={self.user}", cache=False)
            pics = parse_allpics(r.text)
            if pics:
                cur = self.j.get("userpics", {}) or {}
                cur.update(pics)
                self.j.put("userpics", cur)
        except FetchError as e:
            self.log(f"  (userpics unavailable: {e})")
        return prof

    # ------------------------------------------------------------------
    def discover(self, years: list[int] | None = None) -> list[int]:
        """Walk the calendar: year pages -> month pages -> entry ids."""
        prof = self.j.get("profile", {}) or {}
        if not years:
            y0 = int((prof.get("created") or "1999")[:4])
            y1 = int((prof.get("last_updated") or str(dt.date.today().year))[:4])
            years = list(range(y0, y1 + 1))
        found: set[int] = set()
        st = self.j.state
        for y in years:
            r = self.get(f"{self.base}{y}/", cache=y < dt.date.today().year)
            if r.status != 200:
                continue
            months, days = calendar_links(r.text, self.base)
            months = {m for m in months if m.startswith(f"{y}/")}
            found |= entry_links(r.text, self.base)
            self.log(f"  {y}: {len(months)} months with entries")
            for m in sorted(months):
                current = m >= dt.date.today().strftime("%Y/%m")
                r = self.get(f"{self.base}{m}/", cache=not current)
                ids = entry_links(r.text, self.base) if r.status == 200 else set()
                if not ids:
                    # some styles don't list entries on month pages: use days
                    for d in sorted(x for x in days if x.startswith(m + "/")):
                        rd = self.get(f"{self.base}{d}/", cache=not current)
                        ids |= entry_links(rd.text, self.base)
                found |= ids
        st["discovered"] = sorted(set(st.get("discovered", [])) | found)
        self.j.save_state(st)
        self.log(f"  {len(found)} entries found via calendar")
        return sorted(found)

    # ------------------------------------------------------------------
    def fetch_entry(self, ditemid: int) -> bool:
        url = f"{self.base}{ditemid}.html"
        r = self.get(site_layout(url))
        if r.status != 200:
            self.log(f"  {ditemid}: HTTP {r.status} (deleted or not visible to you)")
            return False
        if "adult_concepts" in r.url or "adult_explicit" in r.url:
            self.log(f"  {ditemid}: behind an adult-content interstitial; skipped")
            return False
        parsed = parse_entry_page(r.text, self.user, ditemid, url)
        if not parsed:
            self.log(f"  {ditemid}: couldn't find an entry on the page")
            return False
        entry = parsed["entry"]
        if parsed.get("comments_unreadable") and not getattr(self, "_warned_style", False):
            self._warned_style = True
            self.log(f"  WARNING: {self.user}'s style draws comments in a way livelonger can't read\n"
                     f"  yet (entry {ditemid} has {entry['reply_count']} replies, none found).\n"
                     "  Pages are cached, so a fixed version can re-read them with --reparse.")
        if self.http.has_auth() and not entry.get("fetched_logged_in_as") and not getattr(self, "_warned", False):
            self._warned = True
            self.log("  WARNING: LJ isn't treating these page loads as logged in, so friends-locked\n"
                     "  entries will be missing. Try --cookies with cookies exported from a browser\n"
                     "  where you're logged in to LiveJournal.")
        comments = {c["id"]: c for c in parsed["comments"]}

        # more pages of comments
        expected = entry["reply_count"]
        page = 2
        while len(comments) < expected and page < 200:
            rp = self.get(site_layout(url, page=page))
            p2 = parse_entry_page(rp.text, self.user, ditemid, url) if rp.status == 200 else None
            new = [c for c in (p2 or {}).get("comments", []) if c["id"] not in comments]
            if not new:
                break
            for c in new:
                comments[c["id"]] = c
            page += 1

        # collapsed threads: load each unloaded comment's own thread view
        for _ in range(3):
            unloaded = [c for c in comments.values() if not c["loaded"] and c["state"] != "D"]
            if not unloaded:
                break
            done_any = False
            for c in unloaded:
                rt = self.get(site_layout(url, thread=c["id"]))
                pt = parse_entry_page(rt.text, self.user, ditemid, url) if rt.status == 200 else None
                for nc in (pt or {}).get("comments", []):
                    old = comments.get(nc["id"])
                    if old is None or (nc["loaded"] and not old["loaded"]):
                        comments[nc["id"]] = nc
                        done_any = True
            if not done_any:
                break

        merged, _, _ = self.j.save_comments(ditemid, list(comments.values()))
        entry["comment_count"] = live_count(merged)
        return self.j.save_entry(entry)

    def run(self, years=None, limit=None, only=None):
        if not (self.j.p("profile.json").exists()) or self.refresh:
            self.profile()
        else:
            try:
                self.profile()
            except FetchError as e:
                self.log(f"  (profile refresh failed: {e})")
        if self.http.has_auth() and self.as_user:
            from .webauth import ensure_journal_login
            if ensure_journal_login(self.http, self.archive, self.as_user, self.user, self.log):
                self.log("  friends-locked entries you can see will be included")
        elif self.http.has_auth():
            self.log("  fetching with your browser cookies")
        ids = only or self.discover(years)
        have = set(self.j.entry_ids())
        todo = [i for i in ids if self.refresh or self.reparse or i not in have]
        todo.sort(reverse=True)
        if limit:
            todo = todo[:limit]
        self.log(f"Fetching {len(todo)} entries for {self.user} "
                 f"(~{len(todo) * self.http.delays['livejournal'] / 60:.0f} min at the polite rate)…")
        ok = 0
        for n, d in enumerate(todo, 1):
            try:
                if self.fetch_entry(d):
                    ok += 1
            except FetchError as e:
                self.log(f"  {d}: {e}")
            if n % 25 == 0 or n == len(todo):
                self.log(f"  {n}/{len(todo)} entries processed")
        self.log(f"  saved {ok} entries")
