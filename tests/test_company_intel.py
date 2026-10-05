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


XBRL = """<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:in-bse-pit="http://x/pit">
<in-bse-pit:Symbol contextRef="MainI">DAMODARIND</in-bse-pit:Symbol>
<in-bse-pit:NameOfTheCompany contextRef="MainI">DAMODAR INDUSTRIES LIMITED</in-bse-pit:NameOfTheCompany>
<in-bse-pit:CategoryOfPerson contextRef="Disclosure1">Promoter and Director</in-bse-pit:CategoryOfPerson>
<in-bse-pit:NameOfThePerson contextRef="Disclosure1">ARUN KUMAR BIYANI</in-bse-pit:NameOfThePerson>
<in-bse-pit:SecuritiesHeldPriorToAcquisitionOrDisposalPercentageOfShareholding contextRef="Disclosure1">0.3461</in-bse-pit:SecuritiesHeldPriorToAcquisitionOrDisposalPercentageOfShareholding>
<in-bse-pit:SecuritiesAcquiredOrDisposedNumberOfSecurity contextRef="Disclosure1">120000</in-bse-pit:SecuritiesAcquiredOrDisposedNumberOfSecurity>
<in-bse-pit:SecuritiesAcquiredOrDisposedValueOfSecurity contextRef="Disclosure1">0</in-bse-pit:SecuritiesAcquiredOrDisposedValueOfSecurity>
<in-bse-pit:SecuritiesAcquiredOrDisposedTransactionType contextRef="Disclosure1">Buy</in-bse-pit:SecuritiesAcquiredOrDisposedTransactionType>
<in-bse-pit:SecuritiesHeldPostAcquistionOrDisposalPercentageOfShareholding contextRef="Disclosure1">0.3513</in-bse-pit:SecuritiesHeldPostAcquistionOrDisposalPercentageOfShareholding>
<in-bse-pit:ModeOfAcquisitionOrDisposal contextRef="Disclosure1">Gift</in-bse-pit:ModeOfAcquisitionOrDisposal>
<in-bse-pit:CategoryOfPerson contextRef="Disclosure2">Promoter</in-bse-pit:CategoryOfPerson>
<in-bse-pit:NameOfThePerson contextRef="Disclosure2">SOMEONE ELSE</in-bse-pit:NameOfThePerson>
<in-bse-pit:SecuritiesAcquiredOrDisposedNumberOfSecurity contextRef="Disclosure2">50000</in-bse-pit:SecuritiesAcquiredOrDisposedNumberOfSecurity>
<in-bse-pit:SecuritiesAcquiredOrDisposedValueOfSecurity contextRef="Disclosure2">2500000</in-bse-pit:SecuritiesAcquiredOrDisposedValueOfSecurity>
<in-bse-pit:SecuritiesAcquiredOrDisposedTransactionType contextRef="Disclosure2">Sell</in-bse-pit:SecuritiesAcquiredOrDisposedTransactionType>
<in-bse-pit:ModeOfAcquisitionOrDisposal contextRef="Disclosure2">Market Sale</in-bse-pit:ModeOfAcquisitionOrDisposal>
</xbrli:xbrl>"""

RSS = """<?xml version="1.0"?><rss><channel>
<item><title>DAMODAR INDUSTRIES LIMITED</title><description>DAMODARIND|DAMODAR INDUSTRIES LIMITED|Original|Regulation 7 (2)|IT_1.xml|IT_1.html|1|2|-</description><pubDate>05-Oct-2026 20:01:10</pubDate><link>https://nsearchives.nseindia.com/corporate/xbrl/IT_1.xml</link></item>
</channel></rss>"""

ANN = """<?xml version="1.0"?><rss><channel>
<item><title>Talwalkars Better Value Fitness Limited</title><description>Talwalkars has informed the Exchange about Resignation of Managing Director |SUBJECT: Resignation of Managing Director</description><pubDate>05-Oct-2026 18:05:00</pubDate><link>https://nsearchives.nseindia.com/corporate/TW_1.pdf</link></item>
<item><title>eMudhra Limited</title><description>eMudhra has informed the Exchange about Certificate under SEBI (Depositories and Participants) Regulations, 2018 |SUBJECT: Certificate under Reg 74(5)</description><pubDate>05-Oct-2026 18:00:00</pubDate><link>https://nsearchives.nseindia.com/corporate/E_1.pdf</link></item>
</channel></rss>"""


class FakeNSE:
    class R:
        def __init__(self, text): self.text, self.content, self.ok = text, text.encode(), True
        def raise_for_status(self): pass
    def get(self, url, headers=None, timeout=None):
        if url.endswith("IT_1.xml"):
            return self.R(XBRL)
        if "InsiderTrading" in url:
            return self.R(RSS)
        if "Online_announcements" in url:
            return self.R(ANN)
        if "RSS/" in url:
            return self.R("<rss><channel></channel></rss>")
        raise ConnectionError("bse blocked")


def test_insider_xbrl_parsed_into_person_direction_and_size():
    trades, main = CI.insider_details(XBRL)
    assert main["Symbol"] == "DAMODARIND" and len(trades) == 2
    assert trades[0]["person"] == "ARUN KUMAR BIYANI" and trades[0]["type"] == "BUY" and trades[0]["pct_before"] == 34.61
    assert trades[1]["type"] == "SELL" and trades[1]["value_inr"] == 2500000
    sev, why = CI.insider_severity(trades)
    assert sev == 45 and "SOLD" in why                       # a promoter market sale outranks a gift


def test_nse_feeds_and_cross_exchange_dedupe():
    uni = ([("TALWALKARS", "Talwalkars Better Value Fitness Ltd"), ("DAMODARIND", "Damodar Industries Ltd")], "t")
    idx = CI.universe_index(uni[0])
    ev, st = CI.fetch_nse_all(idx, FakeNSE())
    assert st["InsiderTrading"].startswith("ok") and st["Online_announcements"].startswith("ok")
    ins = next(e for e in ev if e["category"] == "INSIDER_TRADE")
    assert ins["symbol"] == "DAMODARIND" and ins["direction"] == "SELL" and "ARUN KUMAR BIYANI" in ins["headline"] and ins["trades"]
    assert not any("eMudhra" in e["company"] for e in ev)       # routine certificate skipped
    # the same MD resignation also filed on BSE -> one event, with the other filing kept as a link
    bse = CI.bse_event(row(1, "Talwalkars Better Value Fitness Ltd", "talwalkars", "Company Update", "Resignation of Managing Director",
                           "Resignation of Ms. X as Managing Director of the company.", t="2026-10-05T18:10:00.000"), idx)
    out = CI.build({}, ev + [bse], NOW)
    tw = [e for e in out["events"] if e["key"] == "TALWALKARS"]
    assert len(tw) == 1 and tw[0]["also_filed"] and tw[0]["severity"] == 70


def test_run_reports_bse_blocked_but_nse_ok():
    out = CI.run({}, now=NOW, universe=UNI, session=FakeNSE(), days=1)
    assert out["coverage"]["nse"]["status"] == "ok" and out["coverage"]["bse"]["status"].startswith("FAILED")
    assert out["events"] and out["coverage"]["nse"]["last_success"]
    light = CI.run(out, now=NOW, universe=UNI, session=FakeNSE(), bse=False)
    assert "full runs" in light["coverage"]["bse"]["note"] and light["new_this_run"] == 0


def test_pledge_release_is_not_a_red_flag_and_large_offmarket_sale_is():
    assert CI.insider_severity([{"category": "Promoter", "type": "PLEDGE REVOKE", "mode": "Pledge Release", "shares": 7731000}])[0] == 15
    sev, why = CI.insider_severity([{"category": "Promoter", "type": "SELL", "mode": "Off Market", "shares": 3668036,
                                     "value_inr": 1507562796, "pct_before": 15.9, "pct_after": 10.8}])
    assert sev >= 55 and "off-market" in why and "large" in why


def test_generic_nse_duplicate_dropped_when_specific_item_exists():
    base = {"company": "Acme Widgets Limited", "symbol": "ACME", "source": "NSE filing", "time": "2026-10-05T18:00:00", "category": "MANAGEMENT_CHANGE",
            "verified": "OFFICIAL EXCHANGE FILING", "pdf": None, "link": None}
    ev = [dict(base, id="a", severity=25, reason="director / KMP / auditor change (details in the filing)", headline="Change in Directors/KMP/SMP/Auditor/RTA"),
          dict(base, id="b", severity=20, reason="management / board appointment", headline="change in Management"),
          dict(base, id="c", severity=45, reason="director resigned", headline="Resignation of Mr Z as Director")]
    out = CI.build({}, ev, NOW)
    assert [e["id"] for e in out["events"]] == ["c"]
