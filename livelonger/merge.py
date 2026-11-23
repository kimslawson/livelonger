"""Merge another livelonger archive into ours, entry by entry.

Precedence, from strongest to weakest:

1. Source: an owner backup ("api") beats a web copy ("web"). This holds per
   entry and per comment, not just per journal.
2. For comments, a fully loaded comment beats a collapsed stub.
3. On a tie, the "preferred side" wins. That's the newer archive, or theirs
   if --force.

Nothing is ever dropped: entries and comments only one side has are kept.
"""
from __future__ import annotations

import shutil
import time
from collections import Counter
from pathlib import Path

from .store import Journal, live_count, rank, read_json, write_json

FRIENDS_RANK = {"api": 2, "foaf": 1}


def archived_time(meta: dict) -> float:
    return max(meta.get("archived_at") or 0, meta.get("web_archived_at") or 0)


def import_journal(src: Path, mine: Journal, force: bool = False) -> Counter:
    theirs = Journal(Path(src), mine.user)
    tm, mm = theirs.meta, mine.meta
    stats: Counter = Counter()

    if not mm:  # nothing here yet: take it wholesale
        shutil.copytree(src, mine.path, dirs_exist_ok=True)
        stats["entries_added"] = len(theirs.entry_ids())
        stats["new_journal"] = 1
        return stats

    tr, mr = rank(tm.get("source")), rank(mm.get("source"))
    prefer_theirs = tr > mr or (tr == mr and (force or archived_time(tm) > archived_time(mm)))
    stats["preferred_theirs"] = int(prefer_theirs)

    # entries + comments ------------------------------------------------
    for d in theirs.entry_ids():
        te, me = theirs.load_entry(d), mine.load_entry(d)
        if me is None:
            write_json(mine.p("entries", f"{d}.json"), te)
            stats["entries_added"] += 1
        elif te == me:
            stats["entries_same"] += 1
        elif rank(te.get("source")) > rank(me.get("source")) or (
                rank(te.get("source")) == rank(me.get("source")) and prefer_theirs):
            write_json(mine.p("entries", f"{d}.json"), te)
            stats["entries_replaced"] += 1
        else:
            stats["entries_kept"] += 1

        tc = theirs.load_comments(d)
        if tc:
            merged, added, updated = mine.save_comments(d, tc, prefer_new=prefer_theirs)
            stats["comments_added"] += added
            stats["comments_updated"] += updated
            e = mine.load_entry(d)
            if e is not None and e.get("comment_count") != live_count(merged):
                e["comment_count"] = live_count(merged)
                write_json(mine.p("entries", f"{d}.json"), e)
    stats["entries_only_ours"] = len(set(mine.entry_ids()) - set(theirs.entry_ids()))

    # per-journal metadata ------------------------------------------------
    def union(name):
        a, b = mine.get(name, {}) or {}, theirs.get(name, {}) or {}
        if b:
            mine.put(name, {**a, **b} if prefer_theirs else {**b, **a})

    union("profile")
    union("userpics")
    union("users")

    tf, mf = theirs.get("friends", {}) or {}, mine.get("friends", {}) or {}
    if tf:
        ft, fm = FRIENDS_RANK.get(tf.get("source"), 0), FRIENDS_RANK.get(mf.get("source"), 0)
        if not mf or ft > fm or (ft == fm and prefer_theirs):
            mine.put("friends", tf)

    tt = theirs.get("tags")
    if tt and (prefer_theirs or not mine.get("tags")):
        mine.put("tags", tt)

    # bookkeeping that only matters to whoever runs incremental backups
    for name in ("state", "comment_bodies"):
        if theirs.get(name) is not None and mine.get(name) is None:
            mine.put(name, theirs.get(name))

    # style: replace ours only with a preferred, usable capture
    if (theirs.p("theme", "shell.html").exists()
            and (prefer_theirs or not mine.p("theme", "shell.html").exists())):
        if mine.p("theme").exists():
            shutil.rmtree(mine.p("theme"))
        shutil.copytree(theirs.p("theme"), mine.p("theme"))
        stats["theme_replaced"] = 1

    # raw responses are evidence: add theirs, never overwrite ours
    if theirs.p("raw").exists():
        for f in theirs.p("raw").rglob("*"):
            if f.is_file():
                dst = mine.p("raw", *f.relative_to(theirs.p("raw")).parts)
                if not dst.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, dst)

    meta = {**mm, **tm} if prefer_theirs else {**tm, **mm}
    meta["source"] = tm.get("source") if tr > mr else mm.get("source")
    meta["archived_at"] = max(mm.get("archived_at") or 0, tm.get("archived_at") or 0) or None
    meta["web_archived_at"] = max(mm.get("web_archived_at") or 0, tm.get("web_archived_at") or 0) or None
    meta["imports"] = (mm.get("imports") or []) + [
        {"from": str(src), "source": tm.get("source"), "at": int(time.time())}]
    write_json(mine.p("journal.json"), {k: v for k, v in meta.items() if v is not None})
    return stats
