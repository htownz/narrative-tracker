"""Growth model: which claims will be in their tracker's top five by 24-hour views, judged from the feature table
at the moment of a snapshot (tracker/features.py writes one row per claim per run; the outcome top5_24h is filled
in a day later).

The bar is the one-line heuristic in features.rank_now(), scored on the insights page since Oct 15: a model that
cannot out-pick it on held-out snapshots does not get to drive the hub. So the report carries, for the heuristic
and for each learner, the same operative number — of the five claims each one ranks highest in a snapshot, how
many reached the top five a day later (precision@5, over competitive snapshots of MIN_FIELD+ claims) — plus AUC,
Brier and a calibration table. Validation is time-ordered with a day's gap: train on snapshots before the
cutoff, test on snapshots a full 24 hours after it, so no training label is computed from the test period.

Features are the row's own numbers (views, accounts, momentum, control, burst, event-follow, news, the first
poster's registry stats, the heuristic itself) and its standing inside its snapshot (rank and share by views in
the last six hours, rank by total views, by momentum, by accounts; field size), because the outcome is a rank.

When the best learner beats the heuristic by MIN_EDGE on precision@5 across at least MIN_TEST_SNAPSHOTS test
snapshots, the report says use_model and features.py ranks "likely to grow" by the model's probability instead;
otherwise the heuristic stays. Either way picks are logged with their method, so the scorecard shows both live.

growth() -> out/growth/model.json (report), out/growth/model.pkl (the fitted winner, for features.py);
rank_with_model(rows) -> (ranked rows, method) for features.py.
"""
from __future__ import annotations

import csv
import json
import math
import pickle
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import DATA_DIR, OUT_DIR
from .scorecard import MIN_FIELD

MIN_LABELLED = 2000
MIN_TEST_SNAPSHOTS = 20
MIN_EDGE = 0.10               # the model must beat the heuristic's precision@5 by this much (absolute) to drive the hub
TOP_K = 5
RAW = ["age_h", "posts", "views", "accounts", "copies", "first_views", "first_followers", "views_1h", "views_6h", "views_24h", "momentum_6h",
       "control", "new_accounts", "burst", "event_follow", "news_sources_48h", "news_before_x", "first_reliability", "first_early_lift"]
FEATURES = RAW + ["heuristic", "log_views", "log_views_6h", "log_views_24h", "log_accounts", "views_per_account", "share_6h", "share_24h",
                  "rank_views_6h", "pct_views_6h", "rank_views", "rank_momentum", "rank_accounts", "field_n", "share_of_field_6h", "hour_utc", "dow"]


def _f(v: Any) -> float:
    try:
        return float(v) if v not in ("", None) else float("nan")
    except (TypeError, ValueError):
        return float("nan")


def _dir() -> Path:
    d = OUT_DIR / "growth"
    d.mkdir(parents=True, exist_ok=True)
    return d


def feature_rows(paths: list[Path] | None = None) -> list[dict[str, Any]]:
    """Every tracker's claim rows from data/features/*.csv (picks.csv is the scorecard's log, not a feature table)."""
    paths = paths if paths is not None else sorted((DATA_DIR / "features").glob("*.csv"))
    out = []
    for p in paths:
        if p.name == "picks.csv":
            continue
        with p.open(encoding="utf-8", newline="") as f:
            out += [r for r in csv.DictReader(f) if r.get("kind") == "claim"]
    return out


def featurize(snapshot: list[dict[str, Any]]) -> list[list[float]]:
    """Feature vectors for every claim row of one snapshot (same slug and asof), in the rows' order."""
    from .features import rank_now
    heur = {r["key"]: r["heuristic"] for r in rank_now([_num(r) for r in snapshot])}
    n = len(snapshot)
    def ranks(col: str, default: float = 0.0) -> dict[str, int]:
        order = sorted(snapshot, key=lambda r: -(_f(r[col]) if not math.isnan(_f(r[col])) else default))
        return {r["key"]: i + 1 for i, r in enumerate(order)}
    r6, rv, rm, ra = ranks("views_6h"), ranks("views"), ranks("momentum_6h", 1.0), ranks("accounts")
    field_6h = sum(max(0.0, _f(r["views_6h"]) if not math.isnan(_f(r["views_6h"])) else 0.0) for r in snapshot) or 1.0
    out = []
    for r in snapshot:
        try:
            t = datetime.fromisoformat(r["asof"])
            hour, dow = float(t.hour), float(t.weekday())
        except ValueError:
            hour, dow = 12.0, 3.0
        views, v6, v24, acc = _f(r["views"]), _f(r["views_6h"]), _f(r["views_24h"]), _f(r["accounts"])
        d = {k: _f(r.get(k)) for k in RAW}
        d.update({
            "heuristic": float(heur.get(r["key"], 0.0)), "log_views": math.log10(1 + max(0.0, views)), "log_views_6h": math.log10(1 + max(0.0, v6)),
            "log_views_24h": math.log10(1 + max(0.0, v24)), "log_accounts": math.log10(1 + max(0.0, acc)),
            "views_per_account": views / acc if acc else 0.0, "share_6h": v6 / views if views else 0.0, "share_24h": v24 / views if views else 0.0,
            "rank_views_6h": float(r6[r["key"]]), "pct_views_6h": r6[r["key"]] / n, "rank_views": float(rv[r["key"]]), "rank_momentum": float(rm[r["key"]]),
            "rank_accounts": float(ra[r["key"]]), "field_n": float(n), "share_of_field_6h": (v6 if not math.isnan(v6) else 0.0) / field_6h,
            "hour_utc": hour, "dow": dow,
        })
        out.append([d[k] for k in FEATURES])
    return out


def _num(r: dict[str, Any]) -> dict[str, Any]:
    from .scorecard import _num as num
    return num(r)


def dataset(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Labelled claim rows on competitive snapshots, featurized, in time order."""
    by_snap: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_snap[(r["slug"], r["asof"])].append(r)
    out = []
    for (slug, asof), snap in sorted(by_snap.items(), key=lambda kv: kv[0][1]):
        if len(snap) < MIN_FIELD or not any(r["gain_24h"] not in ("", None) for r in snap):
            continue
        X = featurize(snap)
        for r, x in zip(snap, X):
            if r["gain_24h"] in ("", None):
                continue
            out.append({"slug": slug, "asof": asof, "key": r["key"], "label": r["label"], "x": x, "y": int(str(r["top5_24h"]) == "1"),
                        "heuristic": x[FEATURES.index("heuristic")]})
    return out


def _auc(y: list[int], p: list[float]) -> float | None:
    pos = [pi for yi, pi in zip(y, p) if yi == 1]
    neg = [pi for yi, pi in zip(y, p) if yi == 0]
    if not pos or not neg:
        return None
    if len(pos) * len(neg) > 4_000_000:                       # rank-based for big sets
        order = sorted(range(len(p)), key=lambda i: p[i])
        rank_sum, i = 0.0, 0
        while i < len(order):                                 # average ranks over ties
            j = i
            while j + 1 < len(order) and p[order[j + 1]] == p[order[i]]:
                j += 1
            avg = (i + j + 2) / 2
            rank_sum += sum(avg for k in range(i, j + 1) if y[order[k]] == 1)
            i = j + 1
        return (rank_sum - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))
    wins = sum((1.0 if a > b else 0.5 if a == b else 0.0) for a in pos for b in neg)
    return wins / (len(pos) * len(neg))


def precision_at_k(data: list[dict[str, Any]], scores: list[float], k: int = TOP_K) -> dict[str, Any]:
    """Of each snapshot's k highest-scored claims, the share that reached the top five; averaged over picks.
    Also counts breakouts: picks that hit while not already in their snapshot's top five by total views."""
    rv = FEATURES.index("rank_views")
    by_snap: dict[tuple[str, str], list[tuple[float, int, bool]]] = defaultdict(list)
    for d, s in zip(data, scores):
        by_snap[(d["slug"], d["asof"])].append((s, d["y"], d["x"][rv] > TOP_K))
    hits = n = new_picks = new_hits = 0
    for picks in by_snap.values():
        top = sorted(picks, key=lambda t: -t[0])[:k]
        hits += sum(y for _, y, _ in top); n += len(top)
        new_picks += sum(1 for _, _, new in top if new); new_hits += sum(y for _, y, new in top if new)
    return {"n": n, "hits": hits, "precision": round(hits / n, 3) if n else None, "snapshots": len(by_snap), "breakout_picks": new_picks, "breakout_hits": new_hits}


def _calibration(y: list[int], p: list[float], bins: int = 5) -> list[dict[str, Any]]:
    order = sorted(range(len(p)), key=lambda i: p[i])
    out = []
    for b in range(bins):
        idx = order[b * len(order) // bins:(b + 1) * len(order) // bins]
        if idx:
            out.append({"bin": b + 1, "n": len(idx), "mean_score": round(sum(p[i] for i in idx) / len(idx), 3), "top5_share": round(sum(y[i] for i in idx) / len(idx), 3)})
    return out


def fit_and_report(data: list[dict[str, Any]]) -> dict[str, Any]:
    import numpy as np
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    n = len(data)
    report: dict[str, Any] = {"n_labelled": n, "trained_at": datetime.now(timezone.utc).isoformat(), "features": FEATURES, "use_model": False}
    if n < MIN_LABELLED:
        report["status"] = f"only {n} labelled claim-days on competitive snapshots; the model needs {MIN_LABELLED}"
        return report
    times = sorted({d["asof"] for d in data})
    # the newest quarter of claim-days is the test set; training stops a full day before it starts, so no training
    # label (a 24h outcome) is computed from the test period
    test_start = datetime.fromisoformat(data[int(n * 0.75)]["asof"])
    cutoff = test_start - timedelta(hours=24)
    train = [d for d in data if datetime.fromisoformat(d["asof"]) < cutoff]
    test = [d for d in data if datetime.fromisoformat(d["asof"]) >= test_start]
    if len(train) < 500 or len(test) < 200 or len({(d["slug"], d["asof"]) for d in test}) < 5:
        report["status"] = (f"the time-ordered split with a day's gap leaves {len(train)} claim-days to train on and {len(test)} to test on "
                            f"({times[0][:10]} to {times[-1][:10]}); more days needed")
        return report
    Xtr, ytr = np.array([d["x"] for d in train]), np.array([d["y"] for d in train])
    Xte, yte = np.array([d["x"] for d in test]), [d["y"] for d in test]
    # missing values (no registry stat, no press check yet) are filled: the median for the logistic, -1 for the trees
    # (a value the trees can split on); keep_empty_features so an all-missing column cannot break a fit
    cands = {
        "logistic": make_pipeline(SimpleImputer(strategy="median", keep_empty_features=True), StandardScaler(), LogisticRegression(C=0.3, max_iter=3000, class_weight="balanced")),
        "gbm": make_pipeline(SimpleImputer(strategy="constant", fill_value=-1.0, keep_empty_features=True),
                             HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=300, l2_regularization=1.0, min_samples_leaf=40, random_state=7)),
    }
    base = sum(yte) / len(yte)
    def block(p: list[float]) -> dict[str, Any]:
        auc = _auc(yte, p)
        p5, p3 = precision_at_k(test, p), precision_at_k(test, p, 3)
        return {"auc": round(auc, 3) if auc is not None else None, "p_at_5": p5["precision"], "p_at_3": p3["precision"], "picks": p5["n"], "hits": p5["hits"],
                "breakout_picks": p5["breakout_picks"], "breakout_hits": p5["breakout_hits"]}
    results: dict[str, Any] = {"heuristic": {**block([d["heuristic"] for d in test]), "brier": None}}
    for name, m in cands.items():
        m.fit(Xtr, ytr)
        p = m.predict_proba(Xte)[:, 1].tolist()
        results[name] = {**block(p), "brier": round(sum((pi - yi) ** 2 for pi, yi in zip(p, yte)) / len(yte), 4), "calibration": _calibration(yte, p)}
    best = max(cands, key=lambda k: ((results[k]["p_at_5"] or 0), (results[k]["auc"] or 0)))
    test_snaps = len({(d["slug"], d["asof"]) for d in test})
    report.update({"split": {"train": len(train), "test": len(test), "test_snapshots": test_snaps, "cutoff": cutoff.isoformat(), "test_base_rate": round(base, 3),
                             "first_asof": times[0], "last_asof": times[-1]},
                   "models": results, "chosen": best})
    edge = (results[best]["p_at_5"] or 0) - (results["heuristic"]["p_at_5"] or 0)
    report["use_model"] = bool(test_snaps >= MIN_TEST_SNAPSHOTS and edge >= MIN_EDGE)
    model = cands[best]
    model.fit(np.array([d["x"] for d in data]), np.array([d["y"] for d in data]))
    # feature importance: coefficients for the logistic, permutation importance on the test set for the GBM
    try:
        if best == "logistic":
            imp = [abs(c) for c in model.named_steps["logisticregression"].coef_[0].tolist()]
        else:
            from sklearn.inspection import permutation_importance
            cands["gbm"].fit(Xtr, ytr)
            imp = permutation_importance(cands["gbm"], Xte, np.array(yte), scoring="roc_auc", n_repeats=3, random_state=7).importances_mean.tolist()
    except Exception:
        imp = [0.0] * len(FEATURES)
    report["importance"] = sorted(({"feature": f, "weight": round(w, 4)} for f, w in zip(FEATURES, imp)), key=lambda d: -d["weight"])[:10]
    r, h = results[best], results["heuristic"]
    auc_s = lambda v: f"{v:.2f}" if v is not None else "n/a"  # noqa: E731
    reading = (f"Trained on {len(train):,} claim-days ({len({(d['slug'], d['asof']) for d in train})} snapshots) and tested on {len(test):,} claim-days across {test_snaps} snapshots a day or more later: "
               f"the {best} picks' top five hit {r['p_at_5'] or 0:.0%} ({r['hits']} of {r['picks']}) against the heuristic's {h['p_at_5'] or 0:.0%} ({h['hits']} of {h['picks']}) and a base rate of {base:.1%}; "
               f"AUC {auc_s(r['auc'])} vs the heuristic's {auc_s(h['auc'])}. Of the model's hits, {r['breakout_hits']} were claims not already in the top five by views when picked (the heuristic: {h['breakout_hits']}). ")
    if report["use_model"]:
        reading += "The model out-picks the heuristic by enough to drive the hub's Gaining now list; the scorecard tracks both from here."
    elif edge >= MIN_EDGE:
        reading += f"That edge is enough, but on too few snapshots ({test_snaps} of the {MIN_TEST_SNAPSHOTS} the switch needs); the heuristic keeps the hub and the model's picks are logged in shadow until then."
    elif edge > 0:
        reading += f"Better than the heuristic, but not by the {MIN_EDGE:.0%} the switch requires; the heuristic keeps the hub and the model's picks are logged in shadow."
    else:
        reading += "No better than the one-line heuristic on held-out snapshots: the extra features are not adding information yet, so the heuristic keeps the hub."
    report["reading"] = reading
    report["_model"] = model
    return report


def growth() -> Path:
    data = dataset(feature_rows())
    report = fit_and_report(data)
    model = report.pop("_model", None)
    d = _dir()
    if model is not None:
        with (d / "model.pkl").open("wb") as f:
            pickle.dump({"features": FEATURES, "model": model, "trained_at": report["trained_at"], "use_model": report["use_model"]}, f)
    elif (d / "model.pkl").exists():
        (d / "model.pkl").unlink()
    (d / "model.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    print(f"growth: {report.get('reading') or report.get('status')}")
    return d / "model.json"


def load_model() -> dict[str, Any] | None:
    p = OUT_DIR / "growth" / "model.pkl"
    if not p.exists():
        return None
    try:
        with p.open("rb") as f:
            m = pickle.load(f)
        return m if m.get("features") == FEATURES else None
    except Exception:
        return None


def rank_with_model(rows: list[dict[str, Any]], model: dict[str, Any] | None = None) -> dict[str, Any]:
    """features.py's rankings for one snapshot: {"heuristic": rows by rank_now, "model": rows by the model's
    probability (None without a model on disk), "method": which one drives the hub}. Rows carry `heuristic`;
    model-ranked rows also carry `p_grow`. The model ranks only what the heuristic would consider (2+ accounts,
    views in the last 6h), so the two are judged on the same field."""
    from .features import rank_now
    heuristic = rank_now(rows)
    model = model if model is not None else load_model()
    out: dict[str, Any] = {"heuristic": heuristic, "model": None, "method": "heuristic"}
    if not model or not heuristic:
        return out
    import numpy as np
    claims = [r for r in rows if r["kind"] == "claim"]
    X = featurize([{**r, "asof": r.get("asof") or ""} for r in claims])
    p = model["model"].predict_proba(np.array(X))[:, 1]
    heur = {r["key"]: r["heuristic"] for r in heuristic}
    ranked = [{**r, "heuristic": heur.get(r["key"], 0.0), "p_grow": round(float(pi), 3)} for r, pi in zip(claims, p) if r["key"] in heur]
    ranked.sort(key=lambda r: (-r["p_grow"], -r["heuristic"]))
    out["model"] = ranked
    out["method"] = "model" if model.get("use_model") else "heuristic"
    return out
