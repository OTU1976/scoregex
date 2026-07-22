"""
scripts/train_xgboost_divergence.py — Entraine le modele XGBoost "signal
secondaire de divergence" pour ScoreGex.

CONTEXTE (feu vert Helen, 22/07/2026) : "XGBoost en signal secondaire :
confirmer si j'implemente comme propose (flag de divergence, pas
remplacement)" -> OUI. Ce script n'entraine PAS un remplacant du moteur
hedonique (compute_avm_hedonique) : il entraine un second modele
INDEPENDANT (gradient boosting, non-parametrique) sur les MEMES donnees
reelles (DVF Pays de Gex x ADEME Observatoire DPE, n=808, le meme jeu de
donnees deja valide pour la regression multivariee DPE de la tache #13),
pour servir de "challenger" au sens SR 11-7 (Federal Reserve / OCC Model
Risk Management Guidance) : un modele independant utilise pour detecter
les cas ou le modele de production (hedonique) et un modele alternatif
divergent fortement -> flag de revue humaine, jamais un remplacement
automatique du prix affiche.

Donnees : dpe_regression_sample.csv (colonnes : code_commune|surface|
type_local|etiquette_dpe|annee_construction|annee_mutation|prix_m2).
Meme fichier que la regression age-controlee de la tache #13.

Sortie : models/xgb_divergence_v1.json (format natif XGBoost, portable,
pas de dependance pickle/version Python), + models/xgb_divergence_v1.meta.json
(schema des features, ordre des colonnes, seuil de divergence calibre sur
la distribution reelle des residus CV -- PAS une valeur arbitraire).

Honnetete methodologique (a reporter telle quelle a Helen, bonne ou
mauvaise nouvelle) : n=808 est un petit echantillon pour du gradient
boosting. Hyperparametres volontairement conservateurs (max_depth<=3,
n_estimators limite via early stopping CV, forte regularisation L2) pour
minimiser l'overfitting sur un jeu de cette taille -- pratique standard
(cf. Hastie/Tibshirani/Friedman, "Elements of Statistical Learning",
ch.10 Boosting -- shrinkage + limiter la profondeur des arbres sur petits
echantillons).
"""
import csv
import json
import math
import os
import sys

import numpy as np
import xgboost as xgb

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.join(HERE, "..")
DATA_PATH = os.path.join(REPO_ROOT, "data", "dpe_regression_sample.csv")
MODEL_PATH = os.path.join(REPO_ROOT, "models", "xgb_divergence_v1.json")
META_PATH = os.path.join(REPO_ROOT, "models", "xgb_divergence_v1.meta.json")

DPE_CLASSES_ORDRE = ["A", "B", "C", "D", "E", "F", "G"]
COMMUNES = [
    "01071", "01160", "01173", "01313", "01281", "01354", "01401", "01419",
]
TYPE_LOCAL = ["Appartement", "Maison"]

FEATURE_NAMES = (
    ["surface", "age_bien", "annee_mutation", "dpe_ordinal"]
    + [f"commune_{c}" for c in COMMUNES]
    + [f"type_{t}" for t in TYPE_LOCAL]
)


def load_rows(path):
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="|")
        for r in reader:
            rows.append(r)
    return rows


def row_to_features(r, annee_mutation_defaut):
    try:
        surface = float(r["surface"])
    except (ValueError, KeyError):
        return None
    dpe = (r.get("etiquette_dpe") or "").strip().upper()
    if dpe not in DPE_CLASSES_ORDRE:
        return None
    ac = r.get("annee_construction") or ""
    am = r.get("annee_mutation") or ""
    try:
        annee_mutation = int(am) if am else annee_mutation_defaut
    except ValueError:
        annee_mutation = annee_mutation_defaut
    if ac:
        try:
            age_bien = max(0, annee_mutation - int(ac))
        except ValueError:
            age_bien = None
    else:
        age_bien = None
    if age_bien is None:
        return None  # l'age est une variable de controle centrale (tache #13) : on exclut les lignes sans annee_construction plutot que d'imputer une valeur arbitraire
    commune = (r.get("code_commune") or "").strip()
    type_local = (r.get("type_local") or "").strip()
    try:
        prix_m2 = float(r["prix_m2"])
    except (ValueError, KeyError):
        return None
    if prix_m2 <= 0 or surface <= 0:
        return None

    feats = [surface, age_bien, annee_mutation, DPE_CLASSES_ORDRE.index(dpe)]
    feats += [1.0 if commune == c else 0.0 for c in COMMUNES]
    feats += [1.0 if type_local == t else 0.0 for t in TYPE_LOCAL]
    return feats, prix_m2


def build_dataset(rows):
    X, y = [], []
    annees = [int(r["annee_mutation"]) for r in rows if (r.get("annee_mutation") or "").isdigit()]
    annee_defaut = int(round(sum(annees) / len(annees))) if annees else 2024
    n_rejetees = 0
    for r in rows:
        out = row_to_features(r, annee_defaut)
        if out is None:
            n_rejetees += 1
            continue
        feats, prix_m2 = out
        X.append(feats)
        y.append(prix_m2)
    return np.array(X, dtype=np.float64), np.array(y, dtype=np.float64), n_rejetees


def kfold_indices(n, k, seed=42):
    rng = np.random.default_rng(seed)
    idx = rng.permutation(n)
    folds = np.array_split(idx, k)
    for i in range(k):
        test_idx = folds[i]
        train_idx = np.concatenate([folds[j] for j in range(k) if j != i])
        yield train_idx, test_idx


def main():
    rows = load_rows(DATA_PATH)
    X, y, n_rejetees = build_dataset(rows)
    n = len(y)
    print(f"[train_xgboost_divergence] lignes brutes={len(rows)} exploitables={n} rejetees={n_rejetees}")
    if n < 100:
        print("ERREUR : echantillon trop petit pour un entrainement fiable (n<100). Abandon.")
        sys.exit(1)

    params = {
        "objective": "reg:squarederror",
        "max_depth": 3,
        "eta": 0.05,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "reg_lambda": 5.0,
        "reg_alpha": 1.0,
        "min_child_weight": 5,
        "seed": 42,
    }

    # ── Cross-validation 5-fold HONNETE (residus hors-echantillon reels,
    # pas les residus d'entrainement) — sert (a) a rapporter un R²/RMSE
    # honnete a Helen et (b) a calibrer le seuil de divergence sur la
    # vraie dispersion des erreurs out-of-sample, pas un chiffre invente.
    k = 5
    cv_preds = np.zeros(n)
    cv_folds = list(kfold_indices(n, k))
    for train_idx, test_idx in cv_folds:
        dtrain = xgb.DMatrix(X[train_idx], label=y[train_idx], feature_names=FEATURE_NAMES)
        dtest = xgb.DMatrix(X[test_idx], feature_names=FEATURE_NAMES)
        bst = xgb.train(
            params, dtrain,
            num_boost_round=500,
            evals=[(dtrain, "train")],
            early_stopping_rounds=20,
            verbose_eval=False,
        )
        cv_preds[test_idx] = bst.predict(dtest, iteration_range=(0, bst.best_iteration + 1))

    residus = y - cv_preds
    ss_res = float(np.sum(residus ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2_cv = 1 - ss_res / ss_tot
    rmse_cv = float(np.sqrt(np.mean(residus ** 2)))
    mape_cv = float(np.mean(np.abs(residus / y)) * 100)
    residus_pct = residus / y  # divergence relative out-of-sample, la vraie distribution empirique

    std_residus_pct = float(np.std(residus_pct))
    mean_residus_pct = float(np.mean(residus_pct))

    print(f"[CV 5-fold, out-of-sample REEL] R2={r2_cv:.4f}  RMSE={rmse_cv:.1f} EUR/m2  MAPE={mape_cv:.2f}%")
    print(f"[CV] biais moyen residu%={mean_residus_pct*100:.2f}%  ecart-type residu%={std_residus_pct*100:.2f}%")

    # ── Modele final : entraine sur 100% des donnees (n=808) avec le
    # meme nombre d'arbres moyen que les folds CV (evite l'overfitting
    # d'un early-stopping sans hold-out sur le modele de production).
    best_iters = []
    for train_idx, test_idx in kfold_indices(n, k):
        dtrain = xgb.DMatrix(X[train_idx], label=y[train_idx], feature_names=FEATURE_NAMES)
        dtest = xgb.DMatrix(X[test_idx], label=y[test_idx], feature_names=FEATURE_NAMES)
        bst = xgb.train(
            params, dtrain,
            num_boost_round=500,
            evals=[(dtest, "valid")],
            early_stopping_rounds=20,
            verbose_eval=False,
        )
        best_iters.append(bst.best_iteration + 1)
    num_round_final = int(round(sum(best_iters) / len(best_iters)))
    print(f"[final] num_boost_round retenu (moyenne des 5 folds) = {num_round_final}")

    dall = xgb.DMatrix(X, label=y, feature_names=FEATURE_NAMES)
    bst_final = xgb.train(params, dall, num_boost_round=num_round_final, verbose_eval=False)
    bst_final.save_model(MODEL_PATH)

    # ── Seuil de divergence calibre : 2 ecarts-types de la distribution
    # REELLE des residus relatifs out-of-sample (loi normale => ~95.4%
    # des estimations hedonique/XGBoost sur des biens similaires a
    # l'echantillon d'entrainement ne devraient PAS declencher le flag ;
    # au-dela, la divergence est statistiquement anormale et merite une
    # revue humaine). PAS un pourcentage arbitraire choisi a la main.
    seuil_divergence_pct = round(abs(mean_residus_pct) + 2 * std_residus_pct, 4)

    meta = {
        "version": "xgb_divergence_v1",
        "date_entrainement": "2026-07-22",
        "n_transactions_entrainement": n,
        "n_transactions_rejetees": n_rejetees,
        "source_donnees": "dpe_regression_sample.csv (DVF Pays de Gex 2014-2025 x ADEME Observatoire DPE, meme jeu que regression age-controlee tache #13)",
        "feature_names_ordre": FEATURE_NAMES,
        "dpe_ordinal_mapping": {c: i for i, c in enumerate(DPE_CLASSES_ORDRE)},
        "communes_ordre": COMMUNES,
        "type_local_ordre": TYPE_LOCAL,
        "hyperparametres": params,
        "num_boost_round": num_round_final,
        "cv_5fold_out_of_sample": {
            "r2": round(r2_cv, 4),
            "rmse_eur_m2": round(rmse_cv, 1),
            "mape_pct": round(mape_cv, 2),
            "biais_moyen_residu_pct": round(mean_residus_pct * 100, 2),
            "ecart_type_residu_pct": round(std_residus_pct * 100, 2),
        },
        "seuil_divergence_pct": seuil_divergence_pct,
        "seuil_divergence_methodologie": "abs(biais_moyen_residu) + 2*ecart_type_residu (CV 5-fold out-of-sample, ~95.4% des cas normaux sous ce seuil si residus ~ normaux)",
        "usage": "SIGNAL SECONDAIRE UNIQUEMENT (flag de divergence, pas remplacement) — voir engine/xgboost_divergence.py",
    }
    with open(META_PATH, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print(f"\nModele sauvegarde : {MODEL_PATH}")
    print(f"Metadonnees sauvegardees : {META_PATH}")
    print(f"Seuil de divergence calibre : {seuil_divergence_pct*100:.2f}%")


if __name__ == "__main__":
    main()
