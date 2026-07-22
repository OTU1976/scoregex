"""
engine/xgboost_divergence.py — Signal secondaire XGBoost (challenger model).

FEU VERT Helen (22/07/2026, en reponse a la proposition faite le meme jour) :
"XGBoost en signal secondaire : ... flag de divergence, pas remplacement" -> OUI.

Ce module N'EST PAS un moteur de pricing. Il ne remplace JAMAIS
compute_avm_hedonique() (engine/score_gexscore.py), qui reste la SEULE
source du prix affiche a l'utilisateur. Ce module fournit uniquement un
second avis independant (gradient boosting non-parametrique, entraine sur
les memes 706 transactions reelles DVF x ADEME que la regression
age-controlee de la tache #13) pour DETECTER, pas corriger, les cas ou le
modele hedonique et un modele alternatif divergent fortement -- exactement
le role d'un "challenger model" au sens SR 11-7 (Federal Reserve / OCC,
"Supervisory Guidance on Model Risk Management", 2011) : deux modeles
independants sur les memes donnees, tout desaccord materiel entre eux
declenche une revue humaine plutot qu'une correction automatique.

Limites methodologiques honnetes (a ne jamais masquer) :
- n=706 apres nettoyage (102 lignes rejetees, cf. train_xgboost_divergence.py)
  est un petit echantillon pour du gradient boosting.
- Le feature set d'entrainement (surface, age, DPE, commune, type, annee)
  est plus PAUVRE que celui du modele hedonique (pas de distance Geneve,
  bruit, vue Leman, ecole internationale, desert medical -- features
  geo-calculees non presentes dans le fichier DVF x ADEME source). Le
  challenger a donc structurellement moins de pouvoir explicatif
  (R2 CV out-of-sample = 0.312, contre R2 = 0.891 du modele hedonique
  complet cite dans compute_avm_hedonique()) : il ne peut PAS distinguer
  un ecart du au bruit/vue/ecole d'une vraie anomalie de prix. Le seuil
  de divergence est calibre sur CETTE distribution de residus plus large
  -- amelioration future proposee : enrichir le jeu d'entrainement avec
  les memes variables geo pour resserrer le seuil (voir meta JSON,
  "TODO_ameliorations").
- Domaine de validite strict : seulement les 8 communes du Pays de Gex
  couvertes par l'entrainement, et une note DPE A-G connue. Hors de ce
  domaine, le module retourne disponible=False plutot que d'extrapoler
  silencieusement (un one-hot commune inconnu produirait une prediction
  non fiable, jamais observee a l'entrainement).
"""
import json
import os
import threading
from typing import Optional

try:
    import xgboost as xgb  # noqa: F401  (import isole : voir _get_model())
    _XGBOOST_DISPONIBLE = True
except Exception:  # pragma: no cover - environnement sans xgboost installe
    _XGBOOST_DISPONIBLE = False

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(HERE, "..", "models", "xgb_divergence_v1.json")
META_PATH = os.path.join(HERE, "..", "models", "xgb_divergence_v1.meta.json")

DPE_CLASSES_ORDRE = ["A", "B", "C", "D", "E", "F", "G"]

# Mapping code INSEE <-> nom, IDENTIQUE à scripts/process_dvf.py::COMMUNES_GEX
# (même 8 communes Pays de Gex). Nécessaire car api/main.py ne dispose que du
# NOM de commune après get_prix_m2() (`commune_nom`, ex. "Ferney-Voltaire"),
# jamais du code INSEE, alors que le modèle a été entraîné avec le code
# (colonne `code_commune` de dpe_regression_sample.csv). Résolution STRICTE
# par égalité exacte (voir _resoudre_code_commune ci-dessous et son
# docstring pour la raison précise d'éviter tout matching par sous-chaîne).
COMMUNES_GEX_NOM = {
    "01071": "Cessy",
    "01160": "Ferney-Voltaire",
    "01173": "Gex",
    "01313": "Prevessin-Moens",
    "01281": "Ornex",
    "01354": "Saint-Genis-Pouilly",
    "01401": "Sergy",
    "01419": "Thoiry",
}


def _resoudre_code_commune(commune_nom_ou_code: str, communes_ordre: list) -> Optional[str]:
    """Resolution STRICTE (egalite exacte, insensible a la casse) — jamais
    de sous-chaine. get_prix_m2() ne retourne QUE deux formes : un des 8 noms
    de commune exacts (ex. "Ferney-Voltaire"), ou le repli littéral
    "Pays de Gex" quand aucune commune n'a matché (PRIX_ZONE_DEFAULT). Une
    correspondance par sous-chaine serait dangereusement fausse ici : "Gex"
    est a la fois un nom de commune réel ET un mot entier contenu dans le
    repli "Pays de Gex" — un match par sous-chaine ferait passer TOUS les
    biens en repli de zone (commune inconnue) pour des biens de Gex,
    fabriquant un faux signal de divergence. D'où l'égalité stricte."""
    if not commune_nom_ou_code:
        return None
    val = commune_nom_ou_code.strip()
    if val in communes_ordre:
        return val
    val_l = val.lower()
    for code, nom in COMMUNES_GEX_NOM.items():
        if code in communes_ordre and val_l == nom.lower():
            return code
    return None


_lock = threading.Lock()
_booster = None
_meta = None
_load_error: Optional[str] = None


def _load_once():
    """Charge le booster + les metadonnees UNE SEULE FOIS par instance
    serverless (cache module-level, reutilise sur les invocations chaudes
    de la meme fonction Vercel). Ne leve jamais d'exception : toute erreur
    est capturee dans _load_error et lue par les appelants via
    get_status(), pour que /health et /estimate restent honnetes sur la
    disponibilite reelle du signal plutot que de planter."""
    global _booster, _meta, _load_error
    with _lock:
        if _booster is not None or _load_error is not None:
            return
        if not _XGBOOST_DISPONIBLE:
            _load_error = "xgboost non installe dans l'environnement"
            return
        try:
            if not os.path.exists(MODEL_PATH):
                _load_error = f"modele introuvable : {MODEL_PATH}"
                return
            with open(META_PATH, "r", encoding="utf-8") as f:
                _meta = json.load(f)
            bst = xgb.Booster()
            bst.load_model(MODEL_PATH)
            _booster = bst
        except Exception as e:  # pragma: no cover
            _load_error = f"echec chargement modele XGBoost : {e!r}"


def get_status() -> dict:
    """Etat du module, pour /health. Ne charge PAS le modele juste pour
    repondre (evite un cold-start couteux sur un simple health-check) --
    ne signale 'ok' que si un chargement a deja reussi ailleurs, ou tente
    un chargement paresseux minimal sinon."""
    _load_once()
    if _booster is not None and _meta is not None:
        cv = _meta.get("cv_5fold_out_of_sample", {})
        return {
            "disponible": True,
            "version": _meta.get("version"),
            "n_entrainement": _meta.get("n_transactions_entrainement"),
            "r2_cv": cv.get("r2"),
            "seuil_divergence_pct": _meta.get("seuil_divergence_pct"),
        }
    return {"disponible": False, "erreur": _load_error or "non initialise"}


def _construire_features(surface_m2, age_bien, annee_mutation, dpe_note, code_commune, type_bien):
    dpe = (dpe_note or "").strip().upper()
    if dpe not in DPE_CLASSES_ORDRE:
        return None, f"DPE '{dpe_note}' hors domaine (A-G attendu)"
    communes_ordre = _meta["communes_ordre"]
    commune = _resoudre_code_commune(code_commune or "", communes_ordre)
    if commune is None:
        return None, f"commune '{code_commune}' non reconnue parmi les 8 communes d'entrainement ({communes_ordre})"
    type_key = "Maison" if (type_bien or "").strip().lower() == "maison" else "Appartement"

    feats = [float(surface_m2), float(age_bien), float(annee_mutation), float(DPE_CLASSES_ORDRE.index(dpe))]
    feats += [1.0 if commune == c else 0.0 for c in communes_ordre]
    feats += [1.0 if type_key == t else 0.0 for t in _meta["type_local_ordre"]]
    return feats, None


def compute_divergence_xgboost(
    prix_m2_hedonique: float,
    surface_m2: float,
    age_bien: int,
    annee_mutation: int,
    dpe_note: str,
    code_commune: str,
    type_bien: Optional[str],
) -> dict:
    """Retourne le second avis XGBoost + le flag de divergence.

    Cle de retour :
      disponible          : bool -- False si modele absent OU bien hors
                             domaine d'entrainement (jamais d'extrapolation
                             silencieuse, voir docstring module).
      prix_m2_xgboost      : float | None
      divergence_pct       : float | None -- (xgb - hedonique) / hedonique
      seuil_divergence_pct : float | None -- seuil calibre CV (voir meta)
      flag_divergence      : bool | None -- |divergence_pct| > seuil
      erreur               : str | None -- raison si disponible=False

    N'A JAMAIS d'effet sur prix_estime_eur / prix_m2_estime hedonique :
    ce sont des cles PURTEMENT INFORMATIVES ajoutees a la reponse /estimate,
    jamais utilisees pour recalculer le prix affiche.
    """
    _load_once()
    if _booster is None or _meta is None:
        return {
            "disponible": False,
            "prix_m2_xgboost": None,
            "divergence_pct": None,
            "seuil_divergence_pct": None,
            "flag_divergence": None,
            "erreur": _load_error or "modele non charge",
        }

    feats, err = _construire_features(surface_m2, age_bien, annee_mutation, dpe_note, code_commune, type_bien)
    if feats is None:
        return {
            "disponible": False,
            "prix_m2_xgboost": None,
            "divergence_pct": None,
            "seuil_divergence_pct": None,
            "flag_divergence": None,
            "erreur": err,
        }

    try:
        dmat = xgb.DMatrix(
            [feats],
            feature_names=_meta["feature_names_ordre"],
        )
        prix_m2_xgb = float(_booster.predict(dmat)[0])
    except Exception as e:  # pragma: no cover
        return {
            "disponible": False,
            "prix_m2_xgboost": None,
            "divergence_pct": None,
            "seuil_divergence_pct": None,
            "flag_divergence": None,
            "erreur": f"echec prediction : {e!r}",
        }

    if prix_m2_hedonique <= 0:
        divergence_pct = None
        flag = None
    else:
        divergence_pct = (prix_m2_xgb - prix_m2_hedonique) / prix_m2_hedonique
        seuil = _meta["seuil_divergence_pct"]
        flag = abs(divergence_pct) > seuil

    return {
        "disponible": True,
        "prix_m2_xgboost": round(prix_m2_xgb),
        "divergence_pct": round(divergence_pct * 100, 1) if divergence_pct is not None else None,
        "seuil_divergence_pct": round(_meta["seuil_divergence_pct"] * 100, 1),
        "flag_divergence": flag,
        "erreur": None,
    }
