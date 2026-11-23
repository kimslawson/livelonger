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


def cmd_backup(args, cfg):
    from .backup_own import OwnBackup
    from .ljapi import LJError
    archive = Archive(cfg.archive_dir)
    http = make_client(cfg, archive)
    user = norm_user(args.user)
    password = get_password(user, args.password_env)
    b = OwnBackup(archive, http, user, password, log=log, full=args.full)
    try:
        b.login()
    except LJError as e:
        sys.exit(f"Login failed: {e}\n(Forgot the password? https://www.livejournal.com/lostinfo.bml)")
    b.entries()
    if not args.no_comments:
        try:
            b.comments()
        except LJError as e:
            log(f"  comment export failed: {e}\n  You can still get comments from the web pages with:\n"
                f"    livelonger fetch {user} --as {user} --refresh")
    if not args.no_connections:
        b.connections()
    # profile + userpics + style come from public pages
    from .web import WebFetcher
    wf = WebFetcher(archive, http, user, log=log)
    try:
        wf.profile()
    except FetchError as e:
        log(f"  (profile: {e})")
    if not args.no_embeds:
        b.enrich_embeds()
    if not args.no_comments and not args.no_commenter_userpics:
        b.commenter_userpics()
    if not args.no_theme:
        _theme(cfg, archive, http, user)
    if not args.no_media:
        from .media import download_all
        download_all(archive, http, users=[user], log=log)
    log(f"\nDone. {http.request_count} requests. Build the site with `livelonger build`.")


def cmd_login(args, cfg):
    from .ljapi import LJClient, LJError, session_cookies
    archive = Archive(cfg.archive_dir)
    http = make_client(cfg, archive)
    user = norm_user(args.user)
    api = LJClient(http, user, password=get_password(user, args.password_env))
    try:
        api.login()
        cookies = session_cookies(api.sessiongenerate())
    except LJError as e:
        sys.exit(f"Login failed: {e}")
    archive.save_session(user, cookies)
    log(f"Session saved for {user}. Use `livelonger fetch SOMEONE --as {user}`.")


def _apply_auth(args, archive, http):
    if getattr(args, "cookies", None):
        http.load_cookie_file(Path(args.cookies))
        log("  using cookies from " + args.cookies)
    elif getattr(args, "as_user", None):
        c = archive.load_session(args.as_user)
        if not c:
            sys.exit(f"No saved session for {args.as_user}; run `livelonger login {args.as_user}` first.")
        for k, v in c.items():
            http.set_cookie(k, v)
        log(f"  fetching as {args.as_user}")


def cmd_fetch(args, cfg):
    from .web import WebFetcher
    archive = Archive(cfg.archive_dir)
    http = make_client(cfg, archive)
    _apply_auth(args, archive, http)
    for user in args.users:
        wf = WebFetcher(archive, http, user, log=log, refresh=args.refresh)
        if archive.journal(user).meta.get("source") == "api":
            log(f"{user} has a complete owner backup here; web copies won't overwrite it.")
        years = [int(y) for y in args.years.split(",")] if args.years else None
        only = [int(x) for x in args.entries.split(",")] if args.entries else None
        wf.run(years=years, limit=args.limit, only=only)
        if not args.no_theme:
            _theme(cfg, archive, http, user)
    if not args.no_media:
        from .media import download_all
        download_all(archive, http, users=[norm_user(u) for u in args.users], log=log)
    log(f"\nDone. {http.request_count} requests.")


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

    s = sub.add_parser("login", help="save a web session (for fetching friends-locked entries)")
    s.add_argument("user")
    s.add_argument("--password-env")
    s.set_defaults(fn=cmd_login)

    s = sub.add_parser("fetch", help="archive someone else's journal from its web pages")
    s.add_argument("users", nargs="+")
    s.add_argument("--as", dest="as_user", help="use your saved session (see friends-locked entries)")
    s.add_argument("--cookies", help="cookies.txt or raw Cookie header from your browser")
    s.add_argument("--refresh", action="store_true", help="re-fetch pages even if cached")
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
