"""
tests/test_engine.py — Suite de tests unitaires du moteur GexScore.

Objectif : verrouiller le comportement du moteur (engine/score_gexscore.py)
contre les régressions silencieuses. Chaque cas reflète soit une propriété
mathématique du modèle (ex: monotonie du DPE), soit un cas réel vérifié en
direct sur l'API de production (Rogeland, Abondance — voir commentaires).

Lancer localement :  pytest tests/ -v
Lancé automatiquement par .github/workflows/ci.yml à chaque push/PR sur main.
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.score_gexscore import (
    compute_avm_hedonique,
    compute_gexscore,
    compute_esg_score,
    is_deal_alert,
    compute_spatial_score_geostat,
    valider_dpe_step_log_monotone,
)


# ── Config de zone minimale pour les tests (miroir de 001_pays_de_gex.yaml) ──
# RECALIBRÉ le 21/07/2026 (A-B, B-C) — voir config/zones/001_pays_de_gex.yaml
# pour la méthodologie complète (crédibilité actuarielle local x national).
ZONE_CFG = {
    "hedonic_betas": {
        "dpe_step_log": {
            "A-B": -0.0155, "B-C": -0.088, "C-D": -0.050,
            "D-E": -0.070, "E-F": -0.120, "F-G": -0.150,
        },
        "distance_gva_per_min": 0.0,
        "threshold_gva_min": 15.0,
        "bruit_a40_penalty": -0.163,
        "vue_leman_bonus": 0.112,
        "ecole_intl_bonus": 0.071,
        "desert_medical_penalty": -0.089,
        "terrain_surface_beta": 0.152,
    },
    "score_weights": {
        "w_spatial": 0.28, "w_frontalier": 0.34, "w_esg": 0.17,
        "w_regime": 0.12, "w_quantum": 0.09,
    },
    "esg_weights": {
        "w_dpe": 0.4, "w_inondation": 0.2, "w_argile": 0.15, "w_ndvi": 0.25,
        "w_medical": 0.6, "w_mixite": 0.4,
        "w_E": 0.5, "w_S": 0.3, "w_G": 0.2,
    },
    "alerts": {"score_deal_threshold": 500},
}


# ── 1. Mécanisme exponentiel (pas de régression sur l'erreur "1,207") ───────
def test_avm_multiplicateur_est_exponentiel_pas_lineaire():
    """total_adj=20% doit donner exp(0.20), jamais 1.20 (piège identifié
    le 17/07/2026 après une explication erronée donnée à Helen)."""
    result = compute_avm_hedonique(
        prix_m2_median_zone=5000, surface_m2=100, dpe_note="C",
        t_gva_min=None, bruit_score=50, vue_leman=False,
        ecole_intl_500m=False, desert_medical=False, age_bien=20,
        zone_cfg=ZONE_CFG,
    )
    # DPE=C=référence, tout neutre -> total_adj = 0 -> multiplicateur = 1
    assert result["total_ajustement_pct"] == 0.0
    assert result["prix_m2_estime"] == 5000


def test_dpe_est_convexe_pas_lineaire():
    """Le saut A->C doit être nettement plus petit que C->G (courbe
    convexe voulue le 18/07/2026, remplace l'ancien beta uniforme -14.8%/
    classe qui donnait +29.6% pour A vs C, jugé excessif par Helen).

    RECALIBRÉ le 21/07/2026 : A-B/B-C revus (synthèse locale x nationale,
    voir YAML) -> adj_a passe de 9.0 à 10.35 (log*100 affiché, soit +10.9%
    réel vs C). adj_g inchangé (-39.0, voir note complète dans le YAML sur
    pourquoi G n'a PAS été baissé malgré l'intuition initiale d'Helen —
    ancré sur F une fois le problème de non-monotonicité détecté)."""
    kwargs = dict(
        prix_m2_median_zone=5000, surface_m2=100, t_gva_min=None,
        bruit_score=50, vue_leman=False, ecole_intl_500m=False,
        desert_medical=False, age_bien=20, zone_cfg=ZONE_CFG,
    )
    adj_a = compute_avm_hedonique(dpe_note="A", **kwargs)["ajustements"]["dpe"]
    adj_g = compute_avm_hedonique(dpe_note="G", **kwargs)["ajustements"]["dpe"]

    assert adj_a == 10.35        # +10.9% réel (était +9,4% réel / 9.0 affiché avant cette révision)
    assert adj_g < -30.0         # effet marqué en bas d'échelle (légal : loi Climat & Résilience)
    assert abs(adj_g) > abs(adj_a) * 2   # convexité : effet bas d'échelle >> effet haut d'échelle


def test_plafond_hedonique_35_pct_est_respecte():
    """Le garde-fou ±35% doit toujours s'appliquer, quelle que soit la
    source du cumul (bug réel Rogeland du 16/07/2026)."""
    result = compute_avm_hedonique(
        prix_m2_median_zone=5000, surface_m2=100, dpe_note="G",
        t_gva_min=60, bruit_score=0, vue_leman=False,
        ecole_intl_500m=False, desert_medical=True, age_bien=50,
        zone_cfg={**ZONE_CFG, "hedonic_betas": {
            **ZONE_CFG["hedonic_betas"], "distance_gva_per_min": -0.028,
        }},
    )
    assert abs(result["total_ajustement_pct"]) <= 35.0
    if result["total_ajustement_brut_pct"] != result["total_ajustement_pct"]:
        assert result["ajustement_plafonne"] is True


# ── 2. Cas réel vérifié en direct : 75bis Passage de l'Abondance, Gex ───────
def test_cas_reel_abondance_gex_dpe_a_sans_terrain():
    """Vérifié en direct sur l'API prod le 18/07/2026 après déploiement du
    DPE convexe : prix ~646 366 EUR (était 794 224 EUR avec l'ancien beta
    uniforme -14.8%/classe). RECALIBRÉ le 21/07/2026 (A-B/B-C revus) :
    ~655 151 EUR (recalculé, cf. YAML pour la méthodologie)."""
    result = compute_avm_hedonique(
        prix_m2_median_zone=5381, surface_m2=120, dpe_note="A",
        t_gva_min=29.97, bruit_score=50, vue_leman=False,
        ecole_intl_500m=False, desert_medical=True, age_bien=20,
        zone_cfg=ZONE_CFG, surface_terrain_m2=0, surface_terrain_ref_m2=684,
    )
    assert result["ajustements"]["dpe"] == 10.35
    assert result["ajustements"]["terrain"] == 0.0   # pas de terrain -> 0, jamais deviné
    assert 648_000 <= result["prix_estime_eur"] <= 663_000


# ── 3. Cas réel vérifié : 522 Rue de Rogeland, Gex (terrain 800m²) ──────────
def test_cas_reel_rogeland_gex_dpe_c_avec_terrain():
    """Vérifié en direct sur l'API prod : prix ~604 969-605 040 EUR."""
    result = compute_avm_hedonique(
        prix_m2_median_zone=5381, surface_m2=120, dpe_note="C",
        t_gva_min=29.97, bruit_score=50, vue_leman=False,
        ecole_intl_500m=False, desert_medical=True, age_bien=5,
        zone_cfg=ZONE_CFG, surface_terrain_m2=800, surface_terrain_ref_m2=684,
    )
    assert result["ajustements"]["dpe"] == 0.0   # C = référence
    assert result["ajustements"]["terrain"] > 0.0
    assert 598_000 <= result["prix_estime_eur"] <= 612_000


def test_terrain_absent_ou_appartement_ne_donne_jamais_dajustement_devine():
    result = compute_avm_hedonique(
        prix_m2_median_zone=5000, surface_m2=80, dpe_note="C",
        t_gva_min=None, bruit_score=50, vue_leman=False,
        ecole_intl_500m=False, desert_medical=False, age_bien=20,
        zone_cfg=ZONE_CFG, surface_terrain_m2=None, surface_terrain_ref_m2=684,
    )
    assert result["ajustements"]["terrain"] == 0.0


# ── 4. GexScore : 5 composantes réelles dans le calcul (pas 3) ──────────────
def test_gexscore_a_bien_5_composantes_ponderees():
    result = compute_gexscore(
        score_spatial=70.7, score_frontalier=43.2, score_esg=66.0,
        regime_bull_prob=0.57, zone_cfg=ZONE_CFG, qubo_quality=0.75,
    )
    composants = result["composants"]
    assert set(composants.keys()) == {
        "score_spatial", "score_frontalier", "score_esg",
        "regime_bull_pct", "qubo_quality_pct",
    }
    raw = (
        0.28 * 70.7 + 0.34 * 43.2 + 0.17 * 66.0 + 0.12 * 57.0 + 0.09 * 75.0
    )
    assert result["gexscore"] == round(min(1000.0, raw * 10), 1)


def test_gexscore_grade_thresholds():
    assert compute_gexscore(100, 100, 100, 1.0, ZONE_CFG, 1.0)["grade"] == "AAA"
    assert compute_gexscore(0, 0, 0, 0.0, ZONE_CFG, 0.0)["grade"] == "CCC"


# ── 5. Deal Alert : ne doit jamais s'activer sur un prix nul/négatif ────────
def test_deal_alert_ignore_prix_annonce_nul():
    result = is_deal_alert(
        prix_annonce=0, avm_hedonique=500_000, gexscore=800,
        zone_cfg=ZONE_CFG,
    )
    assert result["is_deal_alert"] is False


def test_deal_alert_detecte_une_vraie_sous_evaluation():
    result = is_deal_alert(
        prix_annonce=400_000, avm_hedonique=500_000, gexscore=800,
        zone_cfg=ZONE_CFG,
    )
    assert result["is_deal_alert"] is True
    assert result["discount_pct"] == 20.0


# ── 7. Score spatial géostatistique réel (ajouté 18/07/2026) ────────────────
def test_spatial_geostat_neutre_si_local_egal_median():
    score = compute_spatial_score_geostat(
        prix_m2_local_pondere=5000, prix_m2_median_zone=5000, n_voisins=157,
    )
    assert score == 50.0


def test_spatial_geostat_prime_si_local_plus_cher():
    score = compute_spatial_score_geostat(
        prix_m2_local_pondere=6000, prix_m2_median_zone=5000, n_voisins=100,
    )
    assert score > 50.0
    assert score <= 100.0


def test_spatial_geostat_decote_si_local_moins_cher():
    score = compute_spatial_score_geostat(
        prix_m2_local_pondere=4000, prix_m2_median_zone=5000, n_voisins=100,
    )
    assert score < 50.0
    assert score >= 0.0


def test_spatial_geostat_none_si_pas_assez_de_voisins():
    """Sous n_min voisins geocodes -> None (jamais un score devine), pour
    forcer l'appelant a retomber sur le proxy hedonique."""
    assert compute_spatial_score_geostat(5500, 5000, n_voisins=2) is None
    assert compute_spatial_score_geostat(None, 5000, n_voisins=200) is None


# ── 8. dpe_step_log_by_type + garde-fou monotonie (ajouté 21/07/2026) ───────
# Chantier lancé par une question d'Helen ("peux-tu implémenter un
# dpe_step_log différencié par type ?"). Réponse : oui, architecture
# rétrocompatible ci-dessous, PAS ENCORE peuplée en prod (voir YAML).

def test_dpe_step_log_by_type_utilise_si_present():
    """Si zone_cfg fournit dpe_step_log_by_type.maison, il doit être utilisé
    à la place du dpe_step_log partagé pour type_bien='maison'."""
    cfg = {**ZONE_CFG, "hedonic_betas": {
        **ZONE_CFG["hedonic_betas"],
        "dpe_step_log_by_type": {
            "maison": {
                "A-B": -0.045, "B-C": -0.045, "C-D": -0.080,
                "D-E": -0.070, "E-F": -0.020, "F-G": -0.170,
            },
        },
    }}
    kwargs = dict(
        prix_m2_median_zone=5000, surface_m2=100, t_gva_min=None,
        bruit_score=50, vue_leman=False, ecole_intl_500m=False,
        desert_medical=False, age_bien=20, zone_cfg=cfg,
    )
    adj_maison = compute_avm_hedonique(dpe_note="D", type_bien="maison", **kwargs)["ajustements"]["dpe"]
    adj_appart = compute_avm_hedonique(dpe_note="D", type_bien="appartement", **kwargs)["ajustements"]["dpe"]
    # maison utilise la courbe dédiée (C-D=-0.080 -> -8.0), appartement
    # retombe sur dpe_step_log partagé de ZONE_CFG (C-D=-0.050 -> -5.0)
    assert adj_maison == -8.0
    assert adj_appart == -5.0


def test_dpe_step_log_by_type_repli_si_absent():
    """Sans dpe_step_log_by_type dans zone_cfg (cas ZONE_CFG standard,
    comme la prod aujourd'hui), type_bien='maison' doit retomber
    silencieusement sur le dpe_step_log partagé — rétrocompatibilité totale."""
    kwargs = dict(
        prix_m2_median_zone=5000, surface_m2=100, dpe_note="G", t_gva_min=None,
        bruit_score=50, vue_leman=False, ecole_intl_500m=False,
        desert_medical=False, age_bien=20, zone_cfg=ZONE_CFG,
    )
    sans_type = compute_avm_hedonique(**kwargs)["ajustements"]["dpe"]
    avec_type_maison = compute_avm_hedonique(type_bien="maison", **kwargs)["ajustements"]["dpe"]
    assert sans_type == avec_type_maison == -39.0


def test_valider_dpe_step_log_monotone_accepte_courbe_valide():
    """La courbe de production (ZONE_CFG) ne doit jamais lever d'erreur."""
    valider_dpe_step_log_monotone(ZONE_CFG["hedonic_betas"]["dpe_step_log"])


def test_valider_dpe_step_log_monotone_detecte_erreur():
    """Garde-fou ajouté le 21/07/2026 : une courbe où G finit mieux valorisé
    que F (ex. F-G positif, ou trop faible face à un F trop pénalisant)
    doit être rejetée avant déploiement — c'est exactement le bug détecté
    et évité pendant le chantier de recalibrage A/B/G/type (voir YAML)."""
    courbe_cassee = {
        "A-B": -0.045, "B-C": -0.045, "C-D": -0.050,
        "D-E": -0.070, "E-F": -0.300, "F-G": 0.050,  # F-G positif -> G < F en pénalité, invalide
    }
    try:
        valider_dpe_step_log_monotone(courbe_cassee)
        assert False, "aurait dû lever ValueError sur une courbe non monotone"
    except ValueError as e:
        assert "NON MONOTONE" in str(e)


# ── 6. ESG score : bornes [0,100] toujours respectées ───────────────────────
def test_esg_score_reste_dans_les_bornes():
    result = compute_esg_score(
        dpe_note="G", inondation_risk=1.0, argile_risk=1.0, ndvi=0.0,
        nb_medecins=0, mixite_sociale_score=0, vacance_logement_pct=100,
        pop_growth_5y_pct=-50, zone_cfg=ZONE_CFG,
    )
    assert 0.0 <= result["esg_score"] <= 100.0
    assert result["esg_grade"] in list("ABCDEFG")
