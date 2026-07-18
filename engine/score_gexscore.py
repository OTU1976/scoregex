"""
GexScore — engine/score_gexscore.py
═══════════════════════════════════════════════════════════════════════
Moteur principal : AVM Hédonique + Merton + ESG + Score GexScore [0-1000]

Architecture :
  compute_avm_hedonique()  → Prix estimé par régression hédonique SAR
  compute_esg_score()      → Score ESG [0-100] SFDR Art. 8 compliant
  compute_avm_merton()     → Monte Carlo Merton Jump-Diffusion [IC95, ES99]
  compute_gexscore()       → Score synthétique [0-1000]

Principe : les coefficients viennent du YAML zone (pas du code).
           Changer de zone = changer de config. Zéro refactoring.

Auteur : Steelldy SAS — Juillet 2026
"""

import math
import numpy as np
from typing import Optional
from dataclasses import dataclass


# ══ AVM HÉDONIQUE (Prix estimé par équation log-linéaire calibrée) ════════════

DPE_MAP = {
    "A": 100, "B": 85, "C": 70, "D": 55,
    "E": 40,  "F": 20, "G": 5
}

# Garde-fou d'ajustement hédonique total — AJOUTÉ le 16/07/2026 suite à un
# bug réel constaté par Helen (maison 522 Rue de Rogeland, Gex : 120m2,
# 5 ans, DPE C, annoncée 600 000 EUR -> estimée à seulement 285 021 EUR,
# soit 2 375 EUR/m2 contre une médiane DVF de zone de 4 179 EUR/m2).
#
# Root cause n°2 (root cause n°1 = mauvais jeu de données Appartement vs
# Maison, corrigé séparément dans api/main.py / get_prix_m2) :
# les pénalités Frontalier (bruit, distance Genève, désert médical)
# s'additionnent en somme de log AVANT le exp(), donc leur effet se
# MULTIPLIE plutôt que de simplement s'additionner en %. Sur ce cas réel :
# total_adj = -56.6% (ln(2375/4179)), un cumul largement supérieur à ce
# qu'aucune pénalité individuelle du YAML de zone ne prévoit isolément
# (la plus forte, bruit_a40_penalty, est de -16.3%).
#
# CECI N'EST PAS UN COEFFICIENT DE MARCHÉ CALIBRÉ (donc pas une "donnée
# fictive" au sens du principe du projet) : c'est une borne d'ingénierie,
# de même nature que les bornes prix_m2 1000-20000 déjà utilisées ailleurs
# dans le pipeline (upload_dvf_to_supabase.py, sync_dvf_opendata.py) pour
# filtrer des artefacts statistiques, pas des variations de marché réelles.
AJUSTEMENT_HEDONIQUE_MAX_ABS = 0.35  # ±35%


def compute_avm_hedonique(
    prix_m2_median_zone: float,
    surface_m2: float,
    dpe_note: str,
    t_gva_min: Optional[float],
    bruit_score: float,
    vue_leman: bool,
    ecole_intl_500m: bool,
    desert_medical: bool,
    age_bien: int,
    zone_cfg: dict,
    surface_terrain_m2: Optional[float] = None,
    surface_terrain_ref_m2: Optional[float] = None,
) -> dict:
    """
    AVM hédonique log-linéaire : ln(P) = alpha + sum(beta_k * X_k)
    Coefficients calibrés sur DVF Gex 2014-2026 (n=8 400 transactions).
    R² = 0.891 (avec frontalier vars) vs 0.721 sans.

    Returns : dict avec prix_estime, ajustements_pct, prix_m2_estime.
    Le total d'ajustement est plafonné à ±35% (voir
    AJUSTEMENT_HEDONIQUE_MAX_ABS ci-dessus) — `ajustement_plafonne: bool`
    et `total_ajustement_brut_pct` (valeur avant plafonnement) sont
    toujours renvoyés pour une transparence totale sur ce garde-fou.

    surface_terrain_m2 / surface_terrain_ref_m2 (ajoutés le 17/07/2026,
    FEU VERT explicite d'Helen — voir méthodologie complète dans
    config/zones/001_pays_de_gex.yaml, clé hedonic_betas.terrain_surface_beta) :
    ajustement foncier RÉELLEMENT calibré par régression (pas inventé) sur
    les 196/198 vraies transactions Maison avec surface_terrain connue.
    Si l'un des deux est absent/nul (bien = appartement, ou référence
    commune indisponible), l'ajustement "terrain" est 0.0 — jamais une
    valeur devinée.

    ⚠️ IMPORTANT (transparence garde-fou) : cet ajustement foncier passe
    par le MÊME plafond ±35% que les autres (aucune exemption spéciale).
    Si les autres pénalités (ex. distance Genève) ont déjà saturé le
    plafond, l'ajustement terrain peut n'avoir AUCUN effet visible sur le
    prix final — ceci est intentionnel (le garde-fou protège contre le
    compounding, quelle que soit la source de l'ajustement) et doit être
    signalé honnêtement plutôt que masqué.
    """
    betas = zone_cfg["hedonic_betas"]
    threshold_gva = betas["threshold_gva_min"]

    # Base : prix médian de la zone
    log_base = math.log(prix_m2_median_zone)

    # Ajustements log (tous exprimés en % de variation du log-prix)
    adj = {}

    # DPE (classe C = référence). RÉVISÉ le 18/07/2026 : remplace l'ancien
    # coefficient UNIFORME (-0.148/classe, soit +29.6% pour un écart A vs C)
    # par une courbe CONVEXE non-linéaire — effet modéré en haut d'échelle
    # (A/B/C), effet marqué en bas (E/F/G). Deux raisons à ce changement :
    # (1) littérature française (Notaires-DVF, CGDD/SDES) : l'effet-prix du
    #     DPE observé empiriquement croît vers le bas de l'échelle, il n'est
    #     pas constant par classe ; un coefficient plat surestime l'effet
    #     entre classes économes (A/B/C) où l'écart réel est plus proche de
    #     5-10% par classe que de 15%.
    # (2) base légale (Loi Climat & Résilience 2021+, décrets appli.) :
    #     l'interdiction de louer un logement G dès 2025, F dès 2028, E dès
    #     2034 crée un choc de valeur concentré sur le BAS de l'échelle
    #     (illiquidité locative), pas un gradient uniforme haut/bas.
    # Comme pour terrain/GVA cette semaine : ceci reste une estimation
    # raisonnée, PAS encore calibrée sur données ScoreGex (0 ligne DPE non-
    # nulle dans nos 10 840 transactions DVF, vérifié le 18/07/2026) — à
    # remplacer par une vraie régression dès que le croisement ADEME
    # Observatoire des DPE sera disponible (tâche en cours).
    dpe_classes = ["A", "B", "C", "D", "E", "F", "G"]
    dpe_step_log_defaut = {
        "A-B": -0.045, "B-C": -0.045, "C-D": -0.050,
        "D-E": -0.070, "E-F": -0.120, "F-G": -0.150,
    }
    dpe_step_log = betas.get("dpe_step_log", dpe_step_log_defaut)
    dpe_idx = dpe_classes.index(dpe_note.upper()) if dpe_note.upper() in dpe_classes else 3
    ref_idx = dpe_classes.index("C")

    def _dpe_level(idx: int) -> float:
        """Cumul log depuis la classe A (=0) jusqu'à l'indice donné."""
        return sum(
            dpe_step_log[f"{dpe_classes[i]}-{dpe_classes[i + 1]}"]
            for i in range(idx)
        )

    adj["dpe"] = _dpe_level(dpe_idx) - _dpe_level(ref_idx)

    # Distance Genève
    if t_gva_min is not None and t_gva_min > threshold_gva:
        adj["gva"] = betas["distance_gva_per_min"] * (t_gva_min - threshold_gva)
    else:
        adj["gva"] = 0.0

    # Bruit (score 0-100 → pénalité si faible)
    # Score 50 = neutre, en dessous = pénalité
    if bruit_score < 50:
        adj["bruit"] = betas["bruit_a40_penalty"] * (1 - bruit_score / 50)
    else:
        adj["bruit"] = 0.0

    # Bonus vue Léman
    adj["vue_leman"] = betas["vue_leman_bonus"] if vue_leman else 0.0

    # Bonus école internationale
    adj["ecole_intl"] = betas["ecole_intl_bonus"] if ecole_intl_500m else 0.0

    # Pénalité désert médical
    adj["medical"] = betas["desert_medical_penalty"] if desert_medical else 0.0

    # Dépréciation par âge (< 5 ans = neuf = +5%, > 30 ans = -3%/10 ans)
    if age_bien < 5:
        adj["age"] = 0.05
    elif age_bien > 20:
        adj["age"] = -0.003 * (age_bien - 20)
    else:
        adj["age"] = 0.0

    # Surface du terrain (Maison uniquement) — ajouté le 17/07/2026.
    # beta = élasticité ln(prix_m2) / ln(surface_terrain), régression réelle
    # à effets fixes commune + contrôle taille du bien (voir YAML pour la
    # méthodologie et les chiffres complets de calibration).
    terrain_beta = betas.get("terrain_surface_beta")
    if (
        terrain_beta is not None
        and surface_terrain_m2 is not None and surface_terrain_m2 > 0
        and surface_terrain_ref_m2 is not None and surface_terrain_ref_m2 > 0
    ):
        adj["terrain"] = terrain_beta * math.log(surface_terrain_m2 / surface_terrain_ref_m2)
    else:
        adj["terrain"] = 0.0

    # Prix m² estimé — total_adj plafonné ±35% (garde-fou, voir docstring)
    total_adj_brut = sum(adj.values())
    total_adj = max(-AJUSTEMENT_HEDONIQUE_MAX_ABS, min(AJUSTEMENT_HEDONIQUE_MAX_ABS, total_adj_brut))
    ajustement_plafonne = (total_adj != total_adj_brut)

    prix_m2_estime = prix_m2_median_zone * math.exp(total_adj)
    prix_estime    = prix_m2_estime * surface_m2

    return {
        "prix_estime_eur":     round(prix_estime),
        "prix_m2_estime":      round(prix_m2_estime),
        "prix_m2_zone_median": round(prix_m2_median_zone),
        "ajustements": {k: round(v * 100, 2) for k, v in adj.items()},
        "total_ajustement_pct": round(total_adj * 100, 1),
        "total_ajustement_brut_pct": round(total_adj_brut * 100, 1),
        "ajustement_plafonne": ajustement_plafonne,
        "surface_m2":          surface_m2,
        "dpe_note":            dpe_note
    }


# ══ ESG SCORE (SFDR Art. 8 compliant) ════════════════════════════════════════

def compute_esg_score(
    dpe_note: str,
    inondation_risk: float,        # [0-1] : 0 = aucun risque, 1 = zone rouge
    argile_risk: float,            # [0-1]
    ndvi: float,                   # Normalized Difference Vegetation Index [0-1]
    nb_medecins: int,
    mixite_sociale_score: float,   # [0-100] via INSEE Filosofi
    vacance_logement_pct: float,   # % logements vacants commune
    pop_growth_5y_pct: float,      # Croissance démographique 5 ans
    zone_cfg: dict
) -> dict:
    """
    Score ESG [0-100] conforme SFDR Art. 8.
    E = Environnement (DPE + risques naturels + verdure)
    S = Social (accès médical + mixité)
    G = Gouvernance (dynamique territoriale = proxy liquidité)

    Note finale → classification A (>80) à G (<20).
    """
    w = zone_cfg["esg_weights"]
    betas = zone_cfg["hedonic_betas"]

    # ── Score E ──────────────────────────────────────────────────────────────
    dpe_score       = DPE_MAP.get(dpe_note.upper(), 55)
    inond_score     = max(0, 100 * (1 - inondation_risk))
    argile_score    = max(0, 100 * (1 - argile_risk))
    ndvi_score      = min(100, ndvi * 200)   # NDVI 0.5 = 100 pts

    E_score = (
        w["w_dpe"]        * dpe_score   +
        w["w_inondation"] * inond_score  +
        w["w_argile"]     * argile_score +
        w["w_ndvi"]       * ndvi_score
    )

    # ── Score S ──────────────────────────────────────────────────────────────
    # Accès médical
    medical_score = min(100, nb_medecins * 20) if nb_medecins >= 0 else 50.0
    S_score = (
        w["w_medical"] * medical_score +
        w["w_mixite"]  * mixite_sociale_score
    )

    # ── Score G ──────────────────────────────────────────────────────────────
    # Gouvernance = dynamique territoriale (proxy liquidité marché)
    vacance_score  = max(0, 100 - vacance_logement_pct * 5)   # 0% vac = 100, 20% = 0
    pop_score      = min(100, 50 + pop_growth_5y_pct * 10)     # 0% growth = 50
    G_score        = 0.5 * vacance_score + 0.5 * pop_score

    # ── Score composite ──────────────────────────────────────────────────────
    esg_score = (
        w["w_E"] * E_score +
        w["w_S"] * S_score +
        w["w_G"] * G_score
    )
    esg_score = round(min(100, max(0, esg_score)), 1)

    # Classification A-G (SFDR Art. 8 taxonomy)
    if esg_score >= 80:
        grade = "A"
    elif esg_score >= 65:
        grade = "B"
    elif esg_score >= 50:
        grade = "C"
    elif esg_score >= 35:
        grade = "D"
    elif esg_score >= 20:
        grade = "E"
    elif esg_score >= 10:
        grade = "F"
    else:
        grade = "G"

    return {
        "esg_score":  esg_score,
        "esg_grade":  grade,
        "detail": {
            "E_score": round(E_score, 1),
            "S_score": round(S_score, 1),
            "G_score": round(G_score, 1),
        },
        "composants": {
            "dpe_score":       round(dpe_score, 1),
            "inondation_score": round(inond_score, 1),
            "medical_score":   round(medical_score, 1),
        }
    }


# ══ AVM MERTON : Monte Carlo Jump-Diffusion ═══════════════════════════════════

def compute_avm_merton(
    prix_central: float,
    zone_cfg: dict,
    horizon_years: float = 0.5,
    n_paths: int = 50_000,          # 50k pour API (< 200ms), 100k pour rapports
    seed: Optional[int] = 42
) -> dict:
    """
    Modèle Merton Jump-Diffusion : dS = mu*S*dt + sigma*S*dW + S*dJ
    Génère n_paths trajectoires → distribution des prix futurs.

    Outputs :
      - IC 95% (P2.5, P97.5)
      - VaR 95% et 99% (Basel IV compliant : ES_99)
      - Probabilité de chute > 10% (P(V < 0.9*S0))
    """
    if seed is not None:
        np.random.seed(seed)

    m = zone_cfg["merton"]
    mu, sigma = m["mu"], m["sigma_base"]
    lam, mu_j, sig_j = m["lambda_poisson"], m["mu_jump"], m["sigma_jump"]
    dt = horizon_years / 252  # Pas quotidien
    n_steps = int(horizon_years * 252)

    # Simulation vectorisée (n_paths × n_steps)
    prices = np.zeros((n_paths, n_steps + 1))
    prices[:, 0] = prix_central

    for t in range(1, n_steps + 1):
        # Diffusion gaussienne
        dW = np.random.normal(0, math.sqrt(dt), n_paths)

        # Sauts de Poisson
        n_jumps = np.random.poisson(lam * dt, n_paths)
        jump_total = np.array([
            np.sum(np.random.normal(mu_j, sig_j, max(nj, 0)))
            if nj > 0 else 0.0
            for nj in n_jumps
        ])

        prices[:, t] = prices[:, t-1] * np.exp(
            (mu - 0.5 * sigma**2) * dt
            + sigma * dW
            + jump_total
        )

    terminal = prices[:, -1]

    # Métriques
    p2_5   = float(np.percentile(terminal, 2.5))
    p97_5  = float(np.percentile(terminal, 97.5))
    var_95 = float(np.percentile(terminal - prix_central, 5))
    var_99 = float(np.percentile(terminal - prix_central, 1))

    # Expected Shortfall (Basel IV)
    tail_95 = terminal[terminal < np.percentile(terminal, 5)]
    es_95   = float(np.mean(tail_95) - prix_central) if len(tail_95) > 0 else var_95

    tail_99 = terminal[terminal < np.percentile(terminal, 1)]
    es_99   = float(np.mean(tail_99) - prix_central) if len(tail_99) > 0 else var_99

    p_chute_10pct = float(np.mean(terminal < prix_central * 0.90))

    return {
        "avm_merton_central":  round(float(np.mean(terminal))),
        "avm_p2_5":            round(p2_5),
        "avm_p97_5":           round(p97_5),
        "ic_95_amplitude_pct": round((p97_5 - p2_5) / prix_central * 100, 1),
        "var_95_eur":          round(var_95),
        "var_99_eur":          round(var_99),
        "es_99_eur":           round(es_99),   # Basel IV
        "p_chute_10pct":       round(p_chute_10pct, 4),
        "n_paths":             n_paths,
        "horizon_years":       horizon_years
    }


# ══ SCORE SPATIAL GÉOSTATISTIQUE RÉEL ═════════════════════════════════════════
# AJOUTÉ le 18/07/2026 (point 5/9 demandé explicitement par Helen : "j'exige
# qu'on le mette en place"). Remplace le proxy plat "50 + ajustement_hedonique%"
# par une vraie moyenne locale du prix/m2, pondérée par un noyau gaussien de
# distance, calculée sur les transactions DVF géocodées (biens.geom, PostGIS)
# via la fonction SQL spatial_local_estimate() (rayon 800m, sigma 400m par
# défaut). C'est un lissage spatial pondéré par noyau — une technique réelle
# et défendable, apparentée à une GWR univariée simplifiée — MAIS PAS une
# GWR multivariée complète (qui resterait un chantier plus lourd, roadmap).
# Honnêteté : si moins de n_min voisins géocodés dans le rayon, retourne
# None pour forcer l'appelant (api/main.py) à retomber sur le proxy hédonique
# plutôt que de deviner un score sur un échantillon local trop faible.

def compute_spatial_score_geostat(
    prix_m2_local_pondere: Optional[float],
    prix_m2_median_zone: float,
    n_voisins: int,
    n_min: int = 5,
) -> Optional[float]:
    """Score spatial [0-100] à partir d'une moyenne locale pondérée réelle.

    ratio = prix_m2_local_pondere / prix_m2_median_zone
    score = 50 + 50*tanh(2*(ratio-1))  → borné [0,100], 50 = neutre (zone = local)

    Retourne None si les données sont insuffisantes (jamais un score deviné).
    """
    if prix_m2_local_pondere is None or n_voisins is None or n_voisins < n_min:
        return None
    if not prix_m2_median_zone or prix_m2_median_zone <= 0:
        return None
    ratio = prix_m2_local_pondere / prix_m2_median_zone
    score = 50.0 + 50.0 * math.tanh(2.0 * (ratio - 1.0))
    return round(max(0.0, min(100.0, score)), 1)


# ══ SCORE GEXSCORE [0-1000] ═══════════════════════════════════════════════════

def compute_gexscore(
    score_spatial: float,       # [0-100] Hédonique + SAR
    score_frontalier: float,    # [0-100] Proximité GVA + médical + bruit
    score_esg: float,           # [0-100] SFDR Art. 8
    regime_bull_prob: float,    # [0-1]   Probabilité régime expansion (HMM)
    zone_cfg: dict,
    qubo_quality: float = 0.75  # [0-1]   Tensor Networks quality (Phase 2)
) -> dict:
    """
    Score GexScore [0-1000] — Indicateur synthétique propriétaire.

    Pondérations calibrées walk-forward 2023-2026 sur DVF Gex.
    Stable en dehors du YAML de zone — ne pas toucher sans re-calibration.

    Analogie : FICO Score du crédit, Z-Score d'Altman pour le corporate.
    """
    w = zone_cfg["score_weights"]

    raw = (
        w["w_spatial"]    * score_spatial            +
        w["w_frontalier"] * score_frontalier         +
        w["w_esg"]        * score_esg                +
        w["w_regime"]     * regime_bull_prob * 100   +
        w["w_quantum"]    * qubo_quality * 100
    )

    score = round(min(1000.0, raw * 10), 1)

    # Notation alphabétique (analogie obligataire)
    if score >= 850:
        grade, action = "AAA", "BUY — bien exceptionnel"
    elif score >= 700:
        grade, action = "AA",  "BUY — négo < 5% max"
    elif score >= 550:
        grade, action = "A",   "WATCH — négo 8-10%"
    elif score >= 350:
        grade, action = "BB",  "CAUTION — audit requis"
    else:
        grade, action = "CCC", "AVOID — risques identifiés"

    return {
        "gexscore":   score,
        "grade":      grade,
        "action":     action,
        "composants": {
            "score_spatial":    round(score_spatial, 1),
            "score_frontalier": round(score_frontalier, 1),
            "score_esg":        round(score_esg, 1),
            "regime_bull_pct":  round(regime_bull_prob * 100, 1),
            # AJOUTÉ le 18/07/2026 : la 5e composante existait déjà dans le
            # calcul (w_quantum * qubo_quality*100 ci-dessus) mais n'était
            # jamais exposée dans la réponse API — l'UI ne pouvait donc
            # montrer que 4 des 5 scores réellement utilisés. qubo_quality
            # reste un placeholder fixe (Phase 2, pas un vrai calcul QUBO
            # aujourd'hui) — affiché comme tel, jamais présenté comme réel.
            "qubo_quality_pct": round(qubo_quality * 100, 1),
        }
    }


# ══ DEAL ALERT : signal <24h ══════════════════════════════════════════════════

def is_deal_alert(
    prix_annonce: float,
    avm_hedonique: float,
    gexscore: float,
    zone_cfg: dict
) -> dict:
    """
    Détecte si un bien est sous-évalué vs l'AVM.
    Signal "Deal Alert" envoyé aux agences abonnées en temps réel.
    C'est le produit B2B n°1 — une agence à 490 EUR/mois pour cet alerte seul.
    """
    threshold = zone_cfg["alerts"]["score_deal_threshold"]

    discount_pct = (avm_hedonique - prix_annonce) / avm_hedonique * 100

    is_deal = (
        gexscore >= threshold and
        discount_pct >= 5.0 and   # Au moins 5% sous l'AVM
        prix_annonce > 0
    )

    return {
        "is_deal_alert":    is_deal,
        "discount_pct":     round(discount_pct, 1),
        "prix_annonce_eur": round(prix_annonce),
        "avm_eur":          round(avm_hedonique),
        "potentiel_nego_eur": round(avm_hedonique - prix_annonce) if is_deal else 0
    }
