"""
Owner-only encryption of PERSONAL portal files (paper accounts, trades, alerts, approvals, run records).

Market data (prices, signals, calendar, options model, kill switch) stays public: the laptop program reads it
without a key, and it is public information anyway. Everything about YOUR accounts is sealed with AES-256-GCM.

Key: the GitHub secret PORTAL_KEY (a long pass-phrase only you know). It is stretched with PBKDF2-SHA256
(600,000 rounds, salt stored in api/vault.json) into a 256-bit key. The portal asks for the same pass-phrase
and derives the same key inside the browser, so the files are only readable after you sign in.

Changing the pass-phrase: put the OLD one in PORTAL_KEY_OLD and the new one in PORTAL_KEY for one run; files are
opened with the old key and sealed again with the new one. Then delete PORTAL_KEY_OLD.

Safety: if sealed files exist but cannot be opened (secret missing or wrong), every job STOPS before touching
anything - it never starts a fresh ledger over your history.

No PORTAL_KEY set -> encryption is OFF and nothing changes (api/vault.json says enabled: false).
"""
import os, sys, json, glob, base64, hashlib, fnmatch

ITER = 600_000
CHECK_TEXT = "16VITAWS-VAULT-OK"
VERSION = 1
# paths relative to the site folder; '*' never crosses a folder
PRIVATE = [
    "api/paper.json", "api/brain.json", "api/notifications.json", "api/live_proposals.json", "api/live_results.json",
    "data/ledger.json", "data/backups/*.json", "data/runs/*.json", "data/approvals.json", "data/notified.json",
    "data/repairs.json", "data/delivery.json",
]
META = "api/vault.json"


class VaultError(RuntimeError):
    pass


def _b64(b):
    return base64.b64encode(b).decode()


def _unb64(s):
    return base64.b64decode(s)


def derive(passphrase, salt, iterations=ITER):
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, iterations, dklen=32)


def seal(data: bytes, key: bytes, aad: str) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    iv = os.urandom(12)
    ct = AESGCM(key).encrypt(iv, data, aad.encode())
    return {"vault": VERSION, "alg": "AES-256-GCM", "aad": aad, "iv": _b64(iv), "ct": _b64(ct),
            "note": "Private file - sign in to the portal to read it."}


def unseal(env: dict, key: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM(key).decrypt(_unb64(env["iv"]), _unb64(env["ct"]), env.get("aad", "").encode())


def is_sealed(obj):
    return isinstance(obj, dict) and obj.get("vault") == VERSION and "ct" in obj and "iv" in obj


def is_private(rel):
    rel = rel.replace(os.sep, "/")
    for p in PRIVATE:
        if fnmatch.fnmatch(rel, p) and rel.count("/") == p.count("/"):
            return True
    return False


def _files(site):
    for p in glob.glob(os.path.join(site, "**", "*.json"), recursive=True):
        rel = os.path.relpath(p, site).replace(os.sep, "/")
        if rel.startswith(".git/"):
            continue
        yield p, rel


def _read_json(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa
        return None


def _meta(site):
    m = _read_json(os.path.join(site, META))
    return m if isinstance(m, dict) else {}


def _keys(meta, env=None):
    env = os.environ if env is None else env
    salt = _unb64(meta["salt"]) if meta.get("salt") else None
    it = int(meta.get("iter") or ITER)
    out = []
    for name in ("PORTAL_KEY", "PORTAL_KEY_OLD"):
        pw = (env.get(name) or "").strip()
        if pw and salt:
            out.append((name, derive(pw, salt, it)))
    return out


def unlock(site, env=None, log=print):
    """Open every sealed file in place. Raises VaultError if any sealed file cannot be opened."""
    env = os.environ if env is None else env
    _ensure_salt(site, env)
    sealed = [(p, rel, o) for p, rel in _files(site) for o in [_read_json(p)] if is_sealed(o)]
    if not sealed:
        return 0
    meta = _meta(site)
    if not (env.get("PORTAL_KEY") or "").strip():
        raise VaultError(f"{len(sealed)} private files are encrypted but the GitHub secret PORTAL_KEY is not set. "
                         "Stopping so your paper history is not replaced. Add the secret back (Settings > Secrets > Actions).")
    keys = _keys(meta, env)
    if not keys:
        raise VaultError("api/vault.json (salt) is missing, so the encrypted files cannot be opened. Stopping.")
    bad = []
    for p, rel, o in sealed:
        for name, k in keys:
            try:
                if o.get("aad") != rel:
                    raise ValueError("file was moved or renamed")
                data = unseal(o, k)
                break
            except Exception:  # noqa
                data = None
        if data is None:
            bad.append(rel)
            continue
        with open(p + ".tmp", "wb") as f:
            f.write(data)
        os.replace(p + ".tmp", p)
    if bad:
        raise VaultError(f"PORTAL_KEY does not open {len(bad)} private files (e.g. {bad[0]}). If you changed the pass-phrase, "
                         "put the previous one in the secret PORTAL_KEY_OLD for one run. Stopping without changes.")
    log(f"vault: opened {len(sealed)} private files")
    return len(sealed)


def _ensure_salt(site, env):
    """First run with a key: fix the salt now, so files sealed during the run (live copies) and at the end match."""
    meta = _meta(site)
    if (env.get("PORTAL_KEY") or "").strip() and not meta.get("salt"):
        _write_meta(site, {"enabled": False, "salt": _b64(os.urandom(16)), "iter": ITER, "note": "being enabled"})


def seal_copy(site, folder, env=None):
    """Seal the private files inside a copy of part of the site (e.g. the live-data branch) with the site's key."""
    m = os.path.join(site, META)
    if os.path.exists(m):
        os.makedirs(os.path.join(folder, "api"), exist_ok=True)
        with open(m, "rb") as a, open(os.path.join(folder, META), "wb") as b:
            b.write(a.read())
    return lock(folder, env, log=lambda *a: None)


def _write_meta(site, meta):
    p = os.path.join(site, META)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".tmp", "w") as f:
        json.dump(meta, f, separators=(",", ":"))
    os.replace(p + ".tmp", p)


def lock(site, env=None, log=print):
    """Seal every private file in place (no-op, but records enabled:false, when PORTAL_KEY is not set)."""
    env = os.environ if env is None else env
    pw = (env.get("PORTAL_KEY") or "").strip()
    meta = _meta(site)
    if not pw:
        _write_meta(site, {"enabled": False, "note": "Encryption is off: set the GitHub secret PORTAL_KEY to make your accounts private."})
        return 0
    if len(pw) < 12:
        raise VaultError("PORTAL_KEY is too short - use at least 12 characters (a sentence of 4-5 words is best).")
    salt = _unb64(meta["salt"]) if meta.get("salt") else os.urandom(16)
    it = int(meta.get("iter") or ITER)
    key = derive(pw, salt, it)
    n = 0
    for p, rel in _files(site):
        if not is_private(rel):
            continue
        with open(p, "rb") as f:
            raw = f.read()
        try:
            if is_sealed(json.loads(raw)):
                continue
        except Exception:  # noqa
            pass
        env_obj = seal(raw, key, rel)
        with open(p + ".tmp", "w") as f:
            json.dump(env_obj, f, separators=(",", ":"))
        os.replace(p + ".tmp", p)
        n += 1
    _write_meta(site, {"enabled": True, "v": VERSION, "kdf": "PBKDF2-SHA256", "iter": it, "salt": _b64(salt),
                       "check": seal(CHECK_TEXT.encode(), key, "check"), "private": PRIVATE,
                       "note": "Your accounts, trades, alerts and approvals are encrypted. Market data is public."})
    log(f"vault: sealed {n} private files")
    return n


def load(path, rel=None, default=None, env=None):
    """Read a JSON file, opening it if sealed (for code that runs while files may still be sealed)."""
    o = _read_json(path)
    if o is None:
        return default
    if not is_sealed(o):
        return o
    site = os.path.dirname(os.path.dirname(path)) if rel is None else path[: -len(rel)].rstrip("/\\")
    for _, k in _keys(_meta(site), env):
        try:
            return json.loads(unseal(o, k))
        except Exception:  # noqa
            continue
    raise VaultError(f"cannot open sealed file {path}")


if __name__ == "__main__":
    cmd, site = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "site")
    try:
        (unlock if cmd == "unlock" else lock)(site)
    except VaultError as e:
        sys.exit(f"VAULT: {e}")
