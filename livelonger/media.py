"""Download images referenced by entries, comments, userpics and profiles.

Files are stored once, content-addressed by URL, in archive/_media/, shared by
all journals. Failures (dead hosts, 404s — Photobucket, we're looking at you)
are remembered so we don't hammer them on every run.
"""
from __future__ import annotations

import hashlib
import html
import mimetypes
import re
import time
import urllib.parse
from pathlib import Path

from .http import FetchError, PoliteClient
from .store import Archive, read_json, write_json

IMG_SRC_RE = re.compile(r"""<img\b[^>]*?\bsrc\s*=\s*(?:"([^"]+)"|'([^']+)'|([^\s>]+))""", re.I | re.S)
HREF_RE = re.compile(r"""<a\b[^>]*?\bhref\s*=\s*(?:"([^"]+)"|'([^']+)'|([^\s>]+))""", re.I | re.S)
BG_RE = re.compile(r"""background\s*=\s*["']([^"']+)["']""", re.I)
IMAGE_EXT = re.compile(r"\.(jpe?g|png|gif|webp|bmp|svg)(\?.*)?$", re.I)
LJ_PIC_HOSTS = ("pics.livejournal.com", "ic.pics.livejournal.com", "l-userpic.livejournal.com",
                "userpic.livejournal.com", "imgprx.livejournal.net")
EXT_FOR_TYPE = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
                "image/webp": ".webp", "image/svg+xml": ".svg", "image/bmp": ".bmp"}
SKIP_HOSTS = ("l-stat.livejournal.net", "stat.livejournal.com")  # site chrome, not content


def urls_in_html(s: str, include_links=True) -> set[str]:
    out = set()
    for m in IMG_SRC_RE.finditer(s or ""):
        out.add(html.unescape(next(g for g in m.groups() if g)))
    for m in BG_RE.finditer(s or ""):
        out.add(html.unescape(m.group(1)))
    if include_links:
        for m in HREF_RE.finditer(s or ""):
            u = html.unescape(next(g for g in m.groups() if g))
            host = (urllib.parse.urlsplit(u).hostname or "")
            if IMAGE_EXT.search(urllib.parse.urlsplit(u).path) or host.endswith("pics.livejournal.com"):
                out.add(u)
    return {u.strip() for u in out if u.strip().lower().startswith(("http://", "https://", "//"))}


class MediaStore:
    def __init__(self, archive: Archive):
        self.dir = archive.media_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.dir / "index.json"
        self.index: dict = read_json(self.index_path, {}) or {}
        self._dirty = 0

    def save(self):
        write_json(self.index_path, self.index)
        self._dirty = 0

    def local(self, url: str) -> str | None:
        """Path (relative to the media dir) of a downloaded copy, if any."""
        rec = self.index.get(self._norm(url))
        return rec.get("path") if rec and rec.get("path") else None

    @staticmethod
    def _norm(url: str) -> str:
        url = url.strip()
        if url.startswith("//"):
            url = "https:" + url
        return url

    def fetch(self, http: PoliteClient, url: str, retry_failed=False, log=print) -> str | None:
        url = self._norm(url)
        rec = self.index.get(url)
        if rec and rec.get("path") and (self.dir / rec["path"]).exists():
            return rec["path"]
        if rec and rec.get("error") and not retry_failed:
            return None
        host = urllib.parse.urlsplit(url).hostname or ""
        if any(host.endswith(h) for h in SKIP_HOSTS):
            return None
        fetch_url = url
        if url.startswith("http://") and any(host.endswith(h) for h in LJ_PIC_HOSTS):
            fetch_url = "https://" + url[7:]
        try:
            r = http.get(fetch_url, allow_status=(403, 404, 410, 400, 401))
        except FetchError as e:
            self.index[url] = {"error": str(e)[:200], "at": int(time.time())}
            self._bump()
            return None
        ctype = r.content_type
        if r.status != 200 or not r.body:
            self.index[url] = {"error": f"HTTP {r.status}", "at": int(time.time())}
            self._bump()
            return None
        if not (ctype.startswith("image/") or ctype.startswith("video/") or ctype.startswith("audio/")
                or ctype == "application/octet-stream"):
            self.index[url] = {"error": f"not media ({ctype})", "at": int(time.time())}
            self._bump()
            return None
        h = hashlib.sha1(url.encode()).hexdigest()
        ext = EXT_FOR_TYPE.get(ctype) or mimetypes.guess_extension(ctype) or ""
        if not ext:
            m = IMAGE_EXT.search(urllib.parse.urlsplit(url).path)
            ext = "." + m.group(1).lower() if m else ""
        rel = f"files/{h[:2]}/{h}{ext}"
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(r.body)
        self.index[url] = {"path": rel, "type": ctype, "size": len(r.body), "at": int(time.time())}
        self._bump()
        return rel

    def _bump(self):
        self._dirty += 1
        if self._dirty >= 25:
            self.save()


def collect_urls(archive: Archive, users: list[str] | None = None,
                 include_links=True) -> list[str]:
    urls: list[str] = []
    seen = set()

    def add(u):
        if u and u not in seen:
            seen.add(u)
            urls.append(u)

    for j in archive.journals():
        if users and j.user not in users:
            continue
        prof = j.get("profile", {}) or {}
        add(prof.get("userpic"))
        for u in urls_in_html(prof.get("bio_html", ""), include_links):
            add(u)
        for u in (j.get("userpics", {}) or {}).values():
            add(u)
        for e in j.entries():
            add(e.get("userpic_url"))
            for u in urls_in_html(e.get("body", ""), include_links):
                add(u)
            for u in urls_in_html(e.get("rendered_body", ""), include_links):
                add(u)
            for c in j.load_comments(e["ditemid"]):
                add(c.get("userpic_url"))
                for u in urls_in_html(c.get("body", ""), include_links):
                    add(u)
    return urls


def download_all(archive: Archive, http: PoliteClient, users=None, retry_failed=False,
                 log=print, include_links=True):
    store = MediaStore(archive)
    urls = collect_urls(archive, users, include_links)
    todo = [u for u in urls if not store.local(u) and
            (retry_failed or not (store.index.get(MediaStore._norm(u)) or {}).get("error"))]
    log(f"Media: {len(urls)} referenced, {len(todo)} to download")
    ok = 0
    for n, u in enumerate(todo, 1):
        if store.fetch(http, u, retry_failed=retry_failed, log=log):
            ok += 1
        if n % 50 == 0:
            log(f"  {n}/{len(todo)} tried, {ok} saved")
    store.save()
    failed = len(todo) - ok
    log(f"  media done: {ok} saved, {failed} unavailable (dead links are normal for old journals)")
    return store
