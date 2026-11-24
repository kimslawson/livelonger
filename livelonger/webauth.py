"""Being logged in to LiveJournal's *website* (comment export, locked pages).

A session made through the API (sessiongenerate) gives us `ljsession`. The
modern site also uses `ljmastersession` / `ljloggedin`, which we derive. Which
combination the site accepts isn't documented, so we try each (one cheap
request apiece, no password logins) and remember the one that works. If none
does, cookies copied from a logged-in browser always work.
"""
from __future__ import annotations

from .http import PoliteClient

AUTH = ("ljsession", "ljmastersession", "ljloggedin")
VARIANTS = {
    "all": AUTH,
    "ljsession": ("ljsession",),
    "master": ("ljmastersession", "ljloggedin"),
}

BROWSER_HELP = """\
LiveJournal's website won't accept the login livelonger makes through the API
(tried {tried}). Give it your browser's LiveJournal cookies instead. This is
a one-time step and needs no password:
  1. Log in at https://www.livejournal.com in your browser.
  2. Open the developer tools (Chrome/Edge: Cmd-Opt-I; Safari: enable the
     Develop menu first), go to the Network tab, and reload the page.
  3. Click the first request to www.livejournal.com. Under "Request Headers",
     copy the whole value of `Cookie:` into a file, e.g. lj-cookies.txt.
  4. Run:  livelonger login {user} --cookies lj-cookies.txt
     then delete lj-cookies.txt (it's as good as a password while it lasts).
Then re-run the backup."""


def apply(http: PoliteClient, cookies: dict, variant: str | None = "all"):
    """Put a session's cookies in the jar, keeping only the auth cookies of
    the given variant (non-auth cookies, e.g. from a browser, always kept)."""
    keep = VARIANTS.get(variant or "all", AUTH)
    for name in AUTH:
        http.remove_cookie(name)
    for k, v in (cookies or {}).items():
        if k in AUTH and k not in keep:
            continue
        http.set_cookie(k, v)


def find_working(http: PoliteClient, cookies: dict, probe, source: str = "api",
                 first: str | None = None) -> str | None:
    """Try cookie variants until probe() reports we're logged in."""
    if source == "browser":
        order = ["all"]
    else:
        order = ([first] if first in VARIANTS else []) + [v for v in VARIANTS if v != first]
    for v in order:
        apply(http, cookies, v)
        if probe():
            return v
    apply(http, cookies, first or "all")
    return None


def journal_logged_in(http: PoliteClient, journal: str, as_user: str) -> bool:
    """Do `journal`'s own pages (its subdomain) see us as as_user? LJ keeps a
    separate per-journal login cookie, obtained through a redirect dance."""
    from .http import FetchError
    from .store import journal_base_url, norm_user
    from .web import site_json
    try:
        r = http.get(journal_base_url(journal), cache=False)
    except FetchError as e:
        http.log(f"  ({journal}'s page: {e})")
        return False
    remote = site_json(r.text, "remote") if r.status == 200 else None
    return isinstance(remote, dict) and norm_user(remote.get("username") or "") == norm_user(as_user)


def ensure_journal_login(http: PoliteClient, archive, as_user: str, journal: str, log=print) -> bool:
    """Make sure `journal`'s pages see us as logged in, finding (and
    remembering) a cookie style that works. Returns False, with advice, if
    none does; callers then carry on with what's publicly visible."""
    sess = archive.session_info(as_user)
    if not sess.get("cookies"):
        return False
    variant = find_working(http, sess["cookies"], lambda: journal_logged_in(http, journal, as_user),
                           source=sess.get("source", "api"), first=sess.get("journal_variant"))
    if variant is None:
        tried = "your browser cookies" if sess.get("source") == "browser" else "3 cookie styles"
        log(f"  WARNING: {journal}'s pages don't see you as logged in, so locked entries\n"
            "  will be missing (only public ones are visible).\n" +
            "\n".join("  " + line for line in BROWSER_HELP.format(tried=tried, user=as_user).splitlines()))
        return False
    if variant != sess.get("journal_variant"):
        archive.update_session(as_user, journal_variant=variant)
    log(f"  {journal}'s pages see you as {as_user}")
    return True
