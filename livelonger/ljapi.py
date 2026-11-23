"""LiveJournal protocol client (XML-RPC) and comment export.

Authentication uses LJ's challenge/response scheme: we ask the server for a
one-time challenge and send md5(challenge + md5(password)). Your password
itself never goes over the wire and is never written to disk.

`sessiongenerate` then gives us an `ljsession` cookie, which is what
export_comments.bml and the web pages (for friends-locked posts) accept.
"""
from __future__ import annotations

import hashlib
import time
import xml.etree.ElementTree as ET
import xmlrpc.client

from .http import Blocked, FetchError, PoliteClient

XMLRPC_URL = "https://www.livejournal.com/interface/xmlrpc"
EXPORT_URL = "https://www.livejournal.com/export_comments.bml"


class LJError(RuntimeError):
    """An API call failed. `code` is LJ's protocol error code when there is one."""

    def __init__(self, msg, code=None, fault=""):
        super().__init__(msg)
        self.code = code
        self.fault = fault  # LJ's own fault string, without our decoration


class StopRun(Blocked):
    """Stop the whole run cleanly (login budget spent, no password, ...).
    Subclasses Blocked so generic `except LJError` fallbacks never swallow it."""


# LJ protocol error codes (see LJ::Protocol::error_message)
AUTH_FAULTS = {100, 101, 105}       # invalid username / invalid password / challenge expired
UNSUPPORTED_FAULTS = {200, 201, 203, 207, 209}  # missing/unknown/invalid argument, version, range
TRANSIENT_FAULTS = {406, 500, 501, 502, 503, 506}  # repeated requests, server/database trouble
BAN_FAULTS = {402}


def md5hex(s: str | bytes) -> str:
    if isinstance(s, str):
        s = s.encode("utf-8")
    return hashlib.md5(s).hexdigest()


def _decode(obj):
    """XML-RPC hands back non-ASCII text as base64 (bytes); make it all str."""
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    if isinstance(obj, dict):
        return {k: _decode(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decode(v) for v in obj]
    if isinstance(obj, xmlrpc.client.DateTime):
        return str(obj)
    return obj


class LJClient:
    """XML-RPC client.

    Password (challenge/response) logins are rationed: LJ rate-limits them per
    IP and temporarily bans addresses that do too many. A run uses one to make
    a session (none if a saved session still works), then authenticates every
    call with the session cookie (auth_method=cookie), which isn't a login.
    If LJ rejects the session, we make a new one once; only if that one is
    rejected too do we fall back to password logins, capped per run.
    """

    MAX_PASSWORD_LOGINS = 40  # hard stop far below the ~400 that got us banned

    def __init__(self, http: PoliteClient, username: str, password: str | None = None,
                 hpassword: str | None = None, log=print, password_provider=None,
                 on_new_session=None):
        self.http = http
        self.username = username
        self.password_provider = password_provider
        self.hpass = hpassword or (md5hex(password) if password else None)
        self.session = False
        self.session_refreshes = 0
        self.password_logins = 0
        self.on_new_session = on_new_session  # callback(cookies dict)
        self.log = log

    # -- transport ------------------------------------------------------
    def _raw_call(self, method: str, params: dict, headers: dict | None = None):
        body = xmlrpc.client.dumps((params,), f"LJ.XMLRPC.{method}", encoding="utf-8",
                                   allow_none=False)
        h = {"Content-Type": "text/xml; charset=utf-8", **(headers or {})}
        for attempt in range(4):
            r = self.http.post(XMLRPC_URL, body.encode("utf-8"), headers=h, allow_status=())
            try:
                (result,), _ = xmlrpc.client.loads(r.body, use_builtin_types=True)
                return _decode(result)
            except xmlrpc.client.Fault as f:
                fault = (f.faultString or "").strip()
                if f.faultCode in BAN_FAULTS or "banned" in fault.lower():
                    raise StopRun(
                        f"LiveJournal says: {fault}\n"
                        "This is LJ's login rate limiter. It lifts by itself; give it a few\n"
                        "hours (overnight is safest) and don't retry before then, since every\n"
                        "attempt while banned counts against you. Progress so far is saved.") from None
                if f.faultCode in TRANSIENT_FAULTS and attempt < 3:
                    self.http.slow_down()
                    wait = 30 * (attempt + 1)
                    self.log(f"  LJ: {fault} (code {f.faultCode}); retrying in {wait}s")
                    time.sleep(wait)
                    continue
                raise LJError(f"{method}: {fault} (code {f.faultCode})", f.faultCode, fault) from None
            except Exception as e:  # an HTML error/maintenance page instead of XML
                if attempt < 2:
                    self.log(f"  LJ sent a non-API response to {method}; retrying in 60s")
                    time.sleep(60)
                    continue
                raise LJError(f"{method}: unexpected response ({type(e).__name__}): "
                              f"{r.text[:200]!r}") from None
        raise LJError(f"{method}: gave up after retries")

    def challenge(self) -> str:
        return self._raw_call("getchallenge", {})["challenge"]

    # -- auth -----------------------------------------------------------
    def use_session(self):
        """Authenticate later calls with the ljsession cookie (already in the
        HTTP client's cookie jar) instead of the password."""
        self.session = True

    def new_session(self) -> dict:
        """One password login -> a fresh session cookie, installed and in use."""
        ljsession = self._password_call("sessiongenerate", {"expiration": "long"})["ljsession"]
        cookies = session_cookies(ljsession)
        for k, v in cookies.items():
            self.http.set_cookie(k, v)
        self.session = True
        if self.on_new_session:
            self.on_new_session(cookies)
        return cookies

    def call(self, method: str, params: dict | None = None) -> dict:
        params = dict(params or {})
        while self.session:
            p = dict(params, username=self.username, auth_method="cookie", ver=1)
            try:
                return self._raw_call(method, p, headers={"X-LJ-Auth": "cookie"})
            except LJError as e:
                if e.code not in AUTH_FAULTS:
                    raise
                if self.session_refreshes == 0:
                    self.session_refreshes += 1
                    self.log("  (LJ didn't accept the session; making a new one)")
                    self.new_session()
                    continue
                self.log("  (LJ doesn't accept session auth for the API; using password logins, sparingly)")
                self.session = False
        return self._password_call(method, params)

    def _password_call(self, method: str, params: dict) -> dict:
        if not self.hpass and self.password_provider:
            pw = self.password_provider()
            self.hpass = md5hex(pw) if pw else None
        if not self.hpass:
            raise StopRun("A password login is needed but no password was given; "
                          "nothing was sent to LiveJournal.")
        if self.password_logins >= self.MAX_PASSWORD_LOGINS:
            raise StopRun(
                f"Stopping before a {self.MAX_PASSWORD_LOGINS + 1}st password login in one run, "
                "to stay clear of LJ's login ban. Progress is saved; re-run later.")
        if self.password_logins >= 3:
            time.sleep(10)  # never a burst of logins
        self.password_logins += 1
        params = dict(params)
        ch = self.challenge()
        params.update(
            username=self.username,
            auth_method="challenge",
            auth_challenge=ch,
            auth_response=md5hex(ch + self.hpass),
            ver=1,
        )
        return self._raw_call(method, params)

    # -- convenience wrappers -------------------------------------------
    def login(self) -> dict:
        return self.call("login", {"getpickws": 1, "getpickwurls": 1, "getmoods": 0,
                                   "clientversion": "Python-livelonger/0.1"})

    def sessiongenerate(self) -> str:
        return self.call("sessiongenerate", {"expiration": "long"})["ljsession"]


def session_cookies(ljsession: str) -> dict:
    """Turn an ljsession value into the cookie set modern LJ expects.

    ljsession looks like  v1:u12345:s678:a<auth>:g<gen>//<client>
    LJ's web frontend uses ljmastersession (same value) and ljloggedin (u:s).
    """
    cookies = {"ljsession": ljsession, "ljmastersession": ljsession}
    parts = ljsession.split(":")
    u = next((p for p in parts if p.startswith("u")), None)
    s = next((p for p in parts if p.startswith("s")), None)
    if u and s:
        cookies["ljloggedin"] = f"{u}:{s}"
    return cookies


class CommentExporter:
    """export_comments.bml: LJ's purpose-built bulk comment export.

    comment_meta returns (id, posterid, state) for up to ~10k comments per call
    plus the userid -> username map; comment_body returns up to ~1k full
    comments per call. So a journal with 11,000 comments needs ~15 requests.
    """

    def __init__(self, http: PoliteClient, authas: str | None = None):
        self.http = http
        self.authas = authas

    def _get(self, kind: str, startid: int):
        url = f"{EXPORT_URL}?get={kind}&startid={startid}"
        if self.authas:
            url += f"&authas={self.authas}"
        r = self.http.get(url, allow_status=())
        text = r.body
        if b"<livejournal" not in text[:2000]:
            raise LJError(
                "export_comments.bml did not return XML (probably not logged in). "
                f"Got {r.content_type} from {r.url}"
            )
        try:
            return ET.fromstring(text), text
        except ET.ParseError:
            # LJ occasionally emits stray control characters; strip them
            cleaned = bytes(b for b in text if b >= 0x20 or b in (9, 10, 13))
            return ET.fromstring(cleaned), text

    def meta(self, startid: int = 0, on_raw=None):
        comments, users = {}, {}
        maxid = None
        while True:
            root, raw = self._get("comment_meta", startid)
            if on_raw:
                on_raw(f"comment_meta_{startid}.xml", raw)
            mx = root.findtext("maxid")
            maxid = int(mx) if mx and mx.strip().isdigit() else maxid
            last = startid - 1
            for c in root.iter("comment"):
                cid = int(c.get("id"))
                comments[cid] = {"posterid": c.get("posterid"), "state": c.get("state") or "A"}
                last = max(last, cid)
            for u in root.iter("usermap"):
                users[u.get("id")] = u.get("user")
            if maxid is None or last >= maxid or last < startid:
                break
            startid = last + 1
        return comments, users, maxid or 0

    def bodies(self, startid: int, maxid: int, on_raw=None, progress=None, on_batch=None):
        """Fetch comment bodies from startid to maxid, ~1000 per request.
        on_batch(dict) is called after every page so callers can persist as
        they go; an interruption then costs at most one page."""
        out = {}
        while startid <= maxid:
            root, raw = self._get("comment_body", startid)
            if on_raw:
                on_raw(f"comment_body_{startid}.xml", raw)
            last = startid - 1
            for c in root.iter("comment"):
                cid = int(c.get("id"))
                props = {p.get("name"): (p.text or "") for p in c.iter("property")}
                out[cid] = {
                    "id": cid,
                    "jitemid": int(c.get("jitemid") or 0),
                    "posterid": c.get("posterid"),
                    "parentid": int(c.get("parentid") or 0),
                    "state": c.get("state") or "A",
                    "subject": c.findtext("subject") or "",
                    "body": c.findtext("body") or "",
                    "date": c.findtext("date") or "",
                    "props": props,
                }
                last = max(last, cid)
            if on_batch:
                on_batch({k: v for k, v in out.items() if k >= startid})
            if progress:
                progress(len(out), last, maxid)
            if last < startid:  # nothing new: done (gaps at the end)
                break
            startid = last + 1
        return out
