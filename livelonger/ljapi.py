"""LiveJournal protocol client (XML-RPC) and comment export.

Authentication uses LJ's challenge/response scheme: we ask the server for a
one-time challenge and send md5(challenge + md5(password)). Your password
itself never goes over the wire and is never written to disk.

`sessiongenerate` then gives us an `ljsession` cookie, which is what
export_comments.bml and the web pages (for friends-locked posts) accept.
"""
from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
import xmlrpc.client

from .http import PoliteClient, FetchError

XMLRPC_URL = "https://www.livejournal.com/interface/xmlrpc"
EXPORT_URL = "https://www.livejournal.com/export_comments.bml"


class LJError(RuntimeError):
    pass


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
    def __init__(self, http: PoliteClient, username: str, password: str | None = None,
                 hpassword: str | None = None):
        self.http = http
        self.username = username
        self.hpass = hpassword or (md5hex(password) if password else None)

    def _raw_call(self, method: str, params: dict):
        body = xmlrpc.client.dumps((params,), f"LJ.XMLRPC.{method}", encoding="utf-8",
                                   allow_none=False)
        r = self.http.post(XMLRPC_URL, body.encode("utf-8"),
                           headers={"Content-Type": "text/xml; charset=utf-8"},
                           allow_status=())
        try:
            (result,), _ = xmlrpc.client.loads(r.body, use_builtin_types=True)
        except xmlrpc.client.Fault as f:
            raise LJError(f"{method}: {f.faultString} (code {f.faultCode})") from None
        except Exception as e:  # HTML error page or similar
            raise LJError(f"{method}: unexpected response ({e}): {r.text[:300]!r}") from None
        return _decode(result)

    def challenge(self) -> str:
        return self._raw_call("getchallenge", {})["challenge"]

    def call(self, method: str, params: dict | None = None) -> dict:
        if not self.hpass:
            raise LJError("no password available for the API")
        params = dict(params or {})
        ch = self.challenge()
        params.update(
            username=self.username,
            auth_method="challenge",
            auth_challenge=ch,
            auth_response=md5hex(ch + self.hpass),
            ver=1,
        )
        return self._raw_call(method, params)

    # convenience wrappers -------------------------------------------
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

    def _get(self, kind: str, startid: int) -> ET.Element:
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

    def bodies(self, startid: int, maxid: int, on_raw=None, progress=None):
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
            if progress:
                progress(len(out), last, maxid)
            if last < startid:  # nothing new: done (gaps at the end)
                break
            startid = last + 1
        return out
