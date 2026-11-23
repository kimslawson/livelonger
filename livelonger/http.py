"""A deliberately polite HTTP client.

* strictly sequential (no threads, no parallel connections)
* a minimum gap between requests to the same host group (all of
  *.livejournal.com / *.livejournal.net count as one group)
* identifies itself with a User-Agent containing a contact address
* honours Retry-After, backs off exponentially on 429/5xx, and permanently
  slows down for the rest of the run after any throttling signal
* stops immediately if LiveJournal redirects us to its bot-policy page
* optional on-disk cache so re-runs never re-request the same URL
"""
from __future__ import annotations

import gzip
import hashlib
import http.cookiejar
import json
import os
import random
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

RETRY_STATUSES = {429, 500, 502, 503, 504, 520, 521, 522, 524}
AUTH_COOKIES = {"ljsession", "ljmastersession", "ljloggedin"}


class Blocked(RuntimeError):
    """LiveJournal told us to go away. Never retry automatically."""


class FetchError(RuntimeError):
    def __init__(self, msg, status=None, url=None):
        super().__init__(msg)
        self.status = status
        self.url = url


class Response:
    def __init__(self, url, status, headers, body: bytes, from_cache=False):
        self.url = url
        self.status = status
        self.headers = headers
        self.body = body
        self.from_cache = from_cache

    @property
    def ok(self):
        return 200 <= self.status < 300

    @property
    def content_type(self) -> str:
        return (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()

    @property
    def text(self) -> str:
        ct = self.headers.get("Content-Type") or ""
        enc = "utf-8"
        if "charset=" in ct:
            enc = ct.split("charset=")[-1].split(";")[0].strip() or "utf-8"
        try:
            return self.body.decode(enc, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.text)


def host_group(url: str) -> str:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if host.endswith("livejournal.com") or host.endswith("livejournal.net"):
        return "livejournal"
    return host


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """We follow redirects ourselves so each hop is throttled and inspected."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PoliteClient:
    def __init__(self, user_agent: str, delay: float = 1.5, other_delay: float = 0.5,
                 cache_dir: Path | None = None, max_retries: int = 6, log=print,
                 timeout: float = 60):
        self.user_agent = user_agent
        self.delays = {"livejournal": delay}
        self.other_delay = other_delay
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.max_retries = max_retries
        self.log = log
        self.timeout = timeout
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar), _NoRedirect()
        )
        self._last: dict[str, float] = {}
        self.request_count = 0

    # -- cookies --------------------------------------------------------
    def set_cookie(self, name: str, value: str, domain: str = ".livejournal.com"):
        c = http.cookiejar.Cookie(
            version=0, name=name, value=value, port=None, port_specified=False,
            domain=domain, domain_specified=True, domain_initial_dot=domain.startswith("."),
            path="/", path_specified=True, secure=False, expires=None, discard=True,
            comment=None, comment_url=None, rest={}, rfc2109=False,
        )
        self.jar.set_cookie(c)

    def cookies(self) -> dict:
        return {c.name: c.value for c in self.jar}

    def has_auth(self) -> bool:
        return any(c.name in AUTH_COOKIES or c.name.startswith("ljdomsess") for c in self.jar)

    def load_cookie_file(self, path: Path):
        """Accept a Netscape cookies.txt export, or a raw `a=b; c=d` header."""
        raw = Path(path).read_text()
        if "\t" in raw:
            mj = http.cookiejar.MozillaCookieJar()
            mj.load(str(path), ignore_discard=True, ignore_expires=True)
            for c in mj:
                if "livejournal" in c.domain:
                    self.jar.set_cookie(c)
        else:
            raw = raw.strip()
            if raw.lower().startswith("cookie:"):
                raw = raw[7:]
            for part in raw.split(";"):
                if "=" in part:
                    k, v = part.strip().split("=", 1)
                    self.set_cookie(k.strip(), v.strip())

    # -- throttling -----------------------------------------------------
    def _wait(self, group: str):
        delay = self.delays.get(group, self.other_delay)
        last = self._last.get(group)
        if last is not None:
            gap = time.monotonic() - last
            if gap < delay:
                time.sleep(delay - gap)

    def _slow_down(self, group: str):
        if group == "livejournal":
            old = self.delays["livejournal"]
            self.delays["livejournal"] = min(old * 1.5, 30.0)
            self.log(f"  (slowing down: now {self.delays['livejournal']:.1f}s between LJ requests)")

    # -- cache ----------------------------------------------------------
    def _cache_paths(self, method, url, data, auth):
        key = hashlib.sha256(
            f"{method}\n{url}\n{auth}\n".encode() + (data or b"")
        ).hexdigest()
        d = self.cache_dir / key[:2]
        return d / f"{key}.body", d / f"{key}.json"

    # -- main entry point ----------------------------------------------
    def request(self, url: str, data: bytes | None = None, headers: dict | None = None,
                cache: bool = False, method: str | None = None, max_redirects: int = 8,
                allow_status=(404, 410, 403)) -> Response:
        method = method or ("POST" if data is not None else "GET")
        auth = self.has_auth() and host_group(url) == "livejournal"
        if cache and self.cache_dir:
            body_p, meta_p = self._cache_paths(method, url, data, auth)
            if meta_p.exists() and body_p.exists():
                meta = json.loads(meta_p.read_text())
                return Response(meta["url"], meta["status"], meta["headers"],
                                body_p.read_bytes(), from_cache=True)

        resp = self._fetch(url, data, headers or {}, method, max_redirects, allow_status)

        if cache and self.cache_dir and resp.status in (200, 404, 410):
            body_p, meta_p = self._cache_paths(method, url, data, auth)
            body_p.parent.mkdir(parents=True, exist_ok=True)
            body_p.write_bytes(resp.body)
            meta_p.write_text(json.dumps({"url": resp.url, "status": resp.status,
                                          "headers": dict(resp.headers), "fetched": time.time()}))
        return resp

    def get(self, url, **kw) -> Response:
        return self.request(url, **kw)

    def post(self, url, data: bytes, **kw) -> Response:
        return self.request(url, data=data, **kw)

    def _fetch(self, url, data, headers, method, max_redirects, allow_status) -> Response:
        attempt = 0
        redirects = 0
        while True:
            group = host_group(url)
            self._wait(group)
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("User-Agent", self.user_agent)
            req.add_header("Accept-Encoding", "gzip, deflate")
            for k, v in headers.items():
                req.add_header(k, v)
            status, hdrs, body, final = None, {}, b"", url
            err = None
            try:
                with self.opener.open(req, timeout=self.timeout) as r:
                    status, hdrs, body = r.status, dict(r.headers), r.read()
            except urllib.error.HTTPError as e:
                status, hdrs = e.code, dict(e.headers or {})
                try:
                    body = e.read()
                except Exception:
                    body = b""
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
                err = e
            finally:
                self._last[group] = time.monotonic()
                self.request_count += 1

            if err is not None:
                reason = getattr(err, "reason", err)
                # DNS failures for dead image hosts: don't bother retrying
                if isinstance(reason, socket.gaierror) or "Name or service not known" in str(reason) \
                        or "nodename nor servname" in str(reason):
                    raise FetchError(f"host not found: {reason}", url=url)
                attempt += 1
                if attempt > self.max_retries:
                    raise FetchError(f"network error after {attempt} tries: {reason}", url=url)
                wait = min(300, (2 ** attempt) * 2) + random.random()
                self.log(f"  network error ({reason}); retrying in {wait:.0f}s")
                time.sleep(wait)
                continue

            enc = (hdrs.get("Content-Encoding") or hdrs.get("content-encoding") or "").lower()
            if enc == "gzip" and body:
                try:
                    body = gzip.decompress(body)
                except OSError:
                    pass
            elif enc == "deflate" and body:
                try:
                    body = zlib.decompress(body)
                except zlib.error:
                    body = zlib.decompress(body, -zlib.MAX_WBITS)

            location = hdrs.get("Location") or hdrs.get("location")
            if status in (301, 302, 303, 307, 308) and location:
                new = urllib.parse.urljoin(url, location)
                if "/bots" in urllib.parse.urlsplit(new).path:
                    raise Blocked(self._blocked_msg(new))
                redirects += 1
                if redirects > max_redirects:
                    raise FetchError("too many redirects", status, url)
                if status == 303 or (status in (301, 302) and method == "POST"):
                    method, data = "GET", None
                url = new
                continue

            if group == "livejournal" and status == 403 and b"/bots" in body[:20000] \
                    and b"Bot Policy" in body[:20000]:
                raise Blocked(self._blocked_msg(url))

            if status in RETRY_STATUSES:
                attempt += 1
                self._slow_down(group)
                if attempt > self.max_retries:
                    raise FetchError(f"HTTP {status} after {attempt} tries", status, url)
                ra = hdrs.get("Retry-After") or hdrs.get("retry-after")
                try:
                    wait = float(ra) if ra else min(600, (2 ** attempt) * 5)
                except ValueError:
                    wait = min(600, (2 ** attempt) * 5)
                self.log(f"  HTTP {status} from {urllib.parse.urlsplit(url).hostname}; "
                         f"waiting {wait:.0f}s before retrying")
                time.sleep(wait + random.random())
                continue

            if status >= 400 and status not in allow_status:
                raise FetchError(f"HTTP {status}", status, url)
            return Response(url, status, hdrs, body)

    @staticmethod
    def _blocked_msg(url):
        return (
            "LiveJournal redirected us to its bot-policy page, i.e. this IP is being\n"
            f"blocked ({url}). Stopping now rather than making it worse.\n"
            "Wait a while (hours) before trying again, consider raising `delay` in\n"
            "livelonger.toml, and if it persists email webmaster@livejournal.com\n"
            "explaining you are archiving your own journal one request at a time."
        )
