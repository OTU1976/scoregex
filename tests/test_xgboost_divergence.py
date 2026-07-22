"""
tests/test_xgboost_divergence.py — Verrouille le comportement du signal
secondaire XGBoost (engine/xgboost_divergence.py). AJOUTÉ le 22/07/2026
(feu vert Helen : "XGBoost en signal secondaire ... flag de divergence,
pas remplacement").

Ces tests utilisent le VRAI modèle entraîné (models/xgb_divergence_v1.json,
committé dans le repo — voir scripts/train_xgboost_divergence.py), pas un
mock : on veut détecter une régression réelle (fichier modèle corrompu,
schéma de features désaligné, seuil de divergence mal chargé), pas
seulement tester la logique en isolation.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.xgboost_divergence import compute_divergence_xgboost, get_status


def test_modele_charge_correctement():
    """Le modèle committé doit se charger sans erreur, avec les
    métadonnées CV attendues (garde-fou contre un fichier modèle corrompu
    ou un schéma de features désaligné silencieusement)."""
    status = get_status()
    assert status["disponible"] is True, status.get("erreur")
    assert status["n_entrainement"] == 706
    assert status["version"] == "xgb_divergence_v1"


def test_cas_normal_pas_de_flag():
    """Un bien dont le prix hédonique est proche de la prédiction XGBoost
    ne doit JAMAIS déclencher le flag de divergence."""
    r = compute_divergence_xgboost(
        prix_m2_hedonique=4200, surface_m2=68, age_bien=48, annee_mutation=2025,
        dpe_note="D", code_commune="Ferney-Voltaire", type_bien="appartement",
    )
    assert r["disponible"] is True
    assert r["flag_divergence"] is False
    assert r["prix_m2_xgboost"] is not None


def test_divergence_forcee_declenche_le_flag():
    """Un prix hédonique artificiellement très éloigné DOIT déclencher le
    flag — sinon le signal secondaire ne sert à rien."""
    r = compute_divergence_xgboost(
        prix_m2_hedonique=1500, surface_m2=68, age_bien=48, annee_mutation=2025,
        dpe_note="D", code_commune="Ferney-Voltaire", type_bien="appartement",
    )
    assert r["disponible"] is True
    assert r["flag_divergence"] is True
    assert r["divergence_pct"] > r["seuil_divergence_pct"]


def test_commune_hors_domaine_ne_plante_pas():
    """Une commune hors des 8 communes d'entraînement -> disponible=False
    avec une raison explicite, JAMAIS une extrapolation silencieuse."""
    r = compute_divergence_xgboost(
        prix_m2_hedonique=4000, surface_m2=68, age_bien=48, annee_mutation=2025,
        dpe_note="D", code_commune="Paris", type_bien="appartement",
    )
    assert r["disponible"] is False
    assert "hors" in r["erreur"] or "non reconnue" in r["erreur"]


def test_repli_zone_pays_de_gex_nest_pas_confondu_avec_commune_gex():
    """RÉGRESSION : le repli littéral get_prix_m2() = "Pays de Gex" (aucune
    commune spécifique trouvée) ne doit JAMAIS être résolu comme la
    commune "Gex" via un matching par sous-chaîne (bug détecté et corrigé
    le 22/07/2026 avant déploiement — voir docstring
    _resoudre_code_commune). Un faux match ici fabriquerait un flag de
    divergence sur TOUS les biens en repli de zone."""
    r = compute_divergence_xgboost(
        prix_m2_hedonique=4000, surface_m2=68, age_bien=48, annee_mutation=2025,
        dpe_note="D", code_commune="Pays de Gex", type_bien="appartement",
    )
    assert r["disponible"] is False
    # Mais la VRAIE commune "Gex" doit, elle, matcher normalement.
    r_gex = compute_divergence_xgboost(
        prix_m2_hedonique=4000, surface_m2=68, age_bien=48, annee_mutation=2025,
        dpe_note="D", code_commune="Gex", type_bien="appartement",
    )
    assert r_gex["disponible"] is True


def test_dpe_invalide_ne_plante_pas():
    r = compute_divergence_xgboost(
        prix_m2_hedonique=4000, surface_m2=68, age_bien=48, annee_mutation=2025,
        dpe_note="Z", code_commune="Gex", type_bien="appartement",
    )
    assert r["disponible"] is False


def test_ne_leve_jamais_dexception_meme_avec_inputs_degenerees():
    """Le signal secondaire ne doit JAMAIS faire planter /estimate, quelle
    que soit l'entrée (defense-in-depth au-delà des cas déjà couverts)."""
    r = compute_divergence_xgboost(
        prix_m2_hedonique=0, surface_m2=0, age_bien=-5, annee_mutation=1900,
        dpe_note="", code_commune="", type_bien=None,
    )
    assert isinstance(r, dict)
    assert r["disponible"] is False
