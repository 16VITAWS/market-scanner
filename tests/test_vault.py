"""Owner encryption of private portal files: round trip, fail-safe on missing/wrong key, key rotation, public files untouched."""
import os, json, tempfile, pytest
from engine import vault as V

KEY = {"PORTAL_KEY": "correct horse battery staple"}


def _site():
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "api"))
    os.makedirs(os.path.join(d, "data", "backups"))
    json.dump({"accounts": {"IN-SWING": {"cash": "1"}}}, open(os.path.join(d, "data", "ledger.json"), "w"))
    json.dump({"b": 1}, open(os.path.join(d, "data", "backups", "ledger-1.json"), "w"))
    json.dump({"generated_at": "x", "accounts": {}}, open(os.path.join(d, "api", "paper.json"), "w"))
    json.dump({"quotes": {"NIFTY": {"p": 1}}}, open(os.path.join(d, "api", "quotes.json"), "w"))
    json.dump({"kill": False}, open(os.path.join(d, "api", "live_control.json"), "w"))
    return d


def rd(d, rel):
    return json.load(open(os.path.join(d, rel)))


def test_off_without_key_changes_nothing():
    d = _site()
    assert V.lock(d, env={}) == 0
    assert rd(d, "api/paper.json")["accounts"] == {} and rd(d, "api/vault.json")["enabled"] is False


def test_round_trip_and_public_files_stay_readable():
    d = _site()
    V.unlock(d, env=KEY)
    assert V.lock(d, env=KEY) == 3
    assert V.is_sealed(rd(d, "api/paper.json")) and V.is_sealed(rd(d, "data/ledger.json")) and V.is_sealed(rd(d, "data/backups/ledger-1.json"))
    assert rd(d, "api/quotes.json")["quotes"]["NIFTY"]["p"] == 1 and rd(d, "api/live_control.json")["kill"] is False
    meta = rd(d, "api/vault.json")
    assert meta["enabled"] and meta["iter"] == V.ITER and "IN-SWING" not in open(os.path.join(d, "data", "ledger.json")).read()
    assert V.lock(d, env=KEY) == 0                                    # sealing twice never double-encrypts
    V.unlock(d, env=KEY)
    assert rd(d, "data/ledger.json")["accounts"]["IN-SWING"]["cash"] == "1"


def test_missing_or_wrong_key_stops_without_touching_files():
    d = _site()
    V.unlock(d, env=KEY); V.lock(d, env=KEY)
    before = open(os.path.join(d, "data", "ledger.json")).read()
    with pytest.raises(V.VaultError):
        V.unlock(d, env={})
    with pytest.raises(V.VaultError):
        V.unlock(d, env={"PORTAL_KEY": "a different long phrase"})
    assert open(os.path.join(d, "data", "ledger.json")).read() == before


def test_key_rotation_with_old_key():
    d = _site()
    V.unlock(d, env=KEY); V.lock(d, env=KEY)
    new = {"PORTAL_KEY": "a brand new pass phrase 2026", "PORTAL_KEY_OLD": KEY["PORTAL_KEY"]}
    V.unlock(d, env=new); V.lock(d, env=new)
    V.unlock(d, env={"PORTAL_KEY": new["PORTAL_KEY"]})
    assert rd(d, "api/paper.json")["generated_at"] == "x"


def test_tampered_or_moved_file_is_rejected():
    d = _site()
    V.unlock(d, env=KEY); V.lock(d, env=KEY)
    env = rd(d, "data/ledger.json")
    json.dump(env, open(os.path.join(d, "api", "paper.json"), "w"))     # a sealed file copied under another name
    with pytest.raises(V.VaultError):
        V.unlock(d, env=KEY)


def test_short_key_refused():
    with pytest.raises(V.VaultError):
        V.lock(_site(), env={"PORTAL_KEY": "short"})


def test_private_list():
    assert V.is_private("api/paper.json") and V.is_private("data/runs/2026-10-06.json")
    assert not V.is_private("api/signals.json") and not V.is_private("api/candles/NIFTY.json") and not V.is_private("data/runs/x/y.json")
