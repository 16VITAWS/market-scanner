import hashlib, json


class Strategy:
    id = "base"
    name = "Base"
    version = "0.0.0"
    status = "experimental"        # experimental | backtested | paper-approved | disabled
    universe = "NIFTY 500"
    timeframe = "1d"
    segment = "delivery"
    hypothesis = ""
    rules = {}                     # human-readable rule text, shown in the portal
    params = {}
    regime_filter = True
    min_capital = 200000
    holding = "5-20 sessions"
    failure_modes = []
    warmup = 200

    def spec(self):
        p = json.dumps(self.params, sort_keys=True)
        return {"id": self.id, "name": self.name, "version": self.version, "status": self.status, "universe": self.universe,
                "timeframe": self.timeframe, "segment": self.segment, "hypothesis": self.hypothesis, "rules": self.rules,
                "params": self.params, "regime_filter": self.regime_filter, "min_capital": self.min_capital,
                "holding": self.holding, "failure_modes": self.failure_modes, "warmup_bars": self.warmup,
                "param_hash": hashlib.sha1(p.encode()).hexdigest()[:10], "code": True}

    def signal(self, d, ctx):
        raise NotImplementedError
