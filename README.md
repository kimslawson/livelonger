# livelonger

*…and prosper.* 🖖

Archive LiveJournal journals, including entries, comments, profile, connections,
userpics, images and the journal's own style, into a **browsable static mirror**
that works offline straight from disk. It stays polite enough that LJ won't
ban you.

* **Your own journal:** a complete backup through LiveJournal's official API.
  It includes public, friends-only, custom-group and private entries, every
  comment (screened ones too), friend groups and tags.
* **Anyone else's journal:** whatever their pages show you. That's public
  entries, plus friends-locked ones if you're logged in and on their list.
* **Interlinking:** links between archived journals (entries, comment threads,
  `<lj user>` tags, profiles) point inside the mirror. If a friend runs their
  own complete backup and hands you the folder, `livelonger import` merges it
  entry by entry (see [Merging archives](#merging-archives)).
* **Theme:** `original` captures the journal's actual S2 style (layout, colours,
  header art, sidebar) and pours the archive into it. `classic` is a clean
  built-in theme with dark mode.

Pure Python 3.11+ standard library. No dependencies.

## Quick start

```sh
uv tool install .            # or: pipx install .   or just: python -m livelonger …
livelonger init              # asks for a contact email (LJ requires one, see below)
livelonger backup riffraff   # prompts for your password; it is never stored
livelonger build             # writes site/
open site/index.html         # or: livelonger serve
```

Run `backup` again any time. It only fetches what changed.

### Someone else's journal

```sh
livelonger fetch poeticdream                  # public entries
livelonger fetch poeticdream --as riffraff    # + friends-locked entries riffraff can see
livelonger fetch poeticdream --as riffraff --years 2004,2005   # just part of it
livelonger fetch poeticdream --as riffraff --entries 123456    # one specific entry
livelonger build
```

`--as` uses the session saved for that account.

### Logging in to the website (comments, locked posts)

The API takes your password. The *website* needs a login session, and it's
the website that serves the comment export and other people's locked posts.
livelonger first tries the session it gets from the API, in a few cookie
combinations, and remembers whichever one works. If the site refuses all of
them, give it your browser's cookies instead. This is a one-time step and
needs no password:

1. Log in at https://www.livejournal.com in your browser.
2. Open the developer tools, go to the Network tab, and reload the page.
3. Click the first request to `www.livejournal.com`. Under Request Headers,
   copy the whole value of `Cookie:` into a file such as `lj-cookies.txt`.
   A Netscape `cookies.txt` export works too.
4. Run `livelonger login riffraff --cookies lj-cookies.txt`. It checks which
   account LJ sees, saves the session, and tells you to delete the file.

Browser cookies are only used on the website. API calls still use your
password, and nothing ever overwrites the saved browser session.

Each journal's own pages (`name.livejournal.com`) need a separate login on top
of `www`, which LJ hands out through a redirect. Before walking a journal,
livelonger checks that the journal's pages see you as logged in. If they
don't, it warns you before spending requests, because otherwise only public
entries would be found.

### Sharing

`livelonger build --public-only` writes `site-public/` without non-public
entries or screened comments. The normal `site/` **contains private entries**,
so don't put it on the open web.

## How LiveJournal access and privacy work (refresher)

Each entry has a security level:

| Level | Who can read it |
|---|---|
| public | everyone |
| friends (`usemask`, mask 1) | people on the author's friends list |
| custom group (`usemask`, other bits) | friends in that particular friend group |
| private | only the author |

* **The API only reads your own journal** (and communities you maintain). So a
  *complete* backup of a journal needs that journal's password.
* **Friends-locked posts by someone else** can be read by anyone they've
  friended and allowed into that group, through the website. That's what
  `fetch --as you` does: your session sees exactly what your browser would.
* **Private posts** can only be read by their author.

Authentication uses LJ's challenge/response login. The client sends
`md5(challenge + md5(password))` and never the password itself. Nothing
password-equivalent is written to disk. Only a session cookie is saved, in
`archive/_sessions/` with owner-only permissions. To invalidate it, log out of
all sessions on LJ.

### Recovering a forgotten password

https://www.livejournal.com/lostinfo.bml emails a reset link to the address on
the account. If that inbox is gone too, LJ Support
(https://www.livejournal.com/support/submit) can verify ownership by other
means, such as old email addresses and approximate signup date. Once the password
works, `livelonger backup poeticdream` makes a complete backup, including
private posts. Until then, `fetch poeticdream --as riffraff` gets every
friends-locked post riffraff is allowed to read.

## Being a good bot (why this won't get you banned)

From LJ's bot policy (https://www.livejournal.com/bots/): use their data
interfaces instead of screen-scraping, cache, "do not multithread … and do not
connect more than five times per second", and send "a well-formed user agent
which includes a contact email address". livelonger:

* sends one request at a time, 1.5 s apart by default (`delay` in
  `livelonger.toml`)
* backs off on 429/503 (honouring `Retry-After`) and slows down for the rest
  of the run
* stops dead if LJ redirects to the bot-policy page, rather than retrying
* identifies itself with your contact address
* rations password logins. LJ temporarily bans IPs that log in too often, and
  every API call that carries a password counts as a login. A first run uses
  **one** password login to make a session, then authenticates every call with
  the session cookie. Later runs reuse the saved session and don't ask for the
  password at all. If LJ rejects the session, livelonger makes a new one once.
  It refuses outright to do more than 40 password logins in one run.
* keeps one connection open per host (HTTP keep-alive) instead of making a
  new connection for every request
* retries LJ's transient errors (code 406 "repeated requests" and the 500s)
  with backoff, and stops on a ban
* saves every entry and every page of comments the moment it arrives, so an
  interrupted run resumes where it stopped. A failure in one step (friends,
  media, ...) is reported and the remaining steps still run.
* caches every web page, so a re-run never requests the same page twice
* prefers the bulk API: entries come in batches through
  `getevents(selecttype=syncitems)` (the approach of LJ's own `jbackup.pl`),
  and comments through `export_comments.bml`, about 10,000 comment headers and
  1,000 bodies per request. A journal with 1,400 entries and 11,000 comments
  needs on the order of **50 API requests**, versus ~3,000 page loads for a
  scraper.

What still costs page loads: other people's journals (one per entry, plus
calendar pages), and the optional extras for your own: entries containing
video embeds or polls, one allpics lookup per distinct commenter for their
userpic, the style capture (~30) and images. Turn these off with
`--no-embeds`, `--no-commenter-userpics`, `--no-theme` and `--no-media`.

## What's in the archive

```
archive/
  riffraff/
    journal.json  profile.json  userpics.json  friends.json  tags.json
    entries/<id>.json     normalized entry (raw LJ markup kept as-is)
    comments/<id>.json    threaded comments for that entry
    raw/                  untouched API/export responses, for fidelity
    theme/                captured style: shell.html, css/, img/, original front page
  _media/                 downloaded images, shared by all journals
  _sessions/  _cache/  _userpics/
```

The JSON is meant to outlive this tool. `raw/` holds exactly what LJ sent.

## Merging archives

`livelonger import PATH` accepts either a whole archive folder or a single
journal folder from one. Each journal keeps its own folder, with its own
userpics, connections and captured style. The only things shared between
journals are downloaded image files, which are stored once per URL. Sessions
and caches are never imported.

Merging works per entry and per comment, never by replacing a whole journal:

1. **An owner backup beats a web copy**, entry by entry and comment by
   comment. Even an older owner backup outranks a newer web copy, and nothing
   you fetch or import later can downgrade it.
2. **A fully loaded comment beats a collapsed stub.**
3. **On a tie, the newer archive wins**, or theirs if you pass `--force`.
4. **Nothing is dropped.** Entries and comments only one side has are kept.
   That includes comments deleted on LJ since an earlier fetch, and the union
   of two friends' web copies that could each see different locked posts.

Profile fields and userpics are merged with the same tie-break. The friends
list and captured style come from the preferred copy. Raw responses are added
but never overwritten. `journal.json` records every import.

Importing someone's owner backup brings their **private** entries into your
`site/`. Build with `--public-only` if the result will be shared.

## Commands

| | |
|---|---|
| `init` | write `livelonger.toml` |
| `backup USER` | complete backup of a journal you can log in to (`--full` re-downloads everything) |
| `login USER` | just save a session for `fetch --as` |
| `fetch USER…` | web archive of other journals (`--as`, `--cookies`, `--years`, `--entries`, `--refresh`) |
| `theme USER…` | re-capture styles |
| `media [USER…]` | download images (`--retry-failed`) |
| `build` | generate the site (`--theme original/classic`, `--public-only`, `--out`) |
| `serve` | serve the site on localhost |
| `status` | what's archived |
| `import PATH` | merge another livelonger archive, entry by entry (`--force`: ties go to theirs) |

## Known limits

* **Embeds and polls:** `<lj-embed>` videos and polls are taken from LJ's
  rendered page, so they're still live embeds (YouTube etc.). Images are
  downloaded. Video files aren't.
* **Original theme:** this is an approximation. Header, sidebar and colours are
  the real thing. Entries and comments use our own markup, given the class
  names S2 styles use. Some layouts will look better than others, and
  `--theme classic` is always available.
* **Dead images:** pictures on Photobucket, geocities and the like are long
  gone. They stay as links to the original location, and each failure is
  recorded once rather than retried.
