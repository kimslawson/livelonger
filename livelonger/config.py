"""Configuration: livelonger.toml in the working directory (or --config)."""
from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__

DEFAULT_PATH = "livelonger.toml"

TEMPLATE = """\
# livelonger configuration
#
# LiveJournal's bot policy (https://www.livejournal.com/bots/) requires every
# automated client to send a User-Agent with a contact email for the person
# running it. Bots without one "have a higher chance of being blocked".
contact = "{contact}"

# Where raw archived data lives, and where the browsable static site is built.
archive_dir = "archive"
site_dir = "site"

# Seconds to wait between requests to LiveJournal. The bot policy caps you at
# 5 connections/second and forbids parallel requests; we run strictly one at a
# time, and by default far slower than the cap. Backoff is automatic on
# 429/503 responses.
delay = 1.5

# Seconds between requests to *other* hosts (image hosts etc.), per host.
media_delay = 0.5

# Default theme for `livelonger build`: "original" (your journal's own LJ
# style, captured) or "classic" (a clean built-in theme).
theme = "original"
"""


@dataclass
class Config:
    contact: str = ""
    archive_dir: Path = Path("archive")
    site_dir: Path = Path("site")
    delay: float = 1.5
    media_delay: float = 0.5
    max_retries: int = 6
    theme: str = "original"
    path: Path | None = None
    extra: dict = field(default_factory=dict)

    @property
    def user_agent(self) -> str:
        contact = self.contact.strip()
        return (
            f"livelonger/{__version__} (personal LiveJournal archiver; "
            f"one request at a time; contact: {contact})"
        )

    def require_contact(self):
        if not self.contact.strip() or "@" not in self.contact and "://" not in self.contact:
            sys.exit(
                "livelonger: set `contact` (an email address) in livelonger.toml first.\n"
                "LiveJournal's bot policy requires a contact address in the User-Agent;\n"
                "without one you are much more likely to be blocked. Run `livelonger init`."
            )


def load(path: str | os.PathLike | None = None) -> Config:
    p = Path(path or os.environ.get("LIVELONGER_CONFIG", DEFAULT_PATH))
    cfg = Config()
    if p.exists():
        with open(p, "rb") as f:
            data = tomllib.load(f)
        cfg.path = p
        base = p.parent
        cfg.contact = str(data.get("contact", ""))
        cfg.archive_dir = base / data.get("archive_dir", "archive")
        cfg.site_dir = base / data.get("site_dir", "site")
        cfg.delay = float(data.get("delay", cfg.delay))
        cfg.media_delay = float(data.get("media_delay", cfg.media_delay))
        cfg.max_retries = int(data.get("max_retries", cfg.max_retries))
        cfg.theme = str(data.get("theme", cfg.theme))
        cfg.extra = data
    if cfg.delay < 0.25:
        cfg.delay = 0.25  # never exceed ~4 req/s, under LJ's 5/s cap
    return cfg


def write_template(path: Path, contact: str):
    path.write_text(TEMPLATE.format(contact=contact.replace('"', "")))
