"""Mistruth at first sight: which checkable claims will fail verification, judged from what is visible when the
claim first appears — before anyone has checked it.

Label: a verified canonical claim is 1 when its verdict is unsupported or misleading, 0 when supported
(unverifiable claims are left out). Features are only things known at first sight: the claim's wording
(numbers, quotes, attribution, absolutes, length, entities), its stance, who posted it first (followers,
whether an official, the platform), whether it followed a calendar event, and two out-of-time priors — the
first poster's and the lane's share of false claims among claims verified *before* this one appeared, so the
model never sees its own answer.

Validation is time-ordered: train on the earliest 70% of verified claims, test on the latest 30%. The report
carries AUC, Brier score, the precision of the top-scored fifth, and a calibration table, and it says plainly
when the model is not beating the base rate. Two learners are tried (logistic regression, gradient boosting)
and the one with the better held-out AUC is kept; then it is refit on everything and scores every unverified
checkable claim in every tracker. Those scores drive which claims get verified first (tracker/verify.py), a
"verify first" mark on claims pages, and a hub list — they are a queue for checking, not a verdict.

mistruth() -> out/mistruth/model.json (report), out/<slug>/mistruth.json ({cluster_id: score}),
              out/mistruth/queue.json (top unverified claims across trackers)
"""
from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import OUT_DIR

MIN_LABELS = 150
SMOOTH = 5.0              # pseudo-counts for the out-of-time priors
TOP_SHARE = 0.2           # "verify first" = the top fifth by score
FEATURES = ["len_words", "has_number", "has_percent", "has_money", "has_year", "has_quote", "attribution", "absolute", "negation", "n_entities",
            "stance_against", "stance_for", "log_first_views", "log_followers", "first_official", "src_bsky", "src_yt", "src_truth",
            "event_follow", "poster_prior", "poster_n", "lane_prior", "lane_n", "tracker_prior", "hour_utc_night"]
ABSOLUTES = re.compile(r"\b(never|always|every|all|none|no one|nobody|first ever|only|largest|biggest|worst|best|record|unprecedented|zero)\b", re.I)
ATTRIB = re.compile(r"\b(said|says|claimed|claims|admitted|admits|announced|confirmed|reported|according to|told|wrote|posted)\b", re.I)
NEGATION = re.compile(r"\b(not|no|never|n't|denied|denies|refused|without)\b", re.I)


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except ValueError:
        return None


def _trackers() -> list[str]:
    return sorted(d.name for d in OUT_DIR.iterdir() if d.is_dir() and (d / "claims.json").exists()
                  and not (_load(d / "data.json") or {}).get("meta", {}).get("synthetic"))


def text_features(text: str, entities: list[str]) -> dict[str, float]:
    t = text or ""
    return {
        "len_words": float(len(t.split())), "has_number": float(bool(re.search(r"\d", t))), "has_percent": float("%" in t or "percent" in t.lower()),
        "has_money": float(bool(re.search(r"\$\s?\d|\b(million|billion)\b", t, re.I))), "has_year": float(bool(re.search(r"\b(19|20)\d{2}\b", t))),
        "has_quote": float(("“" in t) or ('"' in t) or ("'" in t and re.search(r"'[^']{8,}'", t) is not None)),
        "attribution": float(bool(ATTRIB.search(t))), "absolute": float(bool(ABSOLUTES.search(t))), "negation": float(bool(NEGATION.search(t))),
        "n_entities": float(len(entities or [])),
    }


def rows(trackers: list[str] | None = None) -> tuple[list[dict[str, Any]], dict[str, float]]:
    """Every checkable fact claim across trackers with features; labelled ones carry y. Priors are computed in
    time order from labelled claims that appeared earlier, so no row sees its own verdict."""
    from .officials import is_official
    trackers = trackers or _trackers()
    all_claims = []
    for slug in trackers:
        cj = _load(OUT_DIR / slug / "claims.json") or {}
        for c in cj.get("clusters", []):
            if c.get("type") != "fact" or not c.get("checkable", True) or not c.get("first_seen"):
                continue
            all_claims.append((slug, c))
    all_claims.sort(key=lambda sc: sc[1]["first_seen"])
    poster_hist: dict[str, list[int]] = defaultdict(list)
    lane_hist: dict[str, list[int]] = defaultdict(list)
    tracker_hist: dict[str, list[int]] = defaultdict(list)
    global_hist: list[int] = []
    out = []
    for slug, c in all_claims:
        verdict = c.get("verdict")
        y = None if verdict in (None, "", "unverifiable") else int(verdict in ("unsupported", "misleading"))
        base = (sum(global_hist) + SMOOTH * 0.4) / (len(global_hist) + SMOOTH)
        def prior(hist: list[int]) -> float:
            return (sum(hist) + SMOOTH * base) / (len(hist) + SMOOTH)
        lane_key = f"{slug}:{(c.get('lanes') or [{}])[0].get('key', '')}"
        handle = c.get("first_handle") or ""
        try:
            hour = datetime.fromisoformat(c["first_seen"]).hour
        except ValueError:
            hour = 12
        f = {**text_features(c.get("text", ""), c.get("entities") or []),
             "stance_against": float(c.get("stance") == "against"), "stance_for": float(c.get("stance") == "for"),
             "log_first_views": math.log10(1 + float(c.get("first_views") or 0)),
             "log_followers": math.log10(1 + float(c.get("first_followers") or 0)),
             "first_official": float(bool(is_official(handle))),
             "src_bsky": float(c.get("first_source") == "bsky"), "src_yt": float(c.get("first_source") == "yt"), "src_truth": float(c.get("first_source") == "truth"),
             "event_follow": float(bool(c.get("event"))),
             "poster_prior": prior(poster_hist[handle]), "poster_n": float(len(poster_hist[handle])),
             "lane_prior": prior(lane_hist[lane_key]), "lane_n": float(len(lane_hist[lane_key])),
             "tracker_prior": prior(tracker_hist[slug]), "hour_utc_night": float(hour < 12)}
        out.append({"slug": slug, "id": c["id"], "text": c.get("text", ""), "first_seen": c["first_seen"], "views": c.get("views", 0),
                    "first_handle": handle, "y": y, "x": [f[k] for k in FEATURES], "verdict": verdict})
        if y is not None:                                   # becomes history for everything after it
            poster_hist[handle].append(y); lane_hist[lane_key].append(y); tracker_hist[slug].append(y); global_hist.append(y)
    base_rate = (sum(global_hist) / len(global_hist)) if global_hist else 0.0
    return out, {"base_rate": round(base_rate, 3), "n_labelled": len(global_hist)}


def _auc(y: list[int], p: list[float]) -> float | None:
    pos = [pi for yi, pi in zip(y, p) if yi == 1]
    neg = [pi for yi, pi in zip(y, p) if yi == 0]
    if not pos or not neg:
        return None
    wins = sum((1.0 if a > b else 0.5 if a == b else 0.0) for a in pos for b in neg)
    return wins / (len(pos) * len(neg))


def _calibration(y: list[int], p: list[float], bins: int = 5) -> list[dict[str, Any]]:
    order = sorted(range(len(p)), key=lambda i: p[i])
    out = []
    for b in range(bins):
        idx = order[b * len(order) // bins:(b + 1) * len(order) // bins]
        if idx:
            out.append({"bin": b + 1, "n": len(idx), "mean_score": round(sum(p[i] for i in idx) / len(idx), 3), "false_share": round(sum(y[i] for i in idx) / len(idx), 3)})
    return out


def fit_and_report(data: list[dict[str, Any]]) -> dict[str, Any]:
    import numpy as np
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    labelled = [r for r in data if r["y"] is not None]          # already in time order
    n = len(labelled)
    report: dict[str, Any] = {"n_labelled": n, "trained_at": datetime.now(timezone.utc).isoformat(), "features": FEATURES}
    if n < MIN_LABELS:
        report["status"] = f"only {n} verified claims; the model needs {MIN_LABELS}"
        return report
    split = int(n * 0.7)
    X = np.array([r["x"] for r in labelled]); y = np.array([r["y"] for r in labelled])
    Xtr, ytr, Xte, yte = X[:split], y[:split], X[split:], y[split:]
    cands = {
        "logistic": make_pipeline(StandardScaler(), LogisticRegression(C=0.5, max_iter=2000, class_weight="balanced")),
        "gbm": GradientBoostingClassifier(n_estimators=150, max_depth=2, learning_rate=0.05, subsample=0.8, random_state=7),
    }
    results = {}
    for name, m in cands.items():
        m.fit(Xtr, ytr)
        p = m.predict_proba(Xte)[:, 1].tolist()
        yl = yte.tolist()
        auc = _auc(yl, p)
        brier = sum((pi - yi) ** 2 for pi, yi in zip(p, yl)) / len(yl)
        k = max(1, int(len(p) * TOP_SHARE))
        top = sorted(range(len(p)), key=lambda i: -p[i])[:k]
        prec_top = sum(yl[i] for i in top) / k
        results[name] = {"auc": round(auc, 3) if auc is not None else None, "brier": round(brier, 3), "precision_top_fifth": round(prec_top, 3),
                         "calibration": _calibration(yl, p)}
    best = max(results, key=lambda k_: results[k_]["auc"] or 0)
    test_base = float(yte.mean())
    report.update({"split": {"train": int(split), "test": int(n - split), "test_base_rate": round(test_base, 3)}, "models": results, "chosen": best})
    # refit the winner on everything; importances for the reading
    model = cands[best]
    model.fit(X, y)
    if best == "gbm":
        imp = model.feature_importances_.tolist()
    else:
        imp = [abs(c) for c in model.named_steps["logisticregression"].coef_[0].tolist()]
    report["importance"] = sorted(({"feature": f, "weight": round(w, 3)} for f, w in zip(FEATURES, imp)), key=lambda d: -d["weight"])[:10]
    r = results[best]
    auc = r["auc"] or 0.5
    lift = (r["precision_top_fifth"] / test_base) if test_base else None
    verdict = ("The model separates well; the top fifth is worth checking first." if auc >= 0.7 else
               "The model is useful but modest; use the queue as a tiebreaker, not a judgment." if auc >= 0.6 else
               "The model is not beating chance yet; wording and who-posted-first are not enough to call a claim false before checking it.")
    report["reading"] = (f"Trained on {split} verified claims and tested on the {n - split} most recent: AUC {auc:.2f} ({best}); the top-scored fifth of the "
                         f"test set was {r['precision_top_fifth']:.0%} false against a base rate of {test_base:.0%}"
                         + (f", {lift:.1f}× better than random. " if lift else ". ") + verdict
                         + " The verified set is the claims verification chose, the biggest ones, so scores on smaller claims are extrapolation.")
    report["_model"] = model
    return report


def mistruth() -> Path:
    data, meta = rows()
    report = fit_and_report(data)
    report.update(meta)
    model = report.pop("_model", None)
    out_dir = OUT_DIR / "mistruth"
    out_dir.mkdir(parents=True, exist_ok=True)
    queue: list[dict[str, Any]] = []
    scored_by_tracker: dict[str, dict[str, float]] = defaultdict(dict)
    if model is not None:
        import numpy as np
        unl = [r for r in data if r["y"] is None and not r["verdict"]]
        if unl:
            p = model.predict_proba(np.array([r["x"] for r in unl]))[:, 1]
            for r, pi in zip(unl, p):
                scored_by_tracker[r["slug"]][str(r["id"])] = round(float(pi), 3)
                queue.append({"slug": r["slug"], "id": r["id"], "text": r["text"], "views": r["views"], "first_handle": r["first_handle"], "first_seen": r["first_seen"], "score": round(float(pi), 3)})
        for slug, scores in scored_by_tracker.items():
            (OUT_DIR / slug / "mistruth.json").write_text(json.dumps({"trained_at": report["trained_at"], "scores": scores}), encoding="utf-8")
        queue.sort(key=lambda q: (-q["score"], -q["views"]))
        cut = sorted((q["score"] for q in queue), reverse=True)
        report["verify_first_threshold"] = cut[max(0, int(len(cut) * TOP_SHARE) - 1)] if cut else None
    (out_dir / "model.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    (out_dir / "queue.json").write_text(json.dumps({"generated_at": report["trained_at"], "threshold": report.get("verify_first_threshold"),
                                                     "queue": [q for q in queue if q["views"] >= 20_000][:200]}, ensure_ascii=False), encoding="utf-8")
    print(f"mistruth: {report.get('reading') or report.get('status')}  ({len(queue)} unverified claims scored)")
    return out_dir / "model.json"


def scores_for(slug: str) -> tuple[dict[str, float], float | None]:
    """Unverified claims' scores for one tracker and the 'verify first' threshold, from the last build."""
    d = _load(OUT_DIR / slug / "mistruth.json") or {}
    m = _load(OUT_DIR / "mistruth" / "model.json") or {}
    return {str(k): float(v) for k, v in (d.get("scores") or {}).items()}, m.get("verify_first_threshold")
