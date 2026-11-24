"""Offline tests: no network. Run with `python -m unittest discover tests`."""
import json
import tempfile
import unittest
import urllib.parse
import xmlrpc.client
from pathlib import Path

from livelonger import markup, web
from livelonger.backup_own import OwnBackup
from livelonger.ljapi import LJError
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

    def __init__(self, n_events=2, cookie_auth=True, web_accepts="all"):
        self.web_accepts = web_accepts   # cookie combination the *website* accepts, or None
        self.calls = []
        self.cookies_set = {}
        self.delays = {"livejournal": 0}
        self.request_count = 0
        self.cookie_auth = cookie_auth   # does "LJ" accept auth_method=cookie at all?
        self.revoked = set()             # ljsession values LJ no longer accepts
        self.sessions = 0
        self.faults = {}                 # (method, selecttype) -> [fault codes to raise, in order]
        self.body_fail_at = None         # comment_body startid that errors once
        self.EVENTS = list(FakeHTTP.EVENTS) + [
            {"itemid": i, "anum": 1, "eventtime": "2006-01-01 00:00:00", "subject": f"e{i}",
             "event": "x", "security": "public", "props": {}} for i in range(3, n_events + 1)]

    def slow_down(self, group="livejournal"):
        pass

    @staticmethod
    def fault(url, code, msg):
        return Response(url, 200, {"Content-Type": "text/xml"}, xmlrpc.client.dumps(
            xmlrpc.client.Fault(code, msg), methodresponse=True).encode())

    def set_cookie(self, k, v, domain=".livejournal.com"):
        self.cookies_set[k] = v

    def remove_cookie(self, k):
        self.cookies_set.pop(k, None)

    def web_logged_in(self):
        present = {k for k in self.cookies_set if k in ("ljsession", "ljmastersession", "ljloggedin")}
        return bool(present) and {
            "all": True, "ljsession": present == {"ljsession"},
            "master": present == {"ljmastersession", "ljloggedin"}}.get(self.web_accepts, False)

    def has_auth(self):
        return bool(self.cookies_set)

    EVENTS = [
        {"itemid": 1, "anum": 5, "eventtime": "2005-01-01 09:00:00", "subject": "First",
         "event": xmlrpc.client.Binary("Héllo\n<lj user=\"poeticdream\">".encode()),
         "security": "public", "props": {"taglist": "us, love", "current_moodid": 1}},
        {"itemid": 2, "anum": 6, "eventtime": "2005-02-01 09:00:00", "subject": 2008,
         "event": "only us", "security": "usemask", "allowmask": 1,
         # LJ's XML-RPC types numeric-looking values as ints (seen live: taglist 411)
         "props": {"taglist": 411, "current_music": 1999, "picture_keyword": 42}},
    ]

    def getevents(self, params):
        sel = params["selecttype"]
        if sel == "syncitems":
            # simulate LJ returning the same batch over and over (many entries
            # sharing one modification time): only ever entry 1
            return {"events": self.EVENTS[:1]}
        if sel == "multiple":
            want = {int(x) for x in params["itemids"].split(",")}
            return {"events": [e for e in self.EVENTS if e["itemid"] in want]}
        return {"events": [e for e in self.EVENTS if e["itemid"] == params["itemid"]]}

    def post(self, url, data, headers=None, **kw):
        (params,), method = xmlrpc.client.loads(data)
        method = method.split(".")[-1]
        self.calls.append((method, params.get("auth_method"), (headers or {}).get("X-LJ-Auth"),
                           params.get("selecttype"), params.get("lastsync")))
        if params.get("auth_method") == "cookie" and (
                not self.cookie_auth or self.cookies_set.get("ljsession") in self.revoked
                or "ljsession" not in self.cookies_set):
            return self.fault(url, 101, "Invalid password")
        queued = self.faults.get((method, params.get("selecttype")))
        if queued:
            code = queued.pop(0)
            return self.fault(url, code, f"Simulated fault {code} (mentions login and session)")
        if method == "sessiongenerate":
            self.sessions += 1
            res = {"ljsession": f"v1:u7:s{8 + self.sessions}:aXYZ:g1//1"}
            return Response(url, 200, {"Content-Type": "text/xml"},
                            xmlrpc.client.dumps((res,), methodresponse=True).encode())
        if method == "getevents":
            res = self.getevents(params)
            return Response(url, 200, {"Content-Type": "text/xml"},
                            xmlrpc.client.dumps((res,), methodresponse=True).encode())
        res = {
            "getchallenge": {"challenge": "c0:1:2:3:abc"},
            "login": {"userid": 7, "fullname": "Kim", "pickws": ["socks", 42],
                      "pickwurls": ["https://l-userpic.livejournal.com/1/7",
                                    "https://l-userpic.livejournal.com/2/7"],
                      "defaultpicurl": "https://l-userpic.livejournal.com/1/7",
                      "moods": [{"id": 1, "name": "amused"}]},
            "sessiongenerate": {"ljsession": "v1:u7:s9:aXYZ:g1//1"},
            "syncitems": {"syncitems": [] if params.get("lastsync") else [
                {"item": "L-1", "action": "create", "time": "2005-01-01 10:00:00"},
                {"item": "L-2", "action": "create", "time": "2005-02-01 10:00:00"},
                {"item": "C-9", "action": "create", "time": "2005-02-02 10:00:00"},
            ] + [{"item": f"L-{e['itemid']}", "action": "create", "time": "2006-01-01 10:00:00"}
                 for e in self.EVENTS[2:]], "count": 1, "total": 1},
            "getfriends": {"friends": [{"username": "poeticdream", "fullname": "P", "groupmask": 3}],
                           "friendofs": [{"username": "poeticdream"}],
                           "friendgroups": [{"id": 1, "name": "Close", "sortorder": 1}]},
            "getusertags": {"tags": [{"name": "us", "uses": 1}]},
        }[method]
        return Response(url, 200, {"Content-Type": "text/xml"},
                        xmlrpc.client.dumps((res,), methodresponse=True).encode())

    def get(self, url, **kw):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        if "export_comments" in url and not self.web_logged_in():
            self.calls.append(("export_bounced", None))
            return Response("https://www.livejournal.com/login.bml?returnto=x", 200,
                            {"Content-Type": "text/html"}, b"<html>Log in</html>")
        if "comment_meta" in q.get("get", []):
            body = ("<livejournal><maxid>3</maxid><comments><comment id='1' posterid='20'/>"
                    "<comment id='2' posterid='7'/><comment id='3' posterid='20' state='S'/></comments>"
                    "<usermaps><usermap id='20' user='poeticdream'/><usermap id='7' user='riffraff'/></usermaps></livejournal>")
        else:
            start = int(q["startid"][0])
            if self.body_fail_at is not None and start == self.body_fail_at:
                self.body_fail_at = None
                from livelonger.http import FetchError
                raise FetchError("simulated network failure")
            pages = {
                "1": "<comment id='1' jitemid='1' posterid='20'><body>hi love</body><date>2005-01-01T10:00:00Z</date></comment>",
                "2": "<comment id='2' jitemid='1' posterid='7' parentid='1'><body>hi back</body><date>2005-01-01T11:00:00Z</date></comment>",
                "3": "<comment id='3' jitemid='2' posterid='20' state='S'><subject>psst</subject><body>secret</body><date>2005-02-01T10:00:00Z</date></comment>",
            }
            # one comment per "page", like LJ's 1000-per-request but smaller
            self.calls.append(("comment_body", start))
            page = pages.get(str(max(start, 1)), "")
            return Response(url, 200, {"Content-Type": "text/xml"},
                            f"<livejournal><comments>{page}</comments></livejournal>".encode())
        if False:
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
            e2 = j.load_entry(518)
            self.assertEqual((e2["subject"], e2["tags"], e2["music"]), ("2008", ["411"], "1999"))
            self.assertEqual(e2["userpic_url"], "https://l-userpic.livejournal.com/2/7")
            self.assertIn("ljloggedin", http.cookies_set)
            self.assertEqual(http.cookies_set["ljloggedin"], "u7:s9")
            self.assertEqual(archive.load_session("riffraff")["ljsession"], "v1:u7:s9:aXYZ:g1//1")
            # exactly two password logins; everything after uses the session cookie
            pw = [c for c in http.calls if c[1] == "challenge"]
            self.assertEqual([c[0] for c in pw], ["sessiongenerate"])
            after = [c for c in http.calls if c[0] not in ("getchallenge", "sessiongenerate", "comment_body")]
            self.assertTrue(after and all(c[1] == "cookie" and c[2] == "cookie" for c in after))
            # stalled date walk -> switched to fetching by id list, never one-by-one
            sels = [c[3] for c in http.calls if c[0] == "getevents"]
            self.assertEqual(sels, ["syncitems", "syncitems", "multiple"])
            self.assertEqual(j.state["item_times"], {"1": "2005-01-01 10:00:00", "2": "2005-02-01 10:00:00"})

            # a re-run reuses the saved session: no password needed at all
            http2 = FakeHTTP()
            asked = []
            b2 = OwnBackup(archive, http2, "riffraff", lambda: asked.append(1) or "pw", log=lambda *a: None)
            b2.login()
            b2.entries()
            self.assertEqual(asked, [])
            self.assertFalse(any(c[1] == "challenge" for c in http2.calls))
            self.assertEqual([c[3] for c in http2.calls if c[0] == "getevents"], [])  # nothing changed

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
    def test_site_chrome_vs_style_css(self):
        from livelonger.theme import _site_css
        L = "https://l-stat.livejournal.net/"
        self.assertTrue(_site_css(L + "??schemius_v4/header_v5.css,lj_base-journal.css?v=1"))
        self.assertTrue(_site_css(L + "??svg/flaticon.css,proximanova-opentype.css?v=1"))
        self.assertFalse(_site_css(L + "??sup/s/style.css,sup/s/spring-daze.css?v=1"))   # minimalism
        self.assertFalse(_site_css(L + "??voxhtml/base.css,voxhtml/bloom/screen.css"))    # bloom
        self.assertFalse(_site_css("https://riffraff.livejournal.com/res/12139202/stylesheet?1"))
        self.assertTrue(_site_css(""))

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


class AuthAndOrderingTests(unittest.TestCase):
    """Review findings: session failures, the login cap, partial failures."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.archive = Archive(Path(self.tmp.name) / "a")
        from unittest import mock
        self.nosleep = mock.patch("livelonger.ljapi.time.sleep")
        self.nosleep.start()

    def tearDown(self):
        self.nosleep.stop()
        self.tmp.cleanup()

    def backup(self, http, pw="pw"):
        return OwnBackup(self.archive, http, "riffraff", (lambda: pw), log=lambda *a: None)

    def pw_logins(self, http):
        return [c[0] for c in http.calls if c[1] == "challenge"]

    def test_revoked_session_is_refreshed_once(self):
        from livelonger.ljapi import session_cookies
        old = "v1:u7:s1:aOLD:g1//1"
        self.archive.save_session("riffraff", session_cookies(old))
        http = FakeHTTP()
        http.revoked.add(old)
        b = self.backup(http)
        b.login()
        b.entries()
        b.connections()
        self.assertEqual(self.pw_logins(http), ["sessiongenerate"])   # exactly one
        self.assertNotEqual(self.archive.load_session("riffraff")["ljsession"], old)
        self.assertEqual(len(b.j.entry_ids()), 2)

    def test_no_cookie_auth_falls_back_then_stops_cleanly(self):
        from livelonger.ljapi import StopRun
        http = FakeHTTP(cookie_auth=False)
        b = self.backup(http)
        b.api.MAX_PASSWORD_LOGINS = 4
        with self.assertRaises(StopRun):
            b.login()
            b.entries()
        self.assertLessEqual(len(self.pw_logins(http)), 4)

    def test_non_auth_fault_mentioning_login_keeps_session(self):
        http = FakeHTTP()
        b = self.backup(http)
        b.login()
        http.faults[("getfriends", None)] = [300]
        with self.assertRaises(LJError):
            b.connections()
        self.assertTrue(b.api.session)
        self.assertEqual(self.pw_logins(http), ["sessiongenerate"])

    def test_cookie_auth_unsupported_is_learned_once(self):
        http = FakeHTTP(cookie_auth=False)
        b = self.backup(http)
        b.login()
        b.entries()
        cookie_tries = [c for c in http.calls if c[1] == "cookie"]
        self.assertEqual(len(cookie_tries), 1)               # one refusal settles it
        self.assertEqual(self.pw_logins(http).count("sessiongenerate"), 1)  # no pointless refresh
        self.assertIs(b.j.state["api_cookie_auth"], False)
        # next run: no cookie attempts, no new session, straight to password calls
        http2 = FakeHTTP(cookie_auth=False)
        b2 = self.backup(http2)
        b2.login()
        b2.entries()
        self.assertEqual([c for c in http2.calls if c[1] == "cookie"], [])
        self.assertNotIn("sessiongenerate", self.pw_logins(http2))

    def test_one_bad_entry_doesnt_sink_the_run(self):
        http = FakeHTTP(n_events=5)
        http.EVENTS[3] = dict(http.EVENTS[3], allowmask="not a number")   # itemid 4
        b = self.backup(http)
        b.login()
        b.entries()
        self.assertEqual(b.problem_entries, [4])
        self.assertEqual(len(b.j.entry_ids()), 4)
        self.assertNotIn("4", b.j.state["item_times"])       # retried next run
        self.assertTrue(b.j.p("raw", "events", "4.json").exists())

    def test_phase_survives_unexpected_bugs(self):
        from livelonger import cli
        from unittest import mock
        cli.FAILED_STEPS.clear()
        with mock.patch.object(cli, "log", lambda *a: None):
            self.assertIsNone(cli._phase("demo", lambda: 1 / 0))
        self.assertEqual(cli.FAILED_STEPS, ["demo"])
        cli.FAILED_STEPS.clear()

    def test_website_cookie_style_is_found_and_remembered(self):
        http = FakeHTTP(web_accepts="ljsession")    # site ignores ljsession if master cookies present
        b = self.backup(http)
        b.login()
        b.entries()
        b.comments()
        self.assertEqual(len(b.j.load_comments(261)), 2)
        self.assertEqual(self.archive.session_info("riffraff")["variant"], "ljsession")
        self.assertEqual(self.pw_logins(http), ["sessiongenerate"])   # probing costs no logins
        # next run goes straight to the working style
        http2 = FakeHTTP(web_accepts="ljsession")
        b2 = self.backup(http2)
        b2.login()
        b2.comments()
        self.assertEqual([c for c in http2.calls if c[0] == "export_bounced"], [])

    def test_website_refuses_every_style_points_to_browser_cookies(self):
        http = FakeHTTP(web_accepts=None)
        b = self.backup(http)
        b.login()
        with self.assertRaises(LJError) as cm:
            b.comments()
        self.assertIn("--cookies", str(cm.exception))
        self.assertEqual(len([c for c in http.calls if c[0] == "export_bounced"]), 3)
        self.assertEqual(self.pw_logins(http), ["sessiongenerate"])   # no new sessions burned

    def test_browser_cookies_used_as_is(self):
        self.archive.save_session("riffraff", {"ljmastersession": "m", "ljloggedin": "u:s",
                                               "ljuniq": "x"}, source="browser", variant="all")
        http = FakeHTTP(web_accepts="master")
        b = self.backup(http)
        b.login()
        b.comments()
        self.assertEqual(http.cookies_set.get("ljuniq"), "x")
        self.assertEqual([c for c in http.calls if c[0] == "export_bounced"], [])

    def test_rerun_lists_only_changes(self):
        http = FakeHTTP(cookie_auth=False)
        b = self.backup(http)
        b.login()
        b.entries()
        http2 = FakeHTTP(cookie_auth=False)
        b2 = self.backup(http2)
        b2.login()
        b2.entries()
        b2.connections()
        sync = [c for c in http2.calls if c[0] == "syncitems"]
        self.assertEqual(len(sync), 1)
        self.assertEqual(sync[0][4], "2005-02-01 10:00:00")    # newest entry time seen
        self.assertEqual([c for c in http2.calls if c[1] == "cookie"], [])  # learned setting obeyed

    def test_empty_password_sends_nothing(self):
        from livelonger.ljapi import StopRun
        http = FakeHTTP()
        with self.assertRaises(StopRun):
            self.backup(http, pw="").login()
        self.assertEqual(http.calls, [])

    def _ones(self, http):
        return sum(1 for c in http.calls if c[3] == "one")

    def test_failed_chunk_only_affects_that_chunk(self):
        http = FakeHTTP(n_events=150)
        http.faults[("getevents", "multiple")] = [302]   # not "unsupported"
        b = self.backup(http)
        b.login()
        b.entries()
        self.assertEqual(len(b.j.entry_ids()), 150)
        self.assertEqual(self._ones(http), 100)  # only the failed chunk of 100; the rest batched

    def test_unsupported_multiple_goes_one_by_one(self):
        http = FakeHTTP(n_events=150)
        http.faults[("getevents", "multiple")] = [203]
        b = self.backup(http)
        b.login()
        b.entries()
        self.assertEqual(len(b.j.entry_ids()), 150)
        self.assertEqual(self._ones(http), 149)

    def test_entries_saved_even_if_run_dies(self):
        from livelonger.ljapi import StopRun
        http = FakeHTTP(n_events=150)
        http.faults[("getevents", "multiple")] = [402]   # banned mid-run
        b = self.backup(http)
        b.login()
        with self.assertRaises(StopRun):
            b.entries()
        # entry 1 arrived before the ban and was saved, and so was its sync state
        self.assertEqual(b.j.entry_ids(), [261])
        self.assertIn("1", b.j.state["item_times"])

    def test_comment_pages_persist_and_resume(self):
        from livelonger.http import FetchError
        http = FakeHTTP()
        b = self.backup(http)
        b.login()
        b.entries()
        http.body_fail_at = 3
        with self.assertRaises(FetchError):
            b.comments()
        self.assertEqual(sorted(b.j.get("comment_bodies")), ["1", "2"])
        http.calls.clear()
        b.comments()
        self.assertEqual([c for c in http.calls if c[0] == "comment_body"], [("comment_body", 3)])
        self.assertEqual(len(b.j.load_comments(261)), 2)


class HTTPTests(unittest.TestCase):
    """Real sockets against a local server: keep-alive, cookies, gzip, cache."""

    @classmethod
    def setUpClass(cls):
        import gzip as _gz
        import http.server
        import threading

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                body = (self.headers.get("Cookie") or "").encode()
                extra = {}
                if self.path == "/set":
                    extra["Set-Cookie"] = "flavor=oat; Path=/"
                if self.path == "/gz":
                    body = _gz.compress(b"zipped!")
                    extra["Content-Encoding"] = "gzip"
                if self.path.startswith("/loop"):
                    self.send_response(302)
                    self.send_header("Location", "/loop?v=SECRETSESSIONKEY")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/set")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                for k, v in extra.items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def client(self, cache=None):
        from livelonger.http import PoliteClient
        return PoliteClient("test", delay=0, other_delay=0, cache_dir=cache, log=lambda *a: None)

    def test_keepalive_cookies_redirect_gzip(self):
        c = self.client()
        c.get(self.base + "/redirect")             # 302 -> /set, sets a cookie
        r = c.get(self.base + "/echo")
        self.assertEqual(r.text, "flavor=oat")     # cookie sent back
        self.assertEqual(c.get(self.base + "/gz").text, "zipped!")
        self.assertEqual(c.request_count, 4)
        self.assertEqual(c.connection_count, 1)    # one connection for everything
        c.close()

    def test_reconnects_when_server_drops_idle_connection(self):
        c = self.client()
        c.get(self.base + "/a")
        for conn in c._conns.values():
            conn.sock.close()                      # simulate the server hanging up
        self.assertEqual(c.get(self.base + "/b").status, 200)
        c.close()

    def test_redirect_loop_reports_chain_without_secrets(self):
        from livelonger.http import FetchError
        c = self.client()
        with self.assertRaises(FetchError) as cm:
            c.get(self.base + "/loop")
        msg = str(cm.exception)
        self.assertIn("too many redirects", msg)
        self.assertIn("/loop?v", msg)
        self.assertNotIn("SECRETSESSIONKEY", msg)
        c.close()

    def test_login_bounces_are_not_cached(self):
        with tempfile.TemporaryDirectory() as d:
            c = self.client(Path(d))
            c.get(self.base + "/redirect", cache=True)          # ordinary redirect: cached
            self.assertTrue(any(Path(d).rglob("*.json")))
            from livelonger.http import Response
            n = len(list(Path(d).rglob("*.json")))
            c._fetch = lambda *a, **k: Response("https://www.livejournal.com/login.bml?returnto=x",
                                                200, {}, b"Log in")
            c.get("https://pd.livejournal.com/5.html", cache=True)
            self.assertEqual(len(list(Path(d).rglob("*.json"))), n)   # login page not cached
            c.close()

    def test_cache_is_per_login(self):
        with tempfile.TemporaryDirectory() as d:
            c = self.client(Path(d))
            url = "https://riffraff.livejournal.com/1.html"
            self.assertEqual(c.auth_fingerprint(), "")
            c.set_cookie("ljsession", "A")
            fa = c.auth_fingerprint()
            c.set_cookie("ljsession", "B")
            self.assertNotEqual(fa, c.auth_fingerprint())
            self.assertNotEqual(c._cache_paths("GET", url, None, fa),
                                c._cache_paths("GET", url, None, c.auth_fingerprint()))


class LayoutAndSpamTests(unittest.TestCase):
    def test_site_layout_urls(self):
        from livelonger.web import site_layout
        u = "https://pd.livejournal.com/1.html"
        self.assertEqual(site_layout(u), u + "?style=site")
        self.assertEqual(site_layout(u, page=2), u + "?style=site&page=2")
        self.assertEqual(site_layout(u, thread=77), u + "?style=site&thread=77")

    def test_journal_styled_page_still_yields_body(self):
        page = ('<script>Site.entry = {"title":"T","is_public":false,"eventtime":1};'
                'Site.page = {"replycount":3,"comments":[]};</script>'
                '<div class="asset-content"><div class="asset-body">Bloom body</div></div>')
        p = web.parse_entry_page(page, "pd", 9, "u")
        self.assertEqual(p["entry"]["body"], "Bloom body")
        self.assertEqual(p["entry"]["layout"], "journal-style")
        self.assertTrue(p["comments_unreadable"])

    def test_s2_comments_margin_and_nested(self):
        from livelonger.web import parse_s2_comments, parse_s2_date
        margin = (
            '<div id="ljcmt10" class="comment-wrap" style="margin-left:0px;"><span data-ljuser="kim"></span>'
            '<img src="https://l-userpic.livejournal.com/1/2"><span class="comment-datetimelink">2009-08-02 09:54 am (local)</span>'
            '<div class="comment-text">first</div></div>'
            '<div id="ljcmt11" style="margin-left:25px;"><span data-ljuser="pd"></span>'
            '<span class="comment-datetimelink">2009-08-02 02:52 pm</span><div class="comment-text">reply</div></div>'
            '<div id="ljcmt12" style="margin-left:50px;"><div class="comment-text">anon deep</div></div>'
            '<div id="ljcmt13" style="margin-left:0px;"><div class="comment-text">second root</div></div>')
        cs = parse_s2_comments(htmltree.parse(margin), 1)
        self.assertEqual([(c["id"], c["parent"]) for c in cs], [(10, 0), (11, 10), (12, 11), (13, 0)])
        self.assertEqual(cs[0]["date"], "2009-08-02 09:54:00")
        self.assertEqual(cs[1]["date"], "2009-08-02 14:52:00")
        self.assertEqual((cs[0]["poster"], cs[2]["poster"]), ("kim", None))
        self.assertTrue(cs[0]["userpic_url"].startswith("https://l-userpic"))
        nested = ('<div id="ljcmt20"><div class="comment-body">outer</div>'
                  '<div id="ljcmt21"><div class="comment-body">inner</div></div></div>')
        cs = parse_s2_comments(htmltree.parse(nested), 1)
        self.assertEqual([(c["id"], c["parent"], c["body"]) for c in cs],
                         [(20, 0, "outer"), (21, 20, "inner")])
        self.assertEqual(parse_s2_date("Apr. 9th, 2013 at 2:08 PM"), "2013-04-09 14:08:00")

    def test_spam_hidden_from_site(self):
        with tempfile.TemporaryDirectory() as d:
            a = Archive(Path(d) / "a")
            j = a.journal("kim")
            j.update_meta(username="kim", source="api")
            j.save_entry({"source": "api", "ditemid": 256, "itemid": 1, "anum": 0, "body": "hi",
                          "body_format": "lj", "eventtime": "2005-01-01 00:00:00", "subject": "",
                          "security": "public", "comment_count": 2})
            j.save_comments(256, [{"source": "api", "id": 1, "parent": 0, "state": "A", "body": "real",
                                   "body_format": "lj", "poster": "pd"},
                                  {"source": "api", "id": 2, "parent": 0, "state": "B", "body": "BUY PILLS",
                                   "body_format": "lj", "poster": None}])
            SiteBuilder(a, Path(d) / "s", log=lambda *x: None).build()
            page = (Path(d) / "s" / "kim" / "256.html").read_text()
            self.assertIn("real", page)
            self.assertNotIn("BUY PILLS", page)
            self.assertIn("1 comment<", page)
            self.assertNotIn("PILLS", (Path(d) / "s" / "kim" / "search-data.js").read_text())


class LenientExportTests(unittest.TestCase):
    def test_parse_levels(self):
        from livelonger.ljapi import parse_export_xml
        ok = b"<livejournal><comments><comment id='1'><body>ok</body></comment></comments></livejournal>"
        self.assertEqual(parse_export_xml(ok)[1], "")
        win = b"<livejournal><comments><comment id='1'><body>\x93hi\x94\x0b</body></comment></comments></livejournal>"
        root, note = parse_export_xml(win)
        self.assertEqual(root.find(".//body").text, "\u201chi\u201d")
        self.assertIn("repaired", note)
        broken = (b"<livejournal><maxid>2</maxid><comments><comment id='1'><body>a</body></comment>"
                  b"<comment id='2'><body><b></body></comment></comments></livejournal>")
        root, note = parse_export_xml(broken)
        self.assertEqual([c.get("id") for c in root.iter("comment")], ["1"])
        self.assertEqual(root.findtext("maxid"), "2")
        self.assertIn("1 unreadable", note)


class JournalLoginTests(unittest.TestCase):
    class Web:
        """A journal subdomain that recognises only one cookie combination."""

        def __init__(self, accepts):
            self.accepts, self.cookies, self.gets = accepts, {}, 0
            self.log = lambda *a: None

        def set_cookie(self, k, v, domain=None):
            self.cookies[k] = v

        def remove_cookie(self, k):
            self.cookies.pop(k, None)

        def get(self, url, **kw):
            self.gets += 1
            present = {k for k in self.cookies if k in ("ljsession", "ljmastersession", "ljloggedin")}
            ok = {"all": len(present) == 3, "ljsession": present == {"ljsession"}}.get(self.accepts, False)
            remote = '{"username":"riffraff"}' if ok else "null"
            return Response(url, 200, {"Content-Type": "text/html"},
                            f"<script>Site.remote = {remote};</script>".encode())

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.archive = Archive(Path(self.tmp.name))
        self.archive.save_session("riffraff", {"ljsession": "s", "ljmastersession": "s",
                                               "ljloggedin": "u:s"}, source="api", variant="ljsession")

    def tearDown(self):
        self.tmp.cleanup()

    def test_finds_and_remembers_journal_style(self):
        from livelonger.webauth import ensure_journal_login
        web = self.Web("all")
        self.assertTrue(ensure_journal_login(web, self.archive, "riffraff", "poeticdream", log=lambda *a: None))
        self.assertEqual(self.archive.session_info("riffraff")["journal_variant"], "all")
        self.assertEqual(self.archive.session_info("riffraff")["variant"], "ljsession")  # www style kept
        web2 = self.Web("all")
        ensure_journal_login(web2, self.archive, "riffraff", "poeticdream", log=lambda *a: None)
        self.assertEqual(web2.gets, 1)   # remembered: first try works

    def test_warns_when_nothing_works(self):
        from livelonger.webauth import ensure_journal_login
        msgs = []
        self.assertFalse(ensure_journal_login(self.Web(None), self.archive, "riffraff", "poeticdream",
                                              log=msgs.append))
        self.assertIn("--cookies", msgs[0])


class MediaHostTests(unittest.TestCase):
    def test_gives_up_on_unresponsive_host(self):
        from livelonger.http import FetchError
        from livelonger.media import MediaStore

        class Dead:
            calls = 0

            def get(self, url, **kw):
                Dead.calls += 1
                raise FetchError("network error: RemoteDisconnected", url=url)

        with tempfile.TemporaryDirectory() as d:
            store = MediaStore(Archive(Path(d)))
            for i in range(5):
                store.fetch(Dead(), f"http://old.example/{i}.jpg", log=lambda *a: None)
            self.assertEqual(Dead.calls, 2)                 # then the host is skipped
            self.assertEqual(store.index, {})               # and nothing marked permanently failed


class BanTests(unittest.TestCase):
    def test_ban_fault_stops_run(self):
        from livelonger.http import Blocked
        from livelonger.ljapi import LJClient

        class Banned(FakeHTTP):
            def post(self, url, data, headers=None, **kw):
                fault = xmlrpc.client.Fault(402, "Client error:  Your IP address is temporarily banned "
                                                 "for exceeding the login failure rate.")
                return Response(url, 200, {}, xmlrpc.client.dumps(fault, methodresponse=True).encode())

        with self.assertRaises(Blocked):
            LJClient(Banned(), "x", "pw", log=lambda *a: None).challenge()


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
