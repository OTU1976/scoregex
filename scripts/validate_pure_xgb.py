"""
validate_pure_xgb.py — Compare PureXGBRegressor (pure_xgb_predict.py) contre
xgb.Booster.predict() reel, sur (a) les 706 lignes d'entrainement reelles et
(b) 2000 vecteurs de features aleatoires (uniformes dans des plages
realistes), incluant des cas limites (valeurs extremes, doublons de
splits). Exige une correspondance quasi-exacte avant tout deploiement.
"""
import csv
import json
import os
import random
import sys

import numpy as np
import xgboost as xgb

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.join(HERE, "..")
sys.path.insert(0, REPO_ROOT)

from engine.pure_xgb_predict import PureXGBRegressor  # noqa: E402

MODEL_PATH = os.path.join(REPO_ROOT, "models", "xgb_divergence_v1.json")
DATA_PATH = os.path.join(REPO_ROOT, "data", "dpe_regression_sample.csv")

DPE_CLASSES_ORDRE = ["A", "B", "C", "D", "E", "F", "G"]
COMMUNES = ["01071", "01160", "01173", "01313", "01281", "01354", "01401", "01419"]
TYPE_LOCAL = ["Appartement", "Maison"]
FEATURE_NAMES = (
    ["surface", "age_bien", "annee_mutation", "dpe_ordinal"]
    + [f"commune_{c}" for c in COMMUNES]
    + [f"type_{t}" for t in TYPE_LOCAL]
)


def load_real_rows():
    rows = []
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="|")
        for r in reader:
            rows.append(r)
    feats_list = []
    for r in rows:
        try:
            surface = float(r["surface"])
        except (ValueError, KeyError):
            continue
        dpe = (r.get("etiquette_dpe") or "").strip().upper()
        if dpe not in DPE_CLASSES_ORDRE:
            continue
        ac = r.get("annee_construction") or ""
        am = r.get("annee_mutation") or ""
        if not am or not ac:
            continue
        try:
            annee_mutation = int(am)
            age_bien = max(0, annee_mutation - int(ac))
        except ValueError:
            continue
        commune = (r.get("code_commune") or "").strip()
        type_local = (r.get("type_local") or "").strip()
        feats = [surface, age_bien, annee_mutation, DPE_CLASSES_ORDRE.index(dpe)]
        feats += [1.0 if commune == c else 0.0 for c in COMMUNES]
        feats += [1.0 if type_local == t else 0.0 for t in TYPE_LOCAL]
        feats_list.append(feats)
    return feats_list


def random_rows(n, seed=123):
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        surface = rng.uniform(10, 500)
        age_bien = rng.uniform(0, 150)
        annee_mutation = rng.uniform(2010, 2027)
        dpe_ord = rng.uniform(0, 6)
        commune_oh = [0.0] * len(COMMUNES)
        commune_oh[rng.randrange(len(COMMUNES))] = 1.0
        type_oh = [0.0] * len(TYPE_LOCAL)
        type_oh[rng.randrange(len(TYPE_LOCAL))] = 1.0
        out.append([surface, age_bien, annee_mutation, dpe_ord] + commune_oh + type_oh)
    # cas limites : zeros, valeurs negatives improbables, valeurs enormes
    out.append([0.0] * len(FEATURE_NAMES))
    out.append([1e6, 1e6, 1e6, 6.0] + [0.0] * 8 + [1.0, 0.0])
    out.append([-50.0, -10.0, 1999.0, 0.0] + [1.0] + [0.0] * 7 + [0.0, 1.0])
    return out


def main():
    bst = xgb.Booster()
    bst.load_model(MODEL_PATH)
    pure = PureXGBRegressor(MODEL_PATH)

    assert pure.feature_names == FEATURE_NAMES, "ordre des features desaligne !"
    print(f"[validate] feature_names OK ({len(FEATURE_NAMES)} features)")
    print(f"[validate] base_score charge = {pure.base_score}")
    print(f"[validate] num_trees charges = {pure.num_trees}")

    all_feats = load_real_rows() + random_rows(2000)
    print(f"[validate] {len(all_feats)} vecteurs a tester "
          f"({len(load_real_rows())} reels + {len(all_feats) - len(load_real_rows())} synthetiques/limites)")

    dmat = xgb.DMatrix(np.array(all_feats, dtype=np.float64), feature_names=FEATURE_NAMES)
    preds_real = bst.predict(dmat)

    max_abs_diff = 0.0
    max_rel_diff = 0.0
    n_mismatch = 0
    for i, feats in enumerate(all_feats):
        pred_pure = pure.predict_one(feats)
        pred_real = float(preds_real[i])
        abs_diff = abs(pred_pure - pred_real)
        rel_diff = abs_diff / max(abs(pred_real), 1e-9)
        max_abs_diff = max(max_abs_diff, abs_diff)
        max_rel_diff = max(max_rel_diff, rel_diff)
        if abs_diff > 1e-4 and rel_diff > 1e-6:
            n_mismatch += 1
            if n_mismatch <= 5:
                print(f"  MISMATCH idx={i} pure={pred_pure!r} real={pred_real!r} "
                      f"abs_diff={abs_diff!r} rel_diff={rel_diff!r} feats={feats}")

    print(f"\n[validate] max_abs_diff={max_abs_diff!r}  max_rel_diff={max_rel_diff!r}")
    print(f"[validate] n_mismatch (abs>1e-4 ET rel>1e-6) = {n_mismatch} / {len(all_feats)}")
    if n_mismatch == 0:
        print("[validate] ✅ CORRESPONDANCE EXACTE — implementation pure Python validee.")
    else:
        print("[validate] ❌ ECHEC — des divergences existent, NE PAS DEPLOYER.")


if __name__ == "__main__":
    main()
