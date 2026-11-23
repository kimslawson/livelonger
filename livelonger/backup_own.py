"""Back up a journal you own via the official LiveJournal API.

This gets *everything* — public, friends-only, custom-group and private
entries, every comment (including screened ones), your friends/groups, tags
and userpics — with a few dozen API calls instead of thousands of page loads.
"""
from __future__ import annotations

import datetime as dt
import re
import json
import time

from .http import PoliteClient
from .ljapi import CommentExporter, LJClient, LJError, session_cookies
from .store import Archive, Journal, journal_base_url, live_count, norm_user

TS_FMT = "%Y-%m-%d %H:%M:%S"


def _minus_one_second(ts: str) -> str:
    try:
        return (dt.datetime.strptime(ts, TS_FMT) - dt.timedelta(seconds=1)).strftime(TS_FMT)
    except ValueError:
        return ts


def normalize_api_entry(ev: dict, user: str, moods: dict, pics: dict) -> dict:
    props = ev.get("props") or {}
    itemid, anum = int(ev["itemid"]), int(ev.get("anum") or 0)
    ditemid = itemid * 256 + anum
    tags = [t.strip() for t in (props.get("taglist") or "").split(",") if t.strip()]
    mood = props.get("current_mood") or moods.get(str(props.get("current_moodid", "")), "")
    kw = props.get("picture_keyword") or ""
    return {
        "source": "api",
        "journal": user,
        "itemid": itemid,
        "anum": anum,
        "ditemid": ditemid,
        "url": ev.get("url") or f"{journal_base_url(user)}{ditemid}.html",
        "poster": norm_user(ev.get("poster") or user),
        "eventtime": ev.get("eventtime", ""),
        "subject": ev.get("subject") or "",
        "body": ev.get("event") or "",
        "body_format": "lj",
        "security": ev.get("security") or "public",
        "allowmask": int(ev.get("allowmask") or 0),
        "tags": tags,
        "mood": mood,
        "music": props.get("current_music") or "",
        "location": props.get("current_location") or "",
        "userpic_keyword": kw,
        "userpic_url": pics.get(kw) or pics.get("__default__") or "",
        "preformatted": bool(int(props.get("opt_preformatted") or 0)),
        "props": props,
        "reply_count": int(ev.get("reply_count") or props.get("replycount") or 0),
    }


class OwnBackup:
    def __init__(self, archive: Archive, http: PoliteClient, user: str, password: str,
                 log=print, full: bool = False):
        self.archive = archive
        self.http = http
        self.user = norm_user(user)
        self.api = LJClient(http, self.user, password=password)
        self.j: Journal = archive.journal(self.user)
        self.log = log
        self.full = full

    # ------------------------------------------------------------------
    def run(self, comments=True, connections=True):
        info = self.login()
        self.entries()
        if comments:
            self.comments()
        if connections:
            self.connections()
        return info

    def login(self) -> dict:
        self.log(f"Logging in to the LiveJournal API as {self.user}…")
        info = self.api.login()
        self.j.raw("login.json", json.dumps(info, ensure_ascii=False, indent=1))
        pics = {}
        for kw, url in zip(info.get("pickws") or [], info.get("pickwurls") or []):
            pics[kw] = url
        if info.get("defaultpicurl"):
            pics["__default__"] = info["defaultpicurl"]
        self.j.put("userpics", pics)
        self.moods = {str(m["id"]): m["name"] for m in info.get("moods") or []}
        self.pics = pics
        self.j.update_meta(
            username=self.user, source="api", userid=info.get("userid"),
            fullname=info.get("fullname"), archived_at=int(time.time()),
            url=journal_base_url(self.user),
        )
        # a web session (cookie) for comment export and friends-locked pages
        ljsession = self.api.sessiongenerate()
        cookies = session_cookies(ljsession)
        for k, v in cookies.items():
            self.http.set_cookie(k, v)
        self.archive.save_session(self.user, cookies)
        self.log("  logged in; session saved for later web fetches")
        return info

    # ------------------------------------------------------------------
    def _syncitems(self) -> dict[int, str]:
        """All entry itemids with their last-modified times (one call per ~500)."""
        items: dict[int, str] = {}
        lastsync = ""
        while True:
            params = {"lastsync": lastsync} if lastsync else {}
            r = self.api.call("syncitems", params)
            got = r.get("syncitems") or []
            newest = lastsync
            for it in got:
                kind, _, num = it["item"].partition("-")
                if kind == "L":
                    items[int(num)] = it["time"]
                newest = max(newest, it["time"])
            self.log(f"  syncitems: {len(items)} entries listed so far")
            if not got or newest == lastsync or int(r.get("count", 0)) >= int(r.get("total", 0)):
                break
            lastsync = newest
        return items

    def entries(self):
        st = self.j.state
        known: dict = {} if self.full else st.get("item_times", {})
        self.log("Listing entries (syncitems)…")
        items = self._syncitems()
        need = {i: t for i, t in items.items() if known.get(str(i)) != t}
        self.log(f"  {len(items)} entries in journal, {len(need)} new or changed")
        if not need:
            return
        fetched: dict[int, dict] = {}

        # Bulk: getevents(selecttype=syncitems) returns many entries per call,
        # everything modified after lastsync. Walk forward through time.
        remaining = dict(need)
        stalls = 0
        while remaining and stalls < 2:
            lastsync = _minus_one_second(min(remaining.values()))
            try:
                r = self.api.call("getevents", {
                    "selecttype": "syncitems", "lastsync": lastsync,
                    "lineendings": "unix",
                })
            except LJError as e:
                self.log(f"  bulk fetch failed ({e}); falling back to one at a time")
                break
            events = r.get("events") or []
            progress = 0
            for ev in events:
                iid = int(ev["itemid"])
                if iid not in fetched:
                    fetched[iid] = ev
                if iid in remaining:
                    del remaining[iid]
                    progress += 1
            self.log(f"  fetched {len(fetched)}/{len(need)} entries")
            stalls = 0 if progress else stalls + 1

        for iid in sorted(remaining):
            r = self.api.call("getevents", {"selecttype": "one", "itemid": iid,
                                            "lineendings": "unix"})
            for ev in r.get("events") or []:
                fetched[int(ev["itemid"])] = ev
            self.log(f"  fetched entry {iid} individually")

        for iid, ev in fetched.items():
            self.j.raw(f"events/{iid}.json", json.dumps(ev, ensure_ascii=False, indent=1))
            e = normalize_api_entry(ev, self.user, self.moods, self.pics)
            self.j.save_entry(e, force=True)
            if iid in items:
                known[str(iid)] = items[iid]
        st["item_times"] = known
        # entries deleted on LJ since last time are kept locally, but noted
        gone = set(known) - {str(i) for i in items}
        st["deleted_on_lj"] = sorted(int(x) for x in gone)
        self.j.save_state(st)
        missing = set(need) - set(fetched)
        if missing:
            self.log(f"  WARNING: could not fetch {len(missing)} entries: {sorted(missing)[:20]}")

    # ------------------------------------------------------------------
    def comments(self):
        self.log("Exporting comments (export_comments.bml)…")
        st = self.j.state
        exp = CommentExporter(self.http)
        meta, users, maxid = exp.meta(0, on_raw=lambda n, b: self.j.raw(f"comments/{n}", b))
        known_users = self.j.get("users", {}) or {}
        known_users.update(users)
        self.j.put("users", known_users)
        self.log(f"  {len(meta)} comments on record (max id {maxid})")

        bodies = {} if self.full else {int(k): v for k, v in (self.j.get("comment_bodies", {}) or {}).items()}
        start = 0 if self.full else (max(bodies) + 1 if bodies else 0)
        if start <= maxid:
            new = exp.bodies(
                start, maxid, on_raw=lambda n, b: self.j.raw(f"comments/{n}", b),
                progress=lambda n, last, mx: self.log(f"  comment bodies: up to #{last} of {mx}"),
            )
            bodies.update(new)
        # states change (screened/unscreened/deleted); refresh from meta
        for cid, m in meta.items():
            if cid in bodies:
                bodies[cid]["state"] = m["state"]
        self.j.put("comment_bodies", {str(k): v for k, v in sorted(bodies.items())})
        st["comments_maxid"] = maxid
        self.j.save_state(st)
        self._distribute_comments(bodies, known_users)

    def _distribute_comments(self, bodies: dict, users: dict):
        # jitemid -> anum so we can build public ids (dtalkid = jtalkid*256 + anum)
        anum = {}
        for e in self.j.entries():
            anum[e["itemid"]] = e["anum"]
        per_entry: dict[int, list] = {}
        for cid, c in bodies.items():
            a = anum.get(c["jitemid"])
            if a is None:
                continue  # comment on an entry we don't have (deleted entry)
            ditemid = c["jitemid"] * 256 + a
            poster = users.get(str(c.get("posterid"))) if c.get("posterid") else None
            per_entry.setdefault(ditemid, []).append({
                "source": "api",
                "id": cid * 256 + a,
                "jtalkid": cid,
                "parent": (c["parentid"] * 256 + a) if c.get("parentid") else 0,
                "ditemid": ditemid,
                "poster": poster,
                "subject": c.get("subject", ""),
                "body": c.get("body", ""),
                "body_format": "lj",
                "date": c.get("date", ""),
                "state": c.get("state", "A"),
                "userpic_keyword": (c.get("props") or {}).get("picture_keyword", ""),
                "userpic_url": "",
                "props": c.get("props") or {},
            })
        for ditemid, cs in per_entry.items():
            merged, _, _ = self.j.save_comments(ditemid, cs)
            e = self.j.load_entry(ditemid)
            if e is not None:
                e["comment_count"] = live_count(merged)
                self.j.save_entry(e, force=True)
        self.log(f"  comments filed under {len(per_entry)} entries")

    # ------------------------------------------------------------------
    def commenter_userpics(self):
        """The comment export gives each comment's userpic *keyword*; look up
        each commenter's userpic list once (allpics page) to get the image."""
        from .web import parse_allpics
        from .http import FetchError
        cache = self.archive.root / "_userpics"
        cache.mkdir(exist_ok=True)
        posters = set()
        all_comments = {}
        for d in self.j.entry_ids():
            cs = self.j.load_comments(d)
            if cs:
                all_comments[d] = cs
                posters |= {c["poster"] for c in cs if c.get("poster") and not c["poster"].startswith("ext_")}
        self.log(f"Looking up userpics for {len(posters)} commenters…")
        maps = {}
        for n, p in enumerate(sorted(posters), 1):
            f = cache / f"{p}.json"
            if f.exists():
                maps[p] = json.loads(f.read_text())
                continue
            try:
                r = self.http.get(f"https://www.livejournal.com/allpics.bml?user={p}")
                maps[p] = parse_allpics(r.text) if r.status == 200 else {}
            except FetchError:
                maps[p] = {}
            f.write_text(json.dumps(maps[p]))
            if n % 25 == 0:
                self.log(f"  {n}/{len(posters)}")
        for d, cs in all_comments.items():
            changed = False
            for c in cs:
                m = maps.get(c.get("poster") or "")
                if not m or c.get("userpic_url"):
                    continue
                c["userpic_url"] = m.get(c.get("userpic_keyword") or "") or m.get("__default__", "")
                changed = changed or bool(c["userpic_url"])
            if changed:
                self.j.save_comments(d, cs)

    def enrich_embeds(self):
        """Entries with <lj-embed> or polls: grab LJ's rendered body (one page
        load each) so videos/polls display. Everything else stays API-only."""
        from .web import parse_entry_page
        todo = [e for e in self.j.entries()
                if e.get("source") == "api" and not e.get("rendered_body")
                and re.search(r"<lj-(embed|poll)", e.get("body") or "", re.I)]
        if not todo:
            return
        self.log(f"Fetching rendered pages for {len(todo)} entries with embeds/polls…")
        for e in todo:
            try:
                r = self.http.get(e["url"], cache=True)
            except Exception as ex:  # noqa: BLE001
                self.log(f"  {e['ditemid']}: {ex}")
                continue
            parsed = parse_entry_page(r.text, self.user, e["ditemid"], e["url"]) if r.status == 200 else None
            if parsed and parsed["entry"]["body"]:
                e["rendered_body"] = parsed["entry"]["body"]
                self.j.save_entry(e, force=True)

    # ------------------------------------------------------------------
    def connections(self):
        self.log("Fetching friends, groups and tags…")
        fr = self.api.call("getfriends", {"includefriendof": 1, "includegroups": 1,
                                          "includebdays": 0})
        self.j.raw("getfriends.json", json.dumps(fr, ensure_ascii=False, indent=1))
        self.j.put("friends", {
            "source": "api",
            "friends": fr.get("friends") or [],
            "friendof": fr.get("friendofs") or [],
            "groups": fr.get("friendgroups") or [],
        })
        try:
            tags = self.api.call("getusertags", {})
            self.j.put("tags", tags.get("tags") or [])
        except LJError as e:
            self.log(f"  (tags unavailable: {e})")
        self.log(f"  {len(fr.get('friends') or [])} friends, {len(fr.get('friendofs') or [])} friend-of, "
                 f"{len(fr.get('friendgroups') or [])} groups")
