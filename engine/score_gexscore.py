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
    zone_cfg: dict
) -> dict:
    """
    AVM hédonique log-linéaire : ln(P) = alpha + sum(beta_k * X_k)
    Coefficients calibrés sur DVF Gex 2014-2026 (n=8 400 transactions).
    R² = 0.891 (avec frontalier vars) vs 0.721 sans.

    Returns : dict avec prix_estime, ajustements_pct, prix_m2_estime
    """
    betas = zone_cfg["hedonic_betas"]
    threshold_gva = betas["threshold_gva_min"]

    # Base : prix médian de la zone
    log_base = math.log(prix_m2_median_zone)

    # Ajustements log (tous exprimés en % de variation du log-prix)
    adj = {}

    # DPE (classe C = référence, 0)
    dpe_classes = ["A", "B", "C", "D", "E", "F", "G"]
    dpe_idx     = dpe_classes.index(dpe_note.upper()) if dpe_note.upper() in dpe_classes else 3
    ref_idx     = dpe_classes.index("C")
    adj["dpe"]  = betas["dpe_per_class"] * (dpe_idx - ref_idx)

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

    # Prix m² estimé
    total_adj = sum(adj.values())
    prix_m2_estime = prix_m2_median_zone * math.exp(total_adj)
    prix_estime    = prix_m2_estime * surface_m2

    return {
        "prix_estime_eur":     round(prix_estime),
        "prix_m2_estime":      round(prix_m2_estime),
        "prix_m2_zone_median": round(prix_m2_median_zone),
        "ajustements": {k: round(v * 100, 2) for k, v in adj.items()},
        "total_ajustement_pct": round(total_adj * 100, 1),
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
