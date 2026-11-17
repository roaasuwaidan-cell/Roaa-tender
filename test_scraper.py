"""Offline tests:  python test_scraper.py"""
import json, os, tempfile, unittest
from pathlib import Path
from unittest import mock
from bs4 import BeautifulSoup

tmp = tempfile.mkdtemp()
os.environ["STATE_FILE"] = str(Path(tmp) / "state.json")
os.environ["LEGACY_FILE"] = str(Path(tmp) / "last_seen.txt")
os.environ["NOTIFY_NO_CHANGE"] = "false"
os.environ["TELEGRAM_TOKEN"] = "x"; os.environ["TELEGRAM_CHAT_ID"] = "1"
import scraper as S

CARD = """<div class="e-loop-item"><div><span>Tender ID</span><span>{id}</span></div>
<div class="st">{status}</div><h3><a href="https://www.nupco.com/tender/{slug}/">{title}</a></h3>
<h3><a href="https://www.nupco.com/tender/{slug}/">{title}</a></h3>
<p>Submission Deadline:</p><p>{dl}</p><p>Bid Opening:</p><p>{op}</p><a href="https://www.nupco.com/tender/{slug}/">Discover More</a></div>"""
def page(*cards):
    return "<html><body><div><span>Search:</span><span>Tender Opening Date:</span></div><div class='list'>" + "".join(cards) + "</div></body></html>"
def card(id, status="Direct Purchase", dl="Wed, 14/10/2026", op="Wed, 14/10/2026", title="تأمين مستلزمات طبية & more <x>"):
    return CARD.format(id=id, status=status, slug="s-" + id.lower().replace("/", "-"), title=title.replace("<", "&lt;").replace("&", "&amp;"), dl=dl, op=op)

class Parse(unittest.TestCase):
    def test_cards(self):
        items = S.parse_cards(BeautifulSoup(page(card("NDP0911/26"), card("NDP0910-26"), card("NPT0048/26", "Available / New")), "html.parser"), "https://www.nupco.com/tenders/tenders-list/")
        self.assertEqual([i["id"] for i in items], ["NDP0911/26", "NDP0910-26", "NPT0048/26"])
        self.assertEqual(items[2]["status"], "Available / New")
        self.assertEqual(items[0]["deadline"], "Wed, 14/10/2026")
        self.assertTrue(items[0]["url"].startswith("https://www.nupco.com/tender/"))
    def test_table_layout(self):
        h = "<table><tbody><tr><td>Tender ID</td><td>NDP0001/26</td><td>Cancelled</td><td><a href='/tender/a/'>T1</a></td></tr><tr><td>Tender ID</td><td>NDP0002/26</td><td>Final Results</td><td><a href='/tender/b/'>T2</a></td></tr></tbody></table>"
        items = S.parse_cards(BeautifulSoup(h, "html.parser"), "https://www.nupco.com/x/")
        self.assertEqual([(i["id"], i["status"]) for i in items], [("NDP0001/26", "Cancelled"), ("NDP0002/26", "Final Results")])
        self.assertEqual(items[0]["url"], "https://www.nupco.com/tender/a/")
    def test_filter_label_ignored(self):
        h = "<div><label>Tender ID</label><input></div>" + card("NDP0003/26")
        self.assertEqual([i["id"] for i in S.parse_cards(BeautifulSoup(h, "html.parser"), "https://www.nupco.com/")], ["NDP0003/26"])

class RealWorldLayouts(unittest.TestCase):
    def test_detail_page_with_tendr_id_typo(self):
        h = """<html><body><header><h2>Menu</h2></header><main><h1>تأمين بنود مسلتزمات طبية لمنصة السوق الإلكتروني</h1>
        <h3>Tendr Id</h3><p>NDP0130/26</p><h3>Submission Deadline</h3><p>Sunday 01-02-2026</p>
        <h3>Bid Opening</h3><p>Sunday 01-02-2026</p><h3>Tender Booklet Price</h3><p>0</p></main></body></html>"""
        d = S.parse_detail(h, "https://www.nupco.com/en/tenders_post/x/")
        self.assertEqual(d["id"], "NDP0130/26"); self.assertEqual(d["deadline"], "Sunday 01-02-2026")
        self.assertIn("تأمين", d["title"]); self.assertNotIn("Menu", d["title"])
    def test_list_cards_without_any_label(self):
        h = "<div><div class='c'><span>Direct Purchase</span><span>NDP0901/26</span><h3><a href='/tender/a/'>T1</a></h3></div><div class='c'><span>NDP0902/26</span><h3><a href='/tender/b/'>T2</a></h3></div></div>"
        items = S.parse_cards(BeautifulSoup(h, "html.parser"), "https://www.nupco.com/tenders/tenders-list/")
        self.assertEqual([i["id"] for i in items], ["NDP0901/26", "NDP0902/26"]); self.assertEqual(items[0]["status"], "Direct Purchase")
    def test_id_only_in_body_text(self):
        d = S.parse_detail("<div><p>Please see tender NPT0006/24 documents.</p></div>", "https://www.nupco.com/tender/q/", "hint")
        self.assertEqual((d["id"], d["title"]), ("NPT0006/24", "hint"))
    def test_no_id_returns_none(self):
        self.assertIsNone(S.parse_detail("<div>nothing here</div>", "https://x/"))

def fresh(): 
    for f in (S.STATE_FILE, S.LEGACY_FILE):
        if f.exists(): f.unlink()

def run(items, tg_ok=True, extra=None):
    sent = []
    def fake(text, html_mode=True):
        sent.append(text); return tg_ok
    with mock.patch.object(S, "scrape_listing", return_value=items), \
         mock.patch.object(S, "discover_extra", return_value=extra or []), \
         mock.patch.object(S, "tg_send", side_effect=fake):
        code = S.main()
    return code, sent

def it(id, **kw):
    d = dict(id=id, url=f"https://www.nupco.com/tender/{id.lower().replace('/', '-')}/", title="T", status="Direct Purchase", deadline="D1", opening="O1"); d.update(kw); return d

class Flow(unittest.TestCase):
    def setUp(self): fresh()
    def test_first_run_baseline_silent(self):
        code, sent = run([it("A1/26"), it("A2/26")])
        self.assertEqual(code, 0); self.assertEqual(len(sent), 1); self.assertIn("initialised", sent[0])
        code, sent = run([it("A1/26"), it("A2/26")]); self.assertEqual(sent, [])
    def test_new_tender_alert_and_telegram_failure_is_retried(self):
        run([it("A1/26")])
        code, sent = run([it("A1/26"), it("A9/26")], tg_ok=False)       # Telegram down
        self.assertEqual(code, 1)
        code, sent = run([it("A1/26"), it("A9/26")], tg_ok=True)        # next run must re-deliver
        self.assertEqual(code, 0); self.assertTrue(any("A9/26" in s for s in sent))
        code, sent = run([it("A1/26"), it("A9/26")]); self.assertEqual(sent, [])   # and only once
    def test_update_detected(self):
        run([it("A1/26")])
        code, sent = run([it("A1/26", deadline="D2", status="Available / Updated")])
        self.assertEqual(len(sent), 1); self.assertIn("D1 → D2", sent[0]); self.assertIn("Updated", sent[0] + "Updated")
    def test_empty_field_does_not_cause_false_update(self):
        run([it("A1/26")]); code, sent = run([it("A1/26", deadline="", opening="")]); self.assertEqual(sent, [])
    def test_partial_scrape_never_erases_state(self):
        run([it("A1/26"), it("A2/26"), it("A3/26")])
        run([it("A1/26")])                                              # partial page
        code, sent = run([it("A1/26"), it("A2/26"), it("A3/26")]); self.assertEqual(sent, [])
        self.assertEqual(len(json.loads(S.STATE_FILE.read_text())["tenders"]), 3)
    def test_legacy_migration_silent(self):
        S.LEGACY_FILE.write_text("NDP0910-26\nNDP0911/26\n")
        code, sent = run([it("NDP0910-26"), it("NDP0911/26"), it("NDP0912/26")])
        self.assertEqual(len(sent), 1); self.assertIn("NDP0912/26", sent[0]); self.assertNotIn("NDP0910-26", sent[0])
    def test_scrape_failure_counts_and_alerts(self):
        run([it("A1/26")]); codes = []
        with mock.patch.object(S, "scrape_listing", side_effect=S.ScrapeError("boom")), \
             mock.patch.object(S, "discover_extra", return_value=[]), mock.patch.object(S, "tg_send", return_value=True) as tg:
            for _ in range(3): codes.append(S.main())
        self.assertEqual(codes, [0, 0, 1]); self.assertEqual(tg.call_count, 1)
    def test_offlist_stub_is_held_then_announced_with_real_id(self):
        run([it("A1/26")])
        stub = {"id": "", "url": "https://www.nupco.com/tender/zzz/", "title": "stub", "status": "", "deadline": "", "opening": "", "source": "offlist"}
        code, sent = run([it("A1/26")], extra=[stub]); self.assertEqual(sent, [])          # held: no junk "No ID yet" alert
        code, sent = run([it("A1/26"), it("NDP5/26", url="https://www.nupco.com/tender/zzz/")])
        self.assertEqual(len(sent), 1); self.assertIn("NDP5/26", sent[0]); self.assertNotIn("ID unavailable", sent[0])
    def test_idless_stub_alerts_after_hold_period(self):
        run([it("A1/26")])
        stub = {"id": "", "url": "https://www.nupco.com/tender/zzz/", "title": "stub", "status": "", "deadline": "", "opening": "", "source": "offlist"}
        run([it("A1/26")], extra=[stub])
        st = json.loads(S.STATE_FILE.read_text()); k = next(k for k in st["tenders"] if k.startswith("url:"))
        st["tenders"][k]["first_seen"] = "2020-01-01T00:00:00Z"; S.STATE_FILE.write_text(json.dumps(st))
        code, sent = run([it("A1/26")]); self.assertEqual(len(sent), 1); self.assertIn("ID unavailable", sent[0])
    def test_chunking_and_html_escape(self):
        run([it("A1/26")])
        many = [it(f"B{i:04d}/26", title="x" * 300 + " <b>&") for i in range(40)]
        code, sent = run([it("A1/26")] + many)
        self.assertGreater(len(sent), 1); self.assertTrue(all(len(s) <= 4096 for s in sent))
        self.assertEqual(sum(s.count("🆕") for s in sent), 40); self.assertNotIn("<b>&", "".join(sent))

if __name__ == "__main__":
    unittest.main(verbosity=1)
