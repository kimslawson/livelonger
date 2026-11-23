"""Offline tests: no network. Run with `python -m unittest discover tests`."""
import json
import tempfile
import unittest
import urllib.parse
import xmlrpc.client
from pathlib import Path

from livelonger import markup, web
from livelonger.backup_own import OwnBackup
from livelonger.http import Response
from livelonger.linker import Linker, parse_lj_url
from livelonger.site import SiteBuilder, lj_date
from livelonger.store import Archive, journal_base_url, merge_comments
from livelonger.theme import find_content_column
from livelonger import htmltree


class MarkupTests(unittest.TestCase):
    def test_linebreaks_and_ljuser(self):
        out = markup.render('Hi <lj user="poetic_dream">\nthere')
        self.assertIn("<br />", out)
        self.assertIn('lj:user="poetic_dream"', out)
        self.assertIn("https://poetic-dream.livejournal.com/", out)

    def test_preformatted_keeps_newlines(self):
        self.assertNotIn("<br", markup.render("a\nb", preformatted=True))

    def test_cut_summary_and_full(self):
        raw = 'intro<lj-cut text="more!">secret</lj-cut>outro'
        s = markup.render(raw, summary=True, entry_url="e.html")
        self.assertIn("more!", s)
        self.assertNotIn("secret", s)
        self.assertIn("outro", s)
        f = markup.render(raw)
        self.assertIn("secret", f)
        self.assertIn('name="cutid1"', f)

    def test_unclosed_cut_hides_rest_in_summary(self):
        s = markup.render("a<lj-cut>hidden words", summary=True)
        self.assertNotIn("hidden words", s)

    def test_script_and_handlers_stripped(self):
        out = markup.render('<img src="x.png" onload="evil()"><script>bad()</script>ok')
        self.assertNotIn("evil", out)
        self.assertNotIn("bad()", out)
        self.assertIn("ok", out)

    def test_autolink_not_inside_anchor(self):
        out = markup.render('<a href="http://a.com">http://a.com</a> and http://b.com.')
        self.assertEqual(out.count("<a "), 2)
        self.assertIn('<a href="http://b.com">http://b.com</a>.', out)

    def test_lj_raw_no_breaks(self):
        self.assertNotIn("<br", markup.render("<lj-raw>a\nb</lj-raw>"))

    def test_embed_and_poll_placeholders(self):
        out = markup.render('<lj-embed id="3" /> <lj-poll-123>')
        self.assertIn("embedded media #3", out)
        self.assertIn("poll/?id=123", out)


class LinkerTests(unittest.TestCase):
    def test_parse_urls(self):
        self.assertEqual(parse_lj_url("https://poeticdream.livejournal.com/123.html")[:2],
                         ("poeticdream", "/123.html"))
        self.assertEqual(parse_lj_url("http://www.livejournal.com/users/foo_bar/9.html")[:2],
                         ("foo_bar", "/9.html"))
        self.assertEqual(parse_lj_url("https://foo-bar.livejournal.com/")[:2], ("foo_bar", "/"))
        self.assertEqual(parse_lj_url("https://www.livejournal.com/userinfo.bml?user=x")[:2],
                         ("x", "/profile"))
        self.assertIsNone(parse_lj_url("https://example.com/1.html"))

    def test_rewrite(self):
        lk = Linker({"poeticdream": {123}}, {"https://pics.example/a.jpg": "media/files/a.jpg"})
        html = ('<a href="https://poeticdream.livejournal.com/123.html?thread=77#t77">x</a>'
                '<a href="https://poeticdream.livejournal.com/999.html">y</a>'
                '<img src="https://pics.example/a.jpg">')
        out = lk.rewrite(html, 1)
        self.assertIn('href="../poeticdream/123.html#t77"', out)
        self.assertIn("https://poeticdream.livejournal.com/999.html", out)  # not archived
        self.assertIn('src="../media/files/a.jpg"', out)


class WebParseTests(unittest.TestCase):
    PAGE = """<html><body><script>
      Site.entry = {"title":"Hello","is_public":false,"eventtime":1112400000,"poster":"pd",
                    "poster_userpic_url":"https://l-userpic.livejournal.com/1/2"};
      Site.page = {"replycount":2,"comments_page":1,"comments":[
        {"thread":512,"parent":0,"uname":"kim","dname":"kim","username":[{"username":"kim"}],
         "article":"hi <b>you</b>","ctime_ts":1112400100,"loaded":1,"userpic":"u.png"},
        {"thread":768,"parent":512,"uname":"pd","username":[{"username":"pd"}],
         "article":null,"loaded":0,"collapsed":1,"ctime_ts":1112400200}]};
      Site.remote = {"username":"kim"};
    </script>
    <h1 class="aentry-post__title"><span class="aentry-post__title-text">Hello</span></h1>
    <div class="aentry-post__text"><p>Body &amp; soul</p><div class="ljsale">ad</div></div>
    <div class="aentry-tags"><a href="/tag/a">us</a><a href="/tag/b">2005</a></div>
    </body></html>"""

    def test_entry_page(self):
        p = web.parse_entry_page(self.PAGE, "pd", 1234, "https://pd.livejournal.com/1234.html")
        e = p["entry"]
        self.assertEqual(e["subject"], "Hello")
        self.assertEqual(e["security"], "locked")
        self.assertIn("Body &amp; soul", e["body"])
        self.assertNotIn("ad</div>", e["body"])
        self.assertEqual(e["tags"], ["us", "2005"])
        self.assertEqual(e["eventtime"], "2005-04-02 00:00:00")
        self.assertEqual(e["fetched_logged_in_as"], "kim")
        self.assertEqual([c["id"] for c in p["comments"]], [512, 768])
        self.assertFalse(p["comments"][1]["loaded"])


class FakeHTTP:
    """Answers the XML-RPC and export_comments calls an owner backup makes."""

    def __init__(self):
        self.calls = []
        self.cookies_set = {}
        self.delays = {"livejournal": 0}
        self.request_count = 0

    def set_cookie(self, k, v, domain=".livejournal.com"):
        self.cookies_set[k] = v

    def has_auth(self):
        return bool(self.cookies_set)

    def post(self, url, data, **kw):
        (params,), method = xmlrpc.client.loads(data)
        method = method.split(".")[-1]
        self.calls.append(method)
        res = {
            "getchallenge": {"challenge": "c0:1:2:3:abc"},
            "login": {"userid": 7, "fullname": "Kim", "pickws": ["socks"],
                      "pickwurls": ["https://l-userpic.livejournal.com/1/7"],
                      "defaultpicurl": "https://l-userpic.livejournal.com/1/7",
                      "moods": [{"id": 1, "name": "amused"}]},
            "sessiongenerate": {"ljsession": "v1:u7:s9:aXYZ:g1//1"},
            "syncitems": {"syncitems": [] if params.get("lastsync") else [
                {"item": "L-1", "action": "create", "time": "2005-01-01 10:00:00"},
                {"item": "L-2", "action": "create", "time": "2005-02-01 10:00:00"},
                {"item": "C-9", "action": "create", "time": "2005-02-02 10:00:00"},
            ], "count": 3, "total": 3},
            "getevents": {"events": [
                {"itemid": 1, "anum": 5, "eventtime": "2005-01-01 09:00:00", "subject": "First",
                 "event": xmlrpc.client.Binary("Héllo\n<lj user=\"poeticdream\">".encode()),
                 "security": "public", "props": {"taglist": "us, love", "current_moodid": 1}},
                {"itemid": 2, "anum": 6, "eventtime": "2005-02-01 09:00:00", "subject": "Secret",
                 "event": "only us", "security": "usemask", "allowmask": 1, "props": {}},
            ]},
            "getfriends": {"friends": [{"username": "poeticdream", "fullname": "P", "groupmask": 3}],
                           "friendofs": [{"username": "poeticdream"}],
                           "friendgroups": [{"id": 1, "name": "Close", "sortorder": 1}]},
            "getusertags": {"tags": [{"name": "us", "uses": 1}]},
        }[method]
        return Response(url, 200, {"Content-Type": "text/xml"},
                        xmlrpc.client.dumps((res,), methodresponse=True).encode())

    def get(self, url, **kw):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        if "comment_meta" in q.get("get", []):
            body = ("<livejournal><maxid>3</maxid><comments><comment id='1' posterid='20'/>"
                    "<comment id='2' posterid='7'/><comment id='3' posterid='20' state='S'/></comments>"
                    "<usermaps><usermap id='20' user='poeticdream'/><usermap id='7' user='riffraff'/></usermaps></livejournal>")
        else:
            body = ("<livejournal><comments>"
                    "<comment id='1' jitemid='1' posterid='20'><body>hi love</body><date>2005-01-01T10:00:00Z</date></comment>"
                    "<comment id='2' jitemid='1' posterid='7' parentid='1'><body>hi back</body><date>2005-01-01T11:00:00Z</date></comment>"
                    "<comment id='3' jitemid='2' posterid='20' state='S'><subject>psst</subject><body>secret</body><date>2005-02-01T10:00:00Z</date></comment>"
                    "</comments></livejournal>")
        return Response(url, 200, {"Content-Type": "text/xml"}, body.encode())


class PipelineTests(unittest.TestCase):
    def test_backup_then_build(self):
        with tempfile.TemporaryDirectory() as d:
            archive = Archive(Path(d) / "archive")
            http = FakeHTTP()
            b = OwnBackup(archive, http, "riffraff", "pw", log=lambda *a: None)
            b.login()
            b.entries()
            b.comments()
            b.connections()
            j = archive.journal("riffraff")
            self.assertEqual(j.entry_ids(), [261, 518])
            e = j.load_entry(261)
            self.assertIn("Héllo", e["body"])
            self.assertEqual(e["tags"], ["us", "love"])
            self.assertEqual(e["mood"], "amused")
            cs = j.load_comments(261)
            self.assertEqual([(c["id"], c["parent"], c["poster"]) for c in cs],
                             [(261, 0, "poeticdream"), (517, 261, "riffraff")])
            self.assertEqual(j.load_comments(518)[0]["state"], "S")
            self.assertIn("ljloggedin", http.cookies_set)
            self.assertEqual(http.cookies_set["ljloggedin"], "u7:s9")

            # a second journal, as if fetched from the web, to test interlinking
            pd = archive.journal("poeticdream")
            pd.update_meta(username="poeticdream", source="web", title="Poetic")
            pd.save_entry({"source": "web", "ditemid": 1234, "itemid": 4, "anum": 210,
                           "eventtime": "2005-01-02 00:00:00", "subject": "Us",
                           "body": '<a href="https://riffraff.livejournal.com/261.html?thread=517#t517">his post</a>',
                           "body_format": "html", "security": "locked", "tags": []})

            out = Path(d) / "site"
            SiteBuilder(archive, out, theme="original", log=lambda *a: None).build()
            entry = (out / "riffraff" / "261.html").read_text()
            self.assertIn("Héllo", entry)
            self.assertIn('href="../poeticdream/index.html"', entry)  # <lj user> localized
            self.assertIn('id="t517"', entry)
            self.assertIn("hi back", entry)
            secret = (out / "riffraff" / "518.html").read_text()
            self.assertIn("🔒 friends", secret)
            self.assertIn("screened", secret)
            theirs = (out / "poeticdream" / "1234.html").read_text()
            self.assertIn('href="../riffraff/261.html#t517"', theirs)
            self.assertTrue((out / "riffraff" / "archive" / "2005.html").exists())
            self.assertTrue((out / "riffraff" / "tags" / "index.html").exists())
            self.assertIn("Close", (out / "riffraff" / "connections.html").read_text())
            self.assertIn("archived here", (out / "riffraff" / "connections.html").read_text())

            pub = Path(d) / "pub"
            SiteBuilder(archive, pub, public_only=True, log=lambda *a: None).build()
            self.assertFalse((pub / "riffraff" / "518.html").exists())
            self.assertFalse((pub / "poeticdream" / "1234.html").exists())


class ThemeTests(unittest.TestCase):
    def test_content_column(self):
        base = journal_base_url("x")
        page = (f'<html><body><div id="header"><a href="{base}">home</a></div>'
                f'<div id="content"><div id="alpha"><div class="entry"><a href="{base}1.html">a</a></div>'
                f'<div class="entry"><a href="{base}2.html">b</a></div></div>'
                f'<div id="beta"><a href="{base}3.html">recent</a><a href="{base}1.html">a</a></div></div></body></html>')
        t = htmltree.parse(page)
        self.assertEqual(find_content_column(t, base).attrs.get("id"), "alpha")


def _entry(d, source, body="x"):
    return {"source": source, "ditemid": d, "itemid": d // 256, "anum": d % 256, "body": body,
            "body_format": "html", "eventtime": "2005-01-01 00:00:00", "subject": "", "security": "public"}


def _comment(cid, source, body="c", loaded=True, state="A"):
    return {"source": source, "id": cid, "parent": 0, "body": body, "state": state, "loaded": loaded}


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.archive = Archive(self.root / "mine")

    def tearDown(self):
        self.tmp.cleanup()

    def other(self, name, source, when, entries, comments=None, **extra):
        j = Archive(self.root / name).journal("pd")
        j.update_meta(username="pd", source=source,
                      **({"archived_at": when} if source == "api" else {"web_archived_at": when}), **extra)
        for e in entries:
            j.save_entry(e, force=True)
        for d, cs in (comments or {}).items():
            j.save_comments(d, cs)
        return j.path

    def test_comment_merge_keeps_old_and_prefers_better(self):
        merged, added, updated = merge_comments(
            [_comment(1, "web", loaded=False, body=""), _comment(2, "api", "owner copy"), _comment(3, "web")],
            [_comment(1, "web", "now loaded"), _comment(2, "web", "web copy"), _comment(4, "web")])
        by = {c["id"]: c for c in merged}
        self.assertEqual(sorted(by), [1, 2, 3, 4])          # 3 kept although missing from new
        self.assertEqual(by[1]["body"], "now loaded")       # loaded beats stub
        self.assertEqual(by[2]["body"], "owner copy")       # api beats web
        self.assertEqual((added, updated), (1, 1))

    def test_import_entry_by_entry(self):
        from livelonger.merge import import_journal
        mine = self.archive.journal("pd")
        mine.update_meta(username="pd", source="web", web_archived_at=100, title="Mine")
        mine.save_entry(_entry(256, "web", "mine 1"))
        mine.save_entry(_entry(512, "web", "mine 2"))
        mine.save_comments(512, [_comment(5000, "web", loaded=False, body="")])

        # a newer web copy from a friend: ties go to theirs, nothing of ours is lost
        src = self.other("friend", "web", 200, [_entry(512, "web", "theirs 2"), _entry(768, "web")],
                         {512: [_comment(5000, "web", "full text"), _comment(5001, "web")]})
        st = import_journal(src, mine)
        self.assertEqual(mine.entry_ids(), [256, 512, 768])
        self.assertEqual(mine.load_entry(512)["body"], "theirs 2")
        self.assertEqual(mine.load_entry(512)["comment_count"], 2)
        self.assertEqual({c["id"]: c["body"] for c in mine.load_comments(512)},
                         {5000: "full text", 5001: "c"})
        self.assertEqual((st["entries_added"], st["entries_replaced"], st["entries_only_ours"]), (1, 1, 1))

        # an OLDER owner backup still wins on source, entry by entry
        src = self.other("owner", "api", 50, [_entry(256, "api", "owner 1")],
                         {256: [_comment(9000, "api", state="S")]}, title="Theirs")
        import_journal(src, mine)
        self.assertEqual(mine.load_entry(256)["body"], "owner 1")
        self.assertEqual(mine.meta["source"], "api")
        self.assertEqual(mine.meta["title"], "Theirs")
        self.assertEqual(len(mine.meta["imports"]), 2)

        # and a later web copy can't downgrade it
        src = self.other("late", "web", 999, [_entry(256, "web", "web 1")])
        st = import_journal(src, mine)
        self.assertEqual(mine.load_entry(256)["body"], "owner 1")
        self.assertEqual(st["entries_kept"], 1)

    def test_older_same_kind_needs_force(self):
        from livelonger.merge import import_journal
        mine = self.archive.journal("pd")
        mine.update_meta(username="pd", source="web", web_archived_at=500)
        mine.save_entry(_entry(256, "web", "newer"))
        src = self.other("old", "web", 10, [_entry(256, "web", "older")])
        import_journal(src, mine)
        self.assertEqual(mine.load_entry(256)["body"], "newer")
        import_journal(src, mine, force=True)
        self.assertEqual(mine.load_entry(256)["body"], "older")


class MiscTests(unittest.TestCase):
    def test_lj_date(self):
        self.assertEqual(lj_date("2013-04-09 14:08:00"), "Apr. 9th, 2013 at 2:08 PM")

    def test_base_url(self):
        self.assertEqual(journal_base_url("poetic_dream"), "https://poetic-dream.livejournal.com/")
        self.assertEqual(journal_base_url("_x_"), "https://users.livejournal.com/_x_/")


if __name__ == "__main__":
    unittest.main()
