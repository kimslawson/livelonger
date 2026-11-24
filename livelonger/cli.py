"""livelonger command line."""
from __future__ import annotations

import argparse
import getpass
import os
import shutil
import sys
from pathlib import Path

from . import __version__
from .config import DEFAULT_PATH, load, write_template
from .http import Blocked, FetchError, PoliteClient
from .store import Archive, norm_user, read_json


def log(msg):
    print(msg, flush=True)


def make_client(cfg, archive: Archive) -> PoliteClient:
    cfg.require_contact()
    return PoliteClient(cfg.user_agent, delay=cfg.delay, other_delay=cfg.media_delay,
                        cache_dir=archive.cache_dir, max_retries=cfg.max_retries, log=log)


def get_password(user: str, env: str | None) -> str:
    if env and os.environ.get(env):
        return os.environ[env]
    if os.environ.get("LJ_PASSWORD"):
        return os.environ["LJ_PASSWORD"]
    return getpass.getpass(f"LiveJournal password for {user} (not stored): ")


# ---------------------------------------------------------------------------

def cmd_init(args, cfg):
    p = Path(args.config or DEFAULT_PATH)
    if p.exists() and not args.force:
        sys.exit(f"{p} already exists (use --force to overwrite)")
    contact = args.contact or input(
        "Contact email for the User-Agent header (LJ's bot policy requires one): ").strip()
    write_template(p, contact)
    log(f"Wrote {p}. Next: `livelonger backup YOUR_LJ_USERNAME`")


FAILED_STEPS: list[str] = []


def _phase(name, fn, *a, **kw):
    """Run one stage of a backup. A failure in one stage is reported and the
    next stage still runs; only a StopRun/Blocked (or Ctrl-C) ends the run."""
    import traceback
    from .ljapi import LJError
    try:
        return fn(*a, **kw)
    except (LJError, FetchError) as e:
        FAILED_STEPS.append(name)
        log(f"  {name} failed: {e}\n  (continuing with the next step; re-run later to retry this one)")
    except (Blocked, KeyboardInterrupt):
        raise
    except Exception as e:  # noqa: BLE001 - a bug in one step mustn't lose the others
        FAILED_STEPS.append(name)
        tb = traceback.extract_tb(e.__traceback__)[-1]
        log(f"  {name} hit a bug: {type(e).__name__}: {e}\n"
            f"  (at {Path(tb.filename).name}:{tb.lineno}; continuing with the next step. "
            "Please report this.)")


def _summary(http, extra=""):
    http.close()
    log(f"\nDone. {http.request_count} requests over {http.connection_count} connections{extra}.")
    if FAILED_STEPS:
        log(f"Steps that failed (re-run to retry): {', '.join(FAILED_STEPS)}")
        sys.exit(1)


def cmd_backup(args, cfg):
    from .backup_own import OwnBackup
    from .ljapi import LJError
    archive = Archive(cfg.archive_dir)
    http = make_client(cfg, archive)
    user = norm_user(args.user)
    b = OwnBackup(archive, http, user, lambda: get_password(user, args.password_env),
                  log=log, full=args.full)
    try:
        b.login()
    except LJError as e:
        sys.exit(f"Login failed: {e}\n(Forgot the password? https://www.livejournal.com/lostinfo.bml)")
    _phase("entries", b.entries)
    if not args.no_comments:
        if _phase("comment export", b.comments) is None and not b.j.get("comment_bodies"):
            log(f"  You can also get comments from the web pages: livelonger fetch {user} --as {user}")
    if not args.no_connections:
        _phase("friends/tags", b.connections)
    # profile + userpics + style come from public pages
    from .web import WebFetcher
    _phase("profile", WebFetcher(archive, http, user, log=log).profile)
    if not args.no_embeds:
        _phase("embeds", b.enrich_embeds)
    if not args.no_comments and not args.no_commenter_userpics:
        _phase("commenter userpics", b.commenter_userpics)
    if not args.no_theme:
        _theme(cfg, archive, http, user)
    if not args.no_media:
        from .media import download_all
        _phase("media", download_all, archive, http, users=[user], log=log)
    if b.problem_entries:
        log(f"  {len(b.problem_entries)} entries couldn't be processed: {b.problem_entries[:20]}")
    _summary(http, f", {b.api.password_logins} password login(s). "
                   "Build the site with `livelonger build`")


def cmd_login(args, cfg):
    archive = Archive(cfg.archive_dir)
    http = make_client(cfg, archive)
    user = norm_user(args.user)
    if args.cookies:
        # cookies from a logged-in browser: no password, and the website accepts them
        http.load_cookie_file(Path(args.cookies))
        cookies = http.cookies()
        from .web import whoami
        who = whoami(http, user)
        if who is None:
            sys.exit("LiveJournal doesn't see those cookies as logged in. Copy them again from a "
                     "browser where you're logged in (see README).")
        if who != user:
            log(f"Note: those cookies are logged in as {who}, not {user}; saving them for {who}.")
            user = who
        archive.save_session(user, cookies, source="browser", variant="all")
        log(f"Logged in as {user} via browser cookies; saved. You can delete {args.cookies} now.")
        return
    from .ljapi import LJClient, LJError
    api = LJClient(http, user, log=log, password_provider=lambda: get_password(user, args.password_env),
                   on_new_session=lambda c: archive.save_session(user, c, source="api"))
    try:
        api.new_session()  # exactly one password login
    except LJError as e:
        sys.exit(f"Login failed: {e}")
    log(f"Session saved for {user}. Use `livelonger fetch SOMEONE --as {user}`.")


def _apply_auth(args, archive, http):
    if getattr(args, "cookies", None):
        http.load_cookie_file(Path(args.cookies))
        log("  using cookies from " + args.cookies)
    elif getattr(args, "as_user", None):
        from . import webauth
        sess = archive.session_info(args.as_user)
        if not sess.get("cookies"):
            sys.exit(f"No saved session for {args.as_user}; run `livelonger login {args.as_user} "
                     "--cookies FILE` first (see README).")
        webauth.apply(http, sess["cookies"], sess.get("variant") or "all")
        log(f"  fetching as {args.as_user}")


def cmd_fetch(args, cfg):
    from .web import WebFetcher
    archive = Archive(cfg.archive_dir)
    http = make_client(cfg, archive)
    _apply_auth(args, archive, http)
    for user in args.users:
        wf = WebFetcher(archive, http, user, log=log, refresh=args.refresh,
                        as_user=None if args.cookies else args.as_user)
        wf.reparse = args.reparse
        if archive.journal(user).meta.get("source") == "api" and not args.entries:
            log(f"{user} has a complete owner backup here (from the API), so there's nothing to\n"
                "  add from web pages; skipping. (`livelonger backup` keeps it up to date.)")
            continue
        years = [int(y) for y in args.years.split(",")] if args.years else None
        only = [int(x) for x in args.entries.split(",")] if args.entries else None
        wf.run(years=years, limit=args.limit, only=only)
        if not args.no_theme:
            _theme(cfg, archive, http, user)
    if not args.no_media:
        from .media import download_all
        _phase("media", download_all, archive, http, users=[norm_user(u) for u in args.users], log=log)
    _summary(http)


def _theme(cfg, archive, http, user):
    from .theme import ThemeCapture
    try:
        ThemeCapture(http, archive.journal(user), log=log).capture()
    except FetchError as e:
        log(f"  (theme capture failed: {e})")


def cmd_theme(args, cfg):
    archive = Archive(cfg.archive_dir)
    http = make_client(cfg, archive)
    for u in args.users:
        _theme(cfg, archive, http, norm_user(u))


def cmd_media(args, cfg):
    from .media import download_all
    archive = Archive(cfg.archive_dir)
    http = make_client(cfg, archive)
    download_all(archive, http, users=[norm_user(u) for u in args.users] or None,
                 retry_failed=args.retry_failed, log=log, include_links=not args.no_linked)


def cmd_build(args, cfg):
    from .site import SiteBuilder
    archive = Archive(cfg.archive_dir)
    out = Path(args.out) if args.out else cfg.site_dir
    if args.public_only and not args.out:
        out = out.with_name(out.name + "-public")
    SiteBuilder(archive, out, theme=args.theme or cfg.theme, public_only=args.public_only,
                log=log).build([norm_user(u) for u in args.users] or None)


def cmd_serve(args, cfg):
    import functools
    import http.server
    d = Path(args.dir) if args.dir else cfg.site_dir
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(d))
    with http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler) as srv:
        log(f"Serving {d} at http://127.0.0.1:{args.port}/ (Ctrl-C to stop)")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass


def cmd_status(args, cfg):
    archive = Archive(cfg.archive_dir)
    js = archive.journals()
    if not js:
        log("Archive is empty.")
    for j in js:
        m = j.meta
        ids = j.entry_ids()
        ncom = sum(len(j.load_comments(d)) for d in ids)
        locked = sum(1 for d in ids if (j.load_entry(d) or {}).get("security") != "public")
        theme = "yes" if j.p("theme", "shell.html").exists() else "no"
        log(f"{j.user:20} {m.get('source', '?'):4} {len(ids):6} entries ({locked} non-public) "
            f"{ncom:7} comments  theme:{theme}")


def cmd_import(args, cfg):
    """Merge another livelonger archive (e.g. a friend's own backup) into ours."""
    archive = Archive(cfg.archive_dir)
    src = Path(args.path)
    dirs = [src] if (src / "journal.json").exists() else [d for d in src.iterdir()
                                                           if (d / "journal.json").exists()]
    from .merge import import_journal
    for d in dirs:
        theirs = read_json(d / "journal.json", {})
        user = norm_user(theirs.get("username") or d.name)
        st = import_journal(d, archive.journal(user), force=args.force)
        if st.get("new_journal"):
            log(f"{user}: new journal, {st['entries_added']} entries ({theirs.get('source')})")
            continue
        side = "theirs" if st.get("preferred_theirs") else "ours"
        log(f"{user}: merged {theirs.get('source')} copy (ties go to {side})\n"
            f"  entries: {st['entries_added']} added, {st['entries_replaced']} replaced by better copies, "
            f"{st['entries_kept']} kept ours, {st['entries_same']} identical, {st['entries_only_ours']} only ours\n"
            f"  comments: {st['comments_added']} added, {st['comments_updated']} updated"
            + ("\n  style: replaced with theirs" if st.get("theme_replaced") else ""))
    # shared media
    m = src / "_media"
    if not m.exists() and (src.parent / "_media").exists():
        m = src.parent / "_media"
    if m.exists():
        from .media import MediaStore
        store = MediaStore(archive)
        idx = read_json(m / "index.json", {}) or {}
        for url, rec in idx.items():
            if rec.get("path") and not store.local(url) and (m / rec["path"]).exists():
                dst = store.dir / rec["path"]
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(m / rec["path"], dst)
                store.index[url] = rec
        store.save()
    log("Rebuild with `livelonger build` to interlink.")


# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(prog="livelonger", description="Archive LiveJournal journals into a browsable local mirror.")
    ap.add_argument("--config", help=f"config file (default {DEFAULT_PATH})")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create livelonger.toml")
    s.add_argument("--contact")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("backup", help="complete backup of a journal you can log in to (API)")
    s.add_argument("user")
    s.add_argument("--password-env", help="read the password from this environment variable")
    s.add_argument("--full", action="store_true", help="re-download everything, not just changes")
    for flag in ("comments", "connections", "media", "theme", "embeds", "commenter-userpics"):
        s.add_argument(f"--no-{flag}", action="store_true")
    s.set_defaults(fn=cmd_backup)

    s = sub.add_parser("login", help="save a web session (comment export, friends-locked entries)")
    s.add_argument("user")
    s.add_argument("--password-env")
    s.add_argument("--cookies", help="cookies from a logged-in browser (Cookie: header or cookies.txt)")
    s.set_defaults(fn=cmd_login)

    s = sub.add_parser("fetch", help="archive someone else's journal from its web pages")
    s.add_argument("users", nargs="+")
    s.add_argument("--as", dest="as_user", help="use your saved session (see friends-locked entries)")
    s.add_argument("--cookies", help="cookies.txt or raw Cookie header from your browser")
    s.add_argument("--refresh", action="store_true", help="re-fetch pages even if cached")
    s.add_argument("--reparse", action="store_true",
                   help="re-read already-saved entries from cached pages (no new requests for them)")
    s.add_argument("--years", help="only these years, e.g. 2004,2005")
    s.add_argument("--entries", help="only these entry ids (the number in NNN.html)")
    s.add_argument("--limit", type=int)
    s.add_argument("--no-media", action="store_true")
    s.add_argument("--no-theme", action="store_true")
    s.set_defaults(fn=cmd_fetch)

    s = sub.add_parser("theme", help="(re)capture journals' LJ styles")
    s.add_argument("users", nargs="+")
    s.set_defaults(fn=cmd_theme)

    s = sub.add_parser("media", help="download images referenced in the archive")
    s.add_argument("users", nargs="*")
    s.add_argument("--retry-failed", action="store_true")
    s.add_argument("--no-linked", action="store_true", help="only inline images, not linked ones")
    s.set_defaults(fn=cmd_media)

    s = sub.add_parser("build", help="build the static site")
    s.add_argument("users", nargs="*")
    s.add_argument("--theme", choices=["original", "classic"])
    s.add_argument("--public-only", action="store_true",
                   help="omit non-public entries and screened comments (for sharing)")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_build)

    s = sub.add_parser("serve", help="serve the built site on localhost")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--dir")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("status", help="what's in the archive")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("import", help="merge another livelonger archive (e.g. a friend's backup)")
    s.add_argument("path")
    s.add_argument("--force", action="store_true",
                   help="on ties (same kind of copy), prefer theirs even if ours is newer")
    s.set_defaults(fn=cmd_import)

    args = ap.parse_args(argv)
    cfg = load(args.config)
    try:
        args.fn(args, cfg)
    except Blocked as e:
        sys.exit(f"\nSTOPPED: {e}")
    except KeyboardInterrupt:
        sys.exit("\nInterrupted. Progress so far is saved; re-run to continue.")


if __name__ == "__main__":
    main()
