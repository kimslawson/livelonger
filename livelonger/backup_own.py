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
from . import webauth
from .ljapi import UNSUPPORTED_FAULTS, CommentExporter, LJClient, LJError, NotLoggedIn
from .store import Archive, Journal, journal_base_url, live_count, norm_user

TS_FMT = "%Y-%m-%d %H:%M:%S"


def _minus_one_second(ts: str) -> str:
    try:
        return (dt.datetime.strptime(ts, TS_FMT) - dt.timedelta(seconds=1)).strftime(TS_FMT)
    except ValueError:
        return ts


def txt(v) -> str:
    """LJ's XML-RPC sends anything that looks like a number as a number (a tag
    list of just "411", a subject of "2008"...). Text fields are always text."""
    if v is None:
        return ""
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    return str(v)


def normalize_api_entry(ev: dict, user: str, moods: dict, pics: dict) -> dict:
    props = ev.get("props") or {}
    itemid, anum = int(ev["itemid"]), int(ev.get("anum") or 0)
    ditemid = itemid * 256 + anum
    tags = [t.strip() for t in txt(props.get("taglist")).split(",") if t.strip()]
    mood = txt(props.get("current_mood")) or moods.get(txt(props.get("current_moodid")), "")
    kw = txt(props.get("picture_keyword"))
    return {
        "source": "api",
        "journal": user,
        "itemid": itemid,
        "anum": anum,
        "ditemid": ditemid,
        "url": txt(ev.get("url")) or f"{journal_base_url(user)}{ditemid}.html",
        "poster": norm_user(txt(ev.get("poster")) or user),
        "eventtime": txt(ev.get("eventtime")),
        "subject": txt(ev.get("subject")),
        "body": txt(ev.get("event")),
        "body_format": "lj",
        "security": txt(ev.get("security")) or "public",
        "allowmask": int(ev.get("allowmask") or 0),
        "tags": tags,
        "mood": mood,
        "music": txt(props.get("current_music")),
        "location": txt(props.get("current_location")),
        "userpic_keyword": kw,
        "userpic_url": pics.get(kw) or pics.get("__default__") or "",
        "preformatted": bool(int(props.get("opt_preformatted") or 0)),
        "props": props,
        "reply_count": int(ev.get("reply_count") or props.get("replycount") or 0),
    }


def _people(lst) -> list[dict]:
    out = []
    for f in lst or []:
        f = dict(f)
        for k in ("username", "fullname", "type"):
            if k in f:
                f[k] = txt(f[k])
        out.append(f)
    return out


class OwnBackup:
    def __init__(self, archive: Archive, http: PoliteClient, user: str, password=None,
                 log=print, full: bool = False):
        """password: a string, or a zero-argument callable asked only if a
        password login turns out to be necessary."""
        self.archive = archive
        self.http = http
        self.user = norm_user(user)
        self.api = LJClient(http, self.user, log=log, password=password if isinstance(password, str) else None,
                            password_provider=None if isinstance(password, str) else password,
                            on_new_session=self._session_made)
        self.j: Journal = archive.journal(self.user)
        self.log = log
        self.full = full
        self.problem_entries: list[int] = []

    def _session_made(self, cookies):
        if self.archive.session_info(self.user).get("source") == "browser":
            return  # never replace working browser cookies with an API session
        self.archive.save_session(self.user, cookies, source="api")

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
        st = self.j.state
        sess = self.archive.session_info(self.user)
        saved = sess.get("cookies")
        if saved:  # still good for the website (comment export, locked pages)
            webauth.apply(self.http, saved, sess.get("variant") or "all")
        if sess.get("source") == "browser":
            # browser cookies are for the website; the API gets password logins,
            # and we never make an API session that would replace them
            self.api.cookie_auth_ok = False
            self.log(f"Logging in to the LiveJournal API as {self.user} (website: browser cookies)…")
        elif st.get("api_cookie_auth") is False:
            # learned on an earlier run: LJ's API won't take session cookies,
            # so don't spend (failed) login attempts finding that out again
            self.api.cookie_auth_ok = False
            self.log(f"Logging in to the LiveJournal API as {self.user}…")
        elif saved:
            self.log(f"Connecting to the LiveJournal API as {self.user} (saved session)…")
            self.api.use_session()
        else:
            self.log(f"Logging in to the LiveJournal API as {self.user}…")
            self.api.new_session()
        info = self.api.login()
        if self.api.cookie_auth_ok is not None and st.get("api_cookie_auth") != self.api.cookie_auth_ok:
            st = self.j.state
            st["api_cookie_auth"] = self.api.cookie_auth_ok
            self.j.save_state(st)
        self.j.raw("login.json", json.dumps(info, ensure_ascii=False, indent=1))
        pics = {}
        for kw, url in zip(info.get("pickws") or [], info.get("pickwurls") or []):
            pics[txt(kw)] = txt(url)
        if info.get("defaultpicurl"):
            pics["__default__"] = txt(info["defaultpicurl"])
        self.j.put("userpics", pics)
        self.moods = {txt(m["id"]): txt(m["name"]) for m in info.get("moods") or []}
        self.pics = pics
        self.j.update_meta(
            username=self.user, source="api", userid=info.get("userid"),
            fullname=txt(info.get("fullname")), archived_at=int(time.time()),
            url=journal_base_url(self.user),
        )
        how = "session" if self.api.session else "password logins (LJ's API doesn't take sessions)"
        self.log(f"  connected via {how}")
        return info

    # ------------------------------------------------------------------
    def _syncitems(self, since: str = "") -> dict[int, str]:
        """Entry itemids with last-modified times, changed after `since`
        (everything if empty). ~300 per call; each call is a password login
        now that LJ's API refuses sessions, so re-runs list only changes."""
        items: dict[int, str] = {}
        lastsync = since
        while True:
            params = {"lastsync": lastsync} if lastsync else {}
            r = self.api.call("syncitems", params)
            got = r.get("syncitems") or []
            newest = lastsync
            for it in got:
                kind, _, num = str(it["item"]).partition("-")
                if kind == "L":
                    items[int(num)] = str(it["time"])
                newest = max(newest, str(it["time"]))
            self.log(f"  syncitems: {len(items)} {'changed ' if since else ''}entries listed so far")
            if not got or newest == lastsync or int(r.get("count", 0)) >= int(r.get("total", 0)):
                break
            lastsync = newest
        return items

    def entries(self):
        st = self.j.state
        known: dict = {} if self.full else dict(st.get("item_times", {}))
        since = "" if self.full else st.get("lastsync", "")
        self.log("Listing entries (syncitems)" + (f", changes since {since}…" if since else "…"))
        listed = self._syncitems(since)
        if since:
            items = {int(k): v for k, v in known.items()}
            items.update(listed)
        else:
            items = listed
            st["deleted_on_lj"] = sorted(int(x) for x in set(known) - {str(i) for i in items})
        need = {i: t for i, t in items.items() if known.get(str(i)) != t}
        self.log(f"  {len(items)} entries in journal, {len(need)} new or changed")
        newest = max(items.values(), default="")
        if not need:
            st["lastsync"] = newest
            self.j.save_state(st)
            return
        remaining = dict(need)
        done = 0
        unsaved = 0

        def flush():
            nonlocal unsaved
            st["item_times"] = known
            self.j.save_state(st)
            unsaved = 0

        def store(events):
            """Save entries the moment they arrive, so an interruption loses nothing.
            (The sync bookkeeping is flushed every 25 entries and on exit; if it
            lags, a re-run just re-requests those few entries.)"""
            nonlocal done, unsaved
            got = 0
            for ev in events:
                iid = int(ev["itemid"])
                self.j.raw(f"events/{iid}.json", json.dumps(ev, ensure_ascii=False, indent=1))
                try:
                    self.j.save_entry(normalize_api_entry(ev, self.user, self.moods, self.pics), force=True)
                except Exception as e:  # noqa: BLE001 - one odd entry mustn't sink the run
                    self.log(f"  entry {iid}: couldn't process ({type(e).__name__}: {e}); "
                             "raw data kept, will retry next run")
                    remaining.pop(iid, None)
                    self.problem_entries.append(iid)
                    continue
                if iid in items:
                    known[str(iid)] = items[iid]
                    unsaved += 1
                if remaining.pop(iid, None) is not None:
                    got += 1
            done += got
            if unsaved >= 25:
                flush()
            return got

        try:
            self._fetch_entries(remaining, need, store, lambda: done)
        finally:
            # only move the "listed up to" mark once everything listed is safely in
            if not remaining and not self.problem_entries:
                st["lastsync"] = newest
            flush()
        if remaining:
            self.log(f"  WARNING: LJ returned nothing for {len(remaining)} entries: {sorted(remaining)[:20]}")

    def _fetch_entries(self, remaining, need, store, done):
        # 1. Walk forward through modification times: getevents(syncitems)
        #    returns up to ~100 entries changed after `lastsync` per call.
        #    If more than a batch share one timestamp (LJ bulk-touched many old
        #    entries at once), this stops making progress: move on.
        while remaining:
            lastsync = _minus_one_second(min(remaining.values()))
            try:
                r = self.api.call("getevents", {"selecttype": "syncitems", "lastsync": lastsync,
                                                "lineendings": "unix"})
            except LJError as e:
                self.log(f"  bulk fetch by date failed ({e})")
                break
            if not store(r.get("events") or []):
                self.log(f"  {len(remaining)} entries share modification times; fetching those by id")
                break
            self.log(f"  fetched {done()}/{len(need)} entries")

        # 2. Ask for specific entries, 100 per call. Only an "unsupported
        #    argument" answer turns this off; other errors skip one chunk,
        #    whose entries then fall through to step 3.
        ids = sorted(remaining)
        while ids:
            chunk, ids = ids[:100], ids[100:]
            try:
                r = self.api.call("getevents", {"selecttype": "multiple",
                                                "itemids": ",".join(map(str, chunk)),
                                                "lineendings": "unix"})
            except LJError as e:
                if e.code in UNSUPPORTED_FAULTS:
                    self.log(f"  LJ doesn't support fetching by id list ({e}); going one at a time")
                    break
                self.log(f"  a batch of {len(chunk)} failed ({e}); will retry those individually")
                continue
            store(r.get("events") or [])
            self.log(f"  fetched {done()}/{len(need)} entries")

        # 3. Last resort: one per call (session-authenticated, so no logins).
        stragglers = sorted(remaining)
        for n, iid in enumerate(stragglers, 1):
            try:
                r = self.api.call("getevents", {"selecttype": "one", "itemid": iid,
                                                "lineendings": "unix"})
            except LJError as e:
                self.log(f"  entry {iid}: {e}")
                continue
            store(r.get("events") or [])
            if n % 25 == 0 or n == len(stragglers):
                self.log(f"  fetched {done()}/{len(need)} entries")

    # ------------------------------------------------------------------
    def comments(self):
        self.log("Exporting comments (export_comments.bml)…")
        st = self.j.state
        exp = CommentExporter(self.http)
        raw = lambda n, b: self.j.raw(f"comments/{n}", b)  # noqa: E731
        meta, users, maxid = self._export_meta_logged_in(exp, raw)
        known_users = self.j.get("users", {}) or {}
        known_users.update(users)
        self.j.put("users", known_users)
        self.log(f"  {len(meta)} comments on record (max id {maxid})")

        bodies = {} if self.full else {int(k): v for k, v in (self.j.get("comment_bodies", {}) or {}).items()}
        start = 0 if self.full else (max(bodies) + 1 if bodies else 0)
        def persist(batch):
            # saved after every page (~1000 comments): an interruption costs one page
            bodies.update(batch)
            self.j.put("comment_bodies", {str(k): v for k, v in sorted(bodies.items())})

        if start <= maxid:
            exp.bodies(
                start, maxid, on_raw=lambda n, b: self.j.raw(f"comments/{n}", b),
                progress=lambda n, last, mx: self.log(f"  comment bodies: up to #{last} of {mx}"),
                on_batch=persist,
            )
        for note in exp.salvage_notes:
            self.log(f"  note: {note}")
        # states change (screened/unscreened/deleted); refresh from meta
        for cid, m in meta.items():
            if cid in bodies:
                bodies[cid]["state"] = m["state"]
        self.j.put("comment_bodies", {str(k): v for k, v in sorted(bodies.items())})
        st["comments_maxid"] = maxid
        self.j.save_state(st)
        self._distribute_comments(bodies, known_users)

    def _export_meta_logged_in(self, exp, raw):
        """Run the first export request, finding a cookie style the website
        accepts (one request per style, no password logins)."""
        sess = self.archive.session_info(self.user)
        if not sess.get("cookies"):
            raise LJError("no session saved; run `livelonger login "
                          f"{self.user} --cookies FILE` (see README)")
        result = {}

        def probe():
            try:
                result["meta"] = exp.meta(0, on_raw=raw)
                return True
            except NotLoggedIn:
                return False

        variant = webauth.find_working(self.http, sess["cookies"], probe,
                                       source=sess.get("source", "api"), first=sess.get("variant"))
        if variant is None and sess.get("source") != "browser" and sess.get("variant"):
            # a style that used to work stopped: the session probably expired
            self.log("  (saved session expired; making a new one)")
            self.api.new_session()
            sess = self.archive.session_info(self.user)
            variant = webauth.find_working(self.http, sess["cookies"], probe, first=sess.get("variant"))
        if variant is None:
            tried = "your browser cookies" if sess.get("source") == "browser" else "3 cookie styles"
            raise LJError("the website didn't accept our login.\n" +
                          webauth.BROWSER_HELP.format(tried=tried, user=self.user))
        if variant != sess.get("variant"):
            self.archive.save_session(self.user, sess["cookies"], source=sess.get("source", "api"),
                                      variant=variant)
            self.log(f"  (website login works with cookie style '{variant}'; remembered)")
        return result["meta"]

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
        from .web import parse_entry_page, site_layout
        todo = [e for e in self.j.entries()
                if e.get("source") == "api" and not e.get("rendered_body")
                and re.search(r"<lj-(embed|poll)", e.get("body") or "", re.I)]
        if not todo:
            return
        self.log(f"Fetching rendered pages for {len(todo)} entries with embeds/polls…")
        if any(e.get("security") != "public" for e in todo):
            if not webauth.ensure_journal_login(self.http, self.archive, self.user, self.user, self.log):
                todo = [e for e in todo if e.get("security") == "public"]
        for e in todo:
            try:
                r = self.http.get(site_layout(e["url"]), cache=True)
            except Exception as ex:  # noqa: BLE001
                self.log(f"  {e['ditemid']}: {ex}")
                continue
            parsed = parse_entry_page(r.text, self.user, e["ditemid"], e["url"]) if r.status == 200 else None
            if parsed and parsed["entry"]["body"]:
                e["rendered_body"] = parsed["entry"]["body"]
                self.j.save_entry(e, force=True)
            else:
                self.log(f"  {e['ditemid']}: couldn't read the rendered entry"
                         f" ({'login page' if 'login.bml' in r.url else 'unrecognised page layout'})")

    # ------------------------------------------------------------------
    def connections(self):
        self.log("Fetching friends, groups and tags…")
        fr = self.api.call("getfriends", {"includefriendof": 1, "includegroups": 1,
                                          "includebdays": 0})
        self.j.raw("getfriends.json", json.dumps(fr, ensure_ascii=False, indent=1))
        self.j.put("friends", {
            "source": "api",
            "friends": _people(fr.get("friends")),
            "friendof": _people(fr.get("friendofs")),
            "groups": [dict(g, name=txt(g.get("name"))) for g in fr.get("friendgroups") or []],
        })
        try:
            tags = self.api.call("getusertags", {})
            self.j.put("tags", [dict(t, name=txt(t.get("name"))) for t in tags.get("tags") or []])
        except LJError as e:
            self.log(f"  (tags unavailable: {e})")
        self.log(f"  {len(fr.get('friends') or [])} friends, {len(fr.get('friendofs') or [])} friend-of, "
                 f"{len(fr.get('friendgroups') or [])} groups")
