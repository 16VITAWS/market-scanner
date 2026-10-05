"""Company Intelligence: classification, symbol mapping, scoring, clusters, alerts, coverage, no invented data."""
import datetime as dt
from engine import company_intel as CI

NOW = dt.datetime(2026, 10, 5, 21, 0, tzinfo=CI.IST)


def row(i, name, slug, cat, sub, head, t="2026-10-05T18:00:00.000", code=500000):
    return {"NEWSID": f"n{i}", "SCRIP_CD": code + i, "SLONGNAME": name, "CATEGORYNAME": cat, "SUBCATNAME": sub, "HEADLINE": head,
            "NEWSSUB": f"{name} - {code + i} - {sub}", "DissemDT": t, "ATTACHMENTNAME": f"f{i}.pdf",
            "NSURL": f"https://www.bseindia.com/stock-share-price/{name.lower().replace(' ', '-')}/{slug}/{code + i}/", "TotalPageCnt": 1}


class FakeResp:
    def __init__(self, j): self.j = j
    def raise_for_status(self): pass
    def json(self): return self.j


class FakeSession:
    def __init__(self, rows, fail=False): self.rows, self.fail, self.calls = rows, fail, 0
    def get(self, url, headers=None, timeout=None):
        self.calls += 1
        if self.fail:
            raise ConnectionError("blocked")
        page = int(url.split("pageno=")[1].split("&")[0])
        day = url.split("strPrevDate=")[1][:8]
        tab = [r for r in self.rows if r["DissemDT"][:10].replace("-", "") == day] if page == 1 else []
        return FakeResp({"Table": tab})


UNI = ([("TALWALKARS", "Talwalkars Better Value Fitness Ltd"), ("RELIANCE", "Reliance Industries Ltd"), ("ACME", "Acme Widgets Limited")], "test list")


def rows():
    return [
        row(1, "Talwalkars Better Value Fitness Ltd", "talwalkars", "Company Update", "Resignation of Managing Director", "Resignation of Ms. X as Managing Director of the company."),
        row(2, "Talwalkars Better Value Fitness Ltd", "talwalkars", "Company Update", "Resignation of Director", "Resignation of Mr. Y, Independent Director"),
        row(3, "Talwalkars Better Value Fitness Ltd", "talwalkars", "Company Update", "Change in Management", "Resignation of Mr. Y, Independent Director"),  # duplicate filing
        row(4, "Acme Widgets Limited", "acmewid", "Insider Trading / SAST", "Disclosures under Reg. 31(1) and 31(2) of SEBI (SAST) Regulations, 2011", "Disclosure received"),
        row(5, "Some Small Co Ltd", "smallco", "Company Update", "General", "Company received a show cause notice from SEBI"),
        row(6, "Reliance Industries Ltd", "reliance", "Company Update", "General", "Compliances-Certificate under Regulation 74(5)"),  # routine -> skipped
        row(7, "Reliance Industries Ltd", "reliance", "Insider Trading / SAST", "Closure of Trading Window", "Trading window closure"),  # routine
        row(8, "Fraudy Ltd", "fraudy", "Company Update", "General", "Initiation of forensic audit by lenders"),
    ]


def test_classify_examples_from_real_bse_wording():
    c = CI.classify
    assert c("Company Update", "Resignation of Managing Director", "Resignation of Ms. M as Managing Director of the company.")[:2] == ("MANAGEMENT_CHANGE", 70)
    assert c("Company Update", "Appointment of Statutory Auditor/s", "Appointment of M/s. NG as Statutory Auditor to fill the casual vacancy")[0] == "AUDITOR"
    assert c("Company Update", "General", "Disclosure under Regulation 7(2) read with Regulation 6(2) of SEBI Prohibition of Insider Trading Regulations")[0] == "INSIDER_TRADE"
    assert c("Company Update", "General", "Intimation under Regulation 7(1) of SEBI LODR") is None
    assert c("Insider Trading / SAST", "Closure of Trading Window", "x") is None
    assert c("Company Update", "General", "Kreon sold 0.57% - Disclosure Under SEBI (Substantial Acquisition Of Shares And Takeover) Regulations")[1] == 30
    assert c("Company Update", "Change in Management", "Reappointment of Director, who retires by rotation")[1] == 10
    assert c("Company Update", "General", "JSW Energy capacity reaches 15 GW") is None


def test_symbol_mapping_slug_then_name():
    idx = CI.universe_index(UNI[0])
    assert CI.map_symbol("Reliance Industries Ltd", "RELIANCE", idx) == ("RELIANCE", "exact")
    assert CI.map_symbol("Acme Widgets Ltd", "ACMEWID", idx) == ("ACME", "name")
    assert CI.map_symbol("Unknown Co Ltd", "UNK", idx) == (None, "none")


def test_full_run_events_profiles_alerts_and_provenance():
    out = CI.run({}, now=NOW, universe=UNI, watch=["ACME"], session=FakeSession(rows()), days=1)
    ev = out["events"]
    assert all(e["source"] == "BSE filing" and e["verified"].startswith("OFFICIAL") and e["pdf"].endswith(".pdf") for e in ev)
    assert not any(e["company"] == "Reliance Industries Ltd" for e in ev)          # routine filings dropped
    assert sum(1 for e in ev if "Independent Director" in e["headline"]) == 1      # duplicate merged
    tw = next(p for p in out["profiles"] if p["symbol"] == "TALWALKARS")
    assert tw["counts"]["MANAGEMENT_CHANGE"] == 2 and any(c["type"] == "MANAGEMENT_EXITS" for c in tw["clusters"])
    assert tw["level"] in ("WATCH", "HIGH")
    al = {a["company"]: a for a in out["alerts"]}
    assert al["Acme Widgets Limited"]["yours"] is True                           # pledge on a held stock alerts even below 65
    assert "Fraudy Ltd" in al and al["Fraudy Ltd"]["severity"] >= 80
    assert out["coverage"]["bse"]["status"] == "ok" and out["limits"] and out["method"]


def test_history_merges_and_failed_fetch_keeps_old_data_and_says_so():
    first = CI.run({}, now=NOW, universe=UNI, session=FakeSession(rows()), days=1)
    later = NOW + dt.timedelta(days=1)
    second = CI.run(first, now=later, universe=UNI, session=FakeSession([], fail=True), days=1)
    assert len(second["events"]) == len(first["events"]) and second["new_this_run"] == 0
    assert second["coverage"]["bse"]["status"].startswith("FAILED")
    assert second["coverage"]["bse"]["last_success"] is None or second["coverage"]["bse"]["last_success"] <= later.isoformat()


def test_old_events_expire_and_scores_decay():
    old = CI.run({}, now=NOW, universe=UNI, session=FakeSession(rows()), days=1)
    s_now = next(p for p in old["profiles"] if p["symbol"] == "TALWALKARS")["score"]
    aged = CI.build(old, [], NOW + dt.timedelta(days=60))
    s_aged = next(p for p in aged["profiles"] if p["symbol"] == "TALWALKARS")["score"]
    assert s_aged < s_now / 3
    assert CI.build(old, [], NOW + dt.timedelta(days=CI.KEEP_DAYS + 1))["events"] == []


def test_media_news_marked_unverified():
    def fake_fetch(q, days, limit):
        return [{"title": "Acme Widgets CEO resigns amid probe - Paper", "link": "https://x/1", "published_utc": "2026-10-05T10:00:00+00:00", "source": "Paper"}], "ok"
    ev, st = CI.media_news([("Acme Widgets Limited", "ACME")], fake_fetch)
    assert ev and ev[0]["verified"].startswith("MEDIA REPORT") and ev[0]["pdf"] is None and ev[0]["severity"] < 70
