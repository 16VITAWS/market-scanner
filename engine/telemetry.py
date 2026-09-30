"""
Pipeline + provider telemetry -> api/pipeline.json (read by the Data Health page). Real measurements only.

jobs[]     one entry per job run: job, started, finished, seconds, status (OK / PARTIAL / FAILED), processed, failed,
           provider(s), error (no secrets), host (github-actions / local)
last_ok    {job: finished time of the last successful run}
providers  {id: {requests, errors, last_ok, last_error, last_error_msg, latency_ms_avg, latency_ms_last, rate_limited}}
"""
import os, json, time, re, datetime as dt

MAX_JOBS = 60
_SECRET = re.compile(r"(x-access-token:)[^@\s]+|(Bearer\s+)[A-Za-z0-9._\-]+|(api[_-]?key=)[^&\s]+", re.I)


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def scrub(msg):
    """Remove anything that looks like a credential before it is written anywhere."""
    s = str(msg or "")[:300]
    s = _SECRET.sub(lambda m: (m.group(1) or m.group(2) or m.group(3) or "") + "***", s)
    for k, v in os.environ.items():
        if v and len(v) >= 8 and re.search(r"KEY|SECRET|TOKEN|PASSWORD|TOTP", k):
            s = s.replace(v, "***")
    return s


class Telemetry:
    def __init__(self, site_path):
        self.path = os.path.join(site_path, "api", "pipeline.json")
        try:
            self.state = json.load(open(self.path))
        except Exception:  # noqa
            self.state = {}
        self.state.setdefault("jobs", [])
        self.state.setdefault("last_ok", {})
        self.state.setdefault("providers", {})

    # ---- provider health
    def call(self, provider, ok, latency_s=None, error=None, rate_limited=False):
        p = self.state["providers"].setdefault(provider, {"requests": 0, "errors": 0})
        p["requests"] += 1
        if latency_s is not None:
            ms = round(latency_s * 1000)
            p["latency_ms_last"] = ms
            p["latency_ms_avg"] = round((p.get("latency_ms_avg") or ms) * 0.8 + ms * 0.2)
        if ok:
            p["last_ok"] = _now()
        else:
            p["errors"] += 1
            p["last_error"] = _now()
            p["last_error_msg"] = scrub(error)
        if rate_limited:
            p["rate_limited_at"] = _now()

    def provider_status(self, provider, **fields):
        self.state["providers"].setdefault(provider, {"requests": 0, "errors": 0}).update(fields)

    # ---- jobs
    active = None

    def job(self, name, provider="yahoo"):
        return _Job(self, name, provider)

    def save(self):
        self.state["generated_at"] = _now()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        json.dump(self.state, open(self.path, "w"), indent=1, default=str)


class _Job:
    def __init__(self, tel, name, provider):
        self.tel, self.name, self.provider = tel, name, provider
        self.processed = 0
        self.failed = 0
        self.note = ""

    def __enter__(self):
        self.tel.active = self
        self.t0 = time.time()
        self.started = _now()
        return self

    def __exit__(self, et, ev, tb):
        status = "FAILED" if et else ("PARTIAL" if self.failed and self.failed >= max(1, self.processed) * 0.2 else "OK")
        rec = {"job": self.name, "started": self.started, "finished": _now(), "seconds": round(time.time() - self.t0, 1),
               "status": status, "processed": self.processed, "failed": self.failed, "provider": self.provider,
               "host": "github-actions" if os.environ.get("GITHUB_ACTIONS") else "local", "note": self.note}
        if et:
            rec["error"] = scrub(f"{et.__name__}: {ev}")
        jobs = self.tel.state["jobs"]
        jobs.append(rec)
        del jobs[:-MAX_JOBS]
        if status != "FAILED":
            self.tel.state["last_ok"][self.name] = rec["finished"]
        self.tel.active = None
        try:
            self.tel.save()
        except Exception:  # noqa
            pass
        return False   # never swallow the exception


_CACHE = {}


def get(site_path):
    """One Telemetry object per site folder, shared by the job wrapper and the job itself (no lost updates)."""
    k = os.path.abspath(site_path)
    if k not in _CACHE:
        _CACHE[k] = Telemetry(site_path)
    return _CACHE[k]
