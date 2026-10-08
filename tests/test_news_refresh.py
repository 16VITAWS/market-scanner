"""Headlines refresh on their own (hourly job + live sessions) and a failed fetch never wipes the last good ones."""
import os, json, tempfile
from engine import run as RUN


def test_refresh_keeps_old_headlines_when_fetch_fails(monkeypatch):
    site = RUN.Site(tempfile.mkdtemp())
    monkeypatch.setattr(RUN.NEWS, "market_news", lambda: {"items": [{"title": "Nifty ends higher", "published_utc": "2026-10-08T10:00:00Z"}]})
    assert RUN.refresh_news(site)
    d = json.load(open(os.path.join(site.path, "api", "news.json")))
    assert d["live"]["items"][0]["title"] == "Nifty ends higher" and d["live"]["refreshed_at"] and "legacy" in d
    monkeypatch.setattr(RUN.NEWS, "market_news", lambda: {"items": []})               # source returned nothing
    assert not RUN.refresh_news(site)
    def boom():
        raise RuntimeError("network down")
    monkeypatch.setattr(RUN.NEWS, "market_news", boom)
    assert not RUN.refresh_news(site)
    d = json.load(open(os.path.join(site.path, "api", "news.json")))
    assert d["live"]["items"][0]["title"] == "Nifty ends higher"                      # last good headlines kept
    assert not RUN.refresh_news(site, offline=True)
