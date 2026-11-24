"""On-disk archive layout.

archive/
  _media/index.json, _media/files/...     shared downloaded images
  _sessions/<user>.json                   saved LJ session cookies (mode 600)
  _cache/                                 HTTP cache for web pages
  <journal>/
    journal.json        who/what/when, and how it was archived ("api" or "web")
    profile.json        profile (FOAF + login data)
    userpics.json       keyword -> url
    friends.json        connections (friends, friend-of, groups)
    tags.json
    state.json          sync bookkeeping
    entries/<ditemid>.json
    comments/<ditemid>.json
    raw/                untouched API/export responses, for fidelity
    theme/              captured LJ style (shell.html + css + images)
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

SOURCE_RANK = {"api": 2, "web": 1}


def norm_user(name: str) -> str:
    return (name or "").strip().lower().replace("-", "_")


def journal_base_url(user: str) -> str:
    """https://user.livejournal.com/ — LJ maps _ to - in hostnames; names that
    begin or end with _ live under users.livejournal.com/_name_/."""
    user = norm_user(user)
    if user.startswith("_") or user.endswith("_"):
        return f"https://users.livejournal.com/{user}/"
    return f"https://{user.replace('_', '-')}.livejournal.com/"


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=False))
    os.replace(tmp, path)


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


class Archive:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def media_dir(self) -> Path:
        return self.root / "_media"

    @property
    def cache_dir(self) -> Path:
        return self.root / "_cache"

    @property
    def sessions_dir(self) -> Path:
        d = self.root / "_sessions"
        d.mkdir(exist_ok=True)
        os.chmod(d, 0o700)
        return d

    def journal(self, user: str) -> "Journal":
        return Journal(self.root / norm_user(user), norm_user(user))

    def journals(self) -> list["Journal"]:
        out = []
        for d in sorted(self.root.iterdir()):
            if d.is_dir() and not d.name.startswith("_") and (d / "journal.json").exists():
                out.append(Journal(d, d.name))
        return out

    # sessions -------------------------------------------------------
    def save_session(self, user: str, cookies: dict, source: str = "api", variant: str | None = None):
        """source: "api" (made by sessiongenerate) or "browser" (copied from a
        logged-in browser). variant: which cookie combination LJ's website
        accepted (see webauth.VARIANTS), once known."""
        p = self.sessions_dir / f"{norm_user(user)}.json"
        write_json(p, {"user": norm_user(user), "cookies": cookies, "source": source,
                       "variant": variant, "saved": time.time()})
        os.chmod(p, 0o600)

    def update_session(self, user: str, **fields):
        p = self.sessions_dir / f"{norm_user(user)}.json"
        d = read_json(p) or {}
        if d:
            d.update(fields)
            write_json(p, d)
            os.chmod(p, 0o600)

    def session_info(self, user: str) -> dict:
        return read_json(self.sessions_dir / f"{norm_user(user)}.json") or {}

    def load_session(self, user: str) -> dict | None:
        d = self.session_info(user)
        return d.get("cookies") or None


class Journal:
    def __init__(self, path: Path, user: str):
        self.path = path
        self.user = user

    def p(self, *parts) -> Path:
        return self.path.joinpath(*parts)

    # metadata -------------------------------------------------------
    @property
    def meta(self) -> dict:
        return read_json(self.p("journal.json"), {}) or {}

    def update_meta(self, **kw):
        m = self.meta
        m.update({k: v for k, v in kw.items() if v is not None})
        m.setdefault("username", self.user)
        write_json(self.p("journal.json"), m)

    @property
    def state(self) -> dict:
        return read_json(self.p("state.json"), {}) or {}

    def save_state(self, st: dict):
        write_json(self.p("state.json"), st)

    def get(self, name: str, default=None):
        return read_json(self.p(f"{name}.json"), default)

    def put(self, name: str, data):
        write_json(self.p(f"{name}.json"), data)

    def raw(self, name: str, data: bytes | str):
        path = self.p("raw", name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode() if isinstance(data, str) else data)

    # entries --------------------------------------------------------
    def entry_ids(self) -> list[int]:
        d = self.p("entries")
        if not d.exists():
            return []
        return sorted(int(f.stem) for f in d.glob("*.json") if f.stem.isdigit())

    def load_entry(self, ditemid: int) -> dict | None:
        return read_json(self.p("entries", f"{ditemid}.json"))

    def entries(self) -> list[dict]:
        return [e for e in (self.load_entry(i) for i in self.entry_ids()) if e]

    def save_entry(self, entry: dict, force: bool = False) -> bool:
        """Write an entry unless a better-sourced copy is already there."""
        path = self.p("entries", f"{entry['ditemid']}.json")
        if not force and path.exists():
            old = read_json(path, {}) or {}
            if rank(old.get("source")) > rank(entry.get("source")):
                return False
        write_json(path, entry)
        return True

    def load_comments(self, ditemid: int) -> list[dict]:
        return read_json(self.p("comments", f"{ditemid}.json"), []) or []

    def save_comments(self, ditemid: int, comments: list[dict], prefer_new: bool = True):
        """Merge comments into what's on disk, comment by comment.

        Comments are keyed by their public id (the same number whether they came
        from the API or the web), so copies from different sources line up. For
        each comment the better-sourced copy wins (owner API beats web; a fully
        loaded comment beats a collapsed stub); on a tie the new copy wins if
        prefer_new. Comments only present on disk are kept, so things deleted
        on LJ since an earlier fetch survive in the archive.

        Returns (merged list, number added, number updated).
        """
        old = self.load_comments(ditemid)
        merged, added, updated = merge_comments(old, comments, prefer_new)
        if added or updated or not self.p("comments", f"{ditemid}.json").exists():
            write_json(self.p("comments", f"{ditemid}.json"), merged)
        return merged, added, updated


def rank(source) -> int:
    return SOURCE_RANK.get(source, 0)


def comment_score(c: dict) -> tuple:
    return (rank(c.get("source")), 0 if c.get("loaded") is False else 1)


def merge_comments(old: list[dict], new: list[dict], prefer_new: bool = True):
    by_id = {c["id"]: c for c in old}
    added = updated = 0
    for c in new:
        o = by_id.get(c["id"])
        if o is None:
            by_id[c["id"]] = c
            added += 1
            continue
        if c == o:
            continue
        sn, so = comment_score(c), comment_score(o)
        if sn > so or (sn == so and prefer_new):
            by_id[c["id"]] = c
            updated += 1
    return sorted(by_id.values(), key=lambda c: c["id"]), added, updated


HIDDEN_STATES = {"D", "B"}  # deleted; B = marked as spam by LJ


def live_count(comments: list[dict]) -> int:
    return sum(1 for c in comments if c.get("state") not in HIDDEN_STATES)


_slug_re = re.compile(r"[^a-z0-9]+")


def slug(s: str) -> str:
    s = _slug_re.sub("-", (s or "").lower()).strip("-")
    return s or "x"
