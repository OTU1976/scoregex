"""
GexScore — api/main.py
═══════════════════════════════════════════════════════════════════════
FastAPI B2B : moteur complet branché.

Endpoints :
  POST /estimate     → GexScore + AVM Hédonique + Merton + Frontalier + ESG*
  GET  /health        → Santé de l'API + vérification chargement moteur/config
  GET  /prix-marche    → Prix DVF réels par commune
  GET  /                → Info plateforme

* IMPORTANT — Honnêteté des données (principe "pas de donnée fictive") :
  - Frontalier (temps Genève, désert médical, bruit) : DONNÉES RÉELLES,
    calculées en direct via OSRM public + OSM Overpass à chaque requête.
  - AVM Hédonique : DONNÉES RÉELLES — prix DVF calibrés en direct via Supabase
    (747 transactions Appartement réelles 2014-2025, mises à jour à chaque
    vague DGFiP) + coefficients hédoniques calibrés zone.
  - Merton Jump-Diffusion : modèle stochastique RÉEL, calibré sur les
    paramètres de zone (mu, sigma, sauts).
  - ESG : SEULEMENT la composante DPE est réelle. Inondation, argile, NDVI,
    mixité sociale, vacance logement, croissance démographique n'ont PAS
    encore de source de données branchée (Georisques / INSEE Filosofi /
    NDVI Planet Labs — roadmap Section VII, non construit à ce stade).
    Des valeurs neutres de zone sont utilisées à la place et EXPLICITEMENT
    signalées dans `data_quality_notes` de chaque réponse. Ne jamais
    présenter ce sous-score comme une mesure réelle tant que ces pipelines
    ne sont pas branchés.
  - Score spatial (SAR/GWR) : le vrai modèle spatial n'est pas construit.
    Un proxy dérivé de l'ajustement hédonique total est utilisé à la place
    (calcul réel, mais ce n'est pas le modèle SAR/GWR complet prévu au
    roadmap). Signalé dans `data_quality_notes`.
  - Régime HMM (bull/bear) : la détection dynamique de régime n'est pas
    construite. La probabilité stationnaire calibrée du régime "expansion"
    est utilisée comme proxy statique. Signalé dans `data_quality_notes`.

⚠️ Risque opérationnel connu : le score Frontalier effectue jusqu'à 4 appels
   réseau externes séquentiels (OSRM + 3x OSM Overpass). Sur l'offre Vercel
   Hobby (timeout 10s), ceci peut dépasser le budget. Un garde-fou de timeout
   (5s, ajustable) protège contre un 504 : au-delà, un score neutre documenté
   est utilisé (voir `data_quality_notes.frontalier`). Le calcul Merton (~1s)
   et le démarrage à froid Vercel consomment le reste du budget de 10s — à
   surveiller après déploiement et ajuster si besoin.

Auteur : Steelldy SAS — Juillet 2026
"""

import os
import sys
import json
import time
import math
import logging
import concurrent.futures
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml
import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# ── Imports moteurs GexScore ──────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

from engine.score_frontalier import compute_frontalier_score
from engine.score_gexscore import (
    compute_avm_hedonique,
    compute_esg_score,
    compute_avm_merton,
    compute_gexscore,
    is_deal_alert,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger("gexscore.api")

VERSION = "3.4.0"

app = FastAPI(
    title="GexScore API",
    description=(
        "API B2B d'évaluation immobilière quantitative — Pays de Gex.\n\n"
        "Moteur : AVM Hédonique + Merton Jump-Diffusion + Score Frontalier CHF/EUR.\n"
        "Steelldy SAS."
    ),
    version=VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "https://scoregex.com").split(","),
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ── Chemins ────────────────────────────────────────────────────────────────
CONFIG_DIR = BASE_DIR / "config" / "zones"
PRIX_JSON_PATH = BASE_DIR / "data" / "prix_gex.json"

# Supabase (lecture publique — clé anon, RLS restreint à SELECT sur `biens`)
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")
PRIX_CACHE_TTL_SECONDS = 3600  # 1h — évite de taper Supabase à chaque requête

# Filets de sécurité, dans l'ordre où ils sont essayés si Supabase échoue :
# 1) data/prix_gex.json (ancien pipeline, snapshot du 07/07/2026)
# 2) ce dict minimal, en tout dernier recours.
# Ce n'est JAMAIS la source de vérité — la vraie donnée vient de Supabase
# (table `biens`, alimentée par scripts/upload_dvf_to_supabase.py).
_PRIX_FALLBACK = {
    "01071": {"commune": "Cessy", "prix_m2_median": 4959},
    "01160": {"commune": "Ferney-Voltaire", "prix_m2_median": 4951},
    "01173": {"commune": "Gex", "prix_m2_median": 4674},
    "01313": {"commune": "Prevessin-Moens", "prix_m2_median": 5543},
    "01281": {"commune": "Ornex", "prix_m2_median": 5269},
    "01354": {"commune": "Saint-Genis-Pouilly", "prix_m2_median": 4875},
    "01401": {"commune": "Sergy", "prix_m2_median": 3480},
    "01419": {"commune": "Thoiry", "prix_m2_median": 4958},
}
PRIX_ZONE_DEFAULT = 4900

_prix_cache: dict = {"data": None, "loaded_at": 0.0, "source": None}


def _load_prix_from_supabase() -> Optional[dict]:
    """Interroge la vue v_prix_marche_appartements en direct sur Supabase.
    Retourne None si Supabase n'est pas configuré ou injoignable — le
    fallback local prend alors le relais (voir load_prix_dvf)."""
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        log.warning("SUPABASE_URL/SUPABASE_ANON_KEY non configurés — utilisation du fallback local")
        return None
    try:
        resp = requests.get(
            f"{SUPABASE_URL.rstrip('/')}/rest/v1/v_prix_marche_appartements",
            headers={"apikey": SUPABASE_ANON_KEY, "Authorization": f"Bearer {SUPABASE_ANON_KEY}"},
            params={"select": "code_commune,nom_commune,prix_m2_median,nb_transactions"},
            timeout=5,
        )
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            log.warning("Supabase a répondu mais la vue est vide (pipeline pas encore exécuté ?)")
            return None
        data = {
            str(r["code_commune"]): {
                "commune": r["nom_commune"],
                "prix_m2_median": r["prix_m2_median"],
                "nb_transactions": r["nb_transactions"],
            }
            for r in rows
        }
        log.info(f"Prix chargés depuis Supabase (live) : {len(data)} communes")
        return data
    except Exception as e:
        log.error(f"Échec requête Supabase ({e}) — repli sur fallback local")
        return None


_regime_cache: dict = {"prob": None, "loaded_at": 0.0, "source": None}


def _compute_regime_bull_prob() -> tuple:
    """Calcule regime_bull_prob à partir de la vraie tendance DVF (vue
    v_regime_marche : médiane 180j récents vs 180j précédents), au lieu
    du biais fixe 0.58 trouvé le 10/07/2026 (zone_cfg["hmm"][...], jamais
    mis à jour, gonflait chaque GexScore de ~70 points en permanence).

    Retourne (probabilité, source) :
      - "tendance_dvf_reelle" si le calcul a pu être fait (assez de données)
      - "neutre_fallback" (0.5) sinon — JAMAIS de valeur inventée présentée
        comme réelle.
    """
    now = time.time()
    if _regime_cache["prob"] is not None and (now - _regime_cache["loaded_at"]) < PRIX_CACHE_TTL_SECONDS:
        return _regime_cache["prob"], _regime_cache["source"]

    prob, source = 0.5, "neutre_fallback"

    if SUPABASE_URL and SUPABASE_ANON_KEY:
        try:
            resp = requests.get(
                f"{SUPABASE_URL.rstrip('/')}/rest/v1/v_regime_marche",
                headers={"apikey": SUPABASE_ANON_KEY, "Authorization": f"Bearer {SUPABASE_ANON_KEY}"},
                params={"select": "n_recent,median_recent,n_prior,median_prior"},
                timeout=5,
            )
            resp.raise_for_status()
            rows = resp.json()
            if rows:
                r = rows[0]
                n_recent, n_prior = r.get("n_recent", 0), r.get("n_prior", 0)
                med_recent, med_prior = r.get("median_recent"), r.get("median_prior")
                # Seuil minimal : sous 10 transactions par fenêtre, le calcul
                # n'est pas fiable statistiquement -> on reste neutre.
                if n_recent >= 10 and n_prior >= 10 and med_recent and med_prior:
                    growth = (med_recent - med_prior) / med_prior
                    k = 8.0  # calibration : +10% sur 6 mois -> ~0.69, -10% -> ~0.31
                    prob = 1.0 / (1.0 + math.exp(-k * growth))
                    source = "tendance_dvf_reelle"
                    log.info(f"Régime calculé depuis tendance DVF : croissance={growth*100:.1f}%, prob={prob:.3f}")
                else:
                    log.warning(f"Échantillon insuffisant pour le régime (n_recent={n_recent}, n_prior={n_prior}) — neutre")
        except Exception as e:
            log.error(f"Échec requête v_regime_marche ({e}) — repli neutre 0.5")

    _regime_cache["prob"] = prob
    _regime_cache["loaded_at"] = now
    _regime_cache["source"] = source
    return prob, source


def load_prix_dvf() -> dict:
    """Source de prix, dans l'ordre de préférence :
    1. Supabase (v_prix_marche_appartements) — donnée live, cache 1h
    2. data/prix_gex.json — snapshot local si Supabase indisponible
    3. _PRIX_FALLBACK — dernier recours minimal
    """
    now = time.time()
    if _prix_cache["data"] is not None and (now - _prix_cache["loaded_at"]) < PRIX_CACHE_TTL_SECONDS:
        return _prix_cache["data"]

    data = _load_prix_from_supabase()
    source = "supabase_live"

    if data is None:
        try:
            with open(PRIX_JSON_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            source = "json_snapshot_local"
            log.info(f"prix_gex.json chargé (repli) : {len(data)} communes")
        except Exception as e:
            log.error(f"Impossible de charger data/prix_gex.json ({e}) — fallback minimal utilisé")
            data = _PRIX_FALLBACK
            source = "fallback_minimal"

    _prix_cache["data"] = data
    _prix_cache["loaded_at"] = now
    _prix_cache["source"] = source
    return data


@lru_cache(maxsize=8)
def get_zone_config(zone_id: str) -> dict:
    if not CONFIG_DIR.exists():
        raise HTTPException(status_code=500, detail=f"Dossier config introuvable : {CONFIG_DIR}")
    for cfg_path in CONFIG_DIR.glob("*.yaml"):
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        if cfg.get("zone_id") == zone_id:
            return cfg
    raise HTTPException(status_code=404, detail=f"Zone '{zone_id}' non trouvée")


def get_prix_m2(commune: Optional[str]):
    prix_dvf = load_prix_dvf()
    if commune:
        for code, d in prix_dvf.items():
            if d["commune"].lower() in commune.lower():
                return d["prix_m2_median"], d["commune"]
    return PRIX_ZONE_DEFAULT, (commune or "Pays de Gex")


def _frontalier_with_timeout(lat: float, lon: float, zone_cfg: dict, budget_s: float = 5.0):
    """Exécute le score Frontalier (appels réels OSRM + OSM Overpass) avec un
    budget de temps maximal, pour éviter un timeout dur (504) côté Vercel.
    Retourne (résultat, a_timeout: bool)."""
    # Important : on N'UTILISE PAS `with ThreadPoolExecutor(...)` ici, car son
    # __exit__ attend (shutdown(wait=True)) que le thread interne se termine
    # avant de rendre la main — ce qui annulerait complètement l'effet du
    # timeout (on se retrouverait à attendre la durée totale de l'appel réseau
    # malgré tout). On gère l'executor manuellement avec shutdown(wait=False)
    # pour rendre la main au budget_s défini, quel que soit l'état du thread.
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = ex.submit(compute_frontalier_score, lat, lon, zone_cfg)
    try:
        result = future.result(timeout=budget_s)
        ex.shutdown(wait=False)
        return result, False
    except concurrent.futures.TimeoutError:
        ex.shutdown(wait=False)
        log.warning(f"compute_frontalier_score > {budget_s}s ({lat},{lon}) — fallback neutre appliqué")
        return {
            "score_frontalier": 50.0,
            "detail": {"score_gva": 50.0, "score_medical": 50.0, "score_bruit": 50.0, "score_services": 50.0},
            "raw": {"t_gva_min": None, "nb_medecins": None},
            "interpretation": "Score neutre — timeout API externe (OSRM/OSM), non représentatif du bien",
        }, True
    except Exception as e:
        ex.shutdown(wait=False)
        log.error(f"compute_frontalier_score erreur ({lat},{lon}) : {e}")
        return {
            "score_frontalier": 50.0,
            "detail": {"score_gva": 50.0, "score_medical": 50.0, "score_bruit": 50.0, "score_services": 50.0},
            "raw": {"t_gva_min": None, "nb_medecins": None},
            "interpretation": "Score neutre — erreur technique, non représentatif du bien",
        }, True


class EstimateRequest(BaseModel):
    lat: float
    lon: float
    surface_m2: float
    dpe_note: Optional[str] = "D"
    prix_annonce: Optional[float] = None
    zone_id: Optional[str] = "gex_001"
    commune: Optional[str] = None
    annee_construction: Optional[int] = None   # Si absent -> âge neutre (20 ans)
    vue_leman: Optional[bool] = False           # Déclaratif — pas de détection auto (viewshed non construit)
    ecole_intl_500m: Optional[bool] = False     # Déclaratif — pas de filtrage OSM par nom d'école


@app.get("/")
async def root():
    return {
        "status": "ok",
        "platform": "ScoreGex",
        "version": VERSION,
        "message": "Quantitative Real Estate Intelligence — Pays de Gex",
        "data_source": "DVF reel 2014-2025 — DGFiP Open Data",
        "engine": "AVM Hedonique + Merton Jump-Diffusion + Score Frontalier CHF/EUR",
    }


@app.get("/health")
async def health():
    """Vérifie le chargement effectif du moteur (config YAML + prix DVF).
    Utile pour diagnostiquer un problème de bundling Vercel sans passer
    par /estimate (qui ferait des appels réseau externes)."""
    checks = {}
    try:
        get_zone_config("gex_001")
        checks["zone_config"] = "ok"
    except Exception as e:
        checks["zone_config"] = f"ERREUR: {e}"
    try:
        prix = load_prix_dvf()
        checks["prix_dvf"] = f"ok ({len(prix)} communes, source={_prix_cache['source']})"
    except Exception as e:
        checks["prix_dvf"] = f"ERREUR: {e}"
    try:
        prob, source = _compute_regime_bull_prob()
        checks["regime_marche"] = f"ok (prob={prob:.3f}, source={source})"
    except Exception as e:
        checks["regime_marche"] = f"ERREUR: {e}"
    return {"status": "ok", "version": VERSION, "checks": checks}


@app.get("/prix-marche")
async def prix_marche():
    return {
        "status": "ok",
        "source": "DVF reel DGFiP 2014-2025",
        "communes": load_prix_dvf(),
    }


@app.post("/estimate")
async def estimate(req: EstimateRequest):
    zone_cfg = get_zone_config(req.zone_id)
    prix_m2_zone, commune_nom = get_prix_m2(req.commune)
    dpe = (req.dpe_note or "D").upper()
    age_bien = (2026 - req.annee_construction) if req.annee_construction else 20  # 20 = neutre (0% ajust.)

    # ── 1. Score Frontalier — RÉEL, OSRM + OSM Overpass en direct ──────────
    frontalier, frontalier_timed_out = _frontalier_with_timeout(req.lat, req.lon, zone_cfg)
    t_gva_min = frontalier["raw"]["t_gva_min"]
    nb_medecins = frontalier["raw"]["nb_medecins"] or 0
    desert_medical = (nb_medecins == 0)
    bruit_score = frontalier["detail"]["score_bruit"]

    # ── 2. AVM Hédonique — RÉEL, prix DVF + coefficients calibrés ──────────
    avm = compute_avm_hedonique(
        prix_m2_median_zone=prix_m2_zone,
        surface_m2=req.surface_m2,
        dpe_note=dpe,
        t_gva_min=t_gva_min,
        bruit_score=bruit_score,
        vue_leman=req.vue_leman,
        ecole_intl_500m=req.ecole_intl_500m,
        desert_medical=desert_medical,
        age_bien=age_bien,
        zone_cfg=zone_cfg,
    )

    # ── 3. ESG — PARTIEL. Seule la composante DPE est une donnée réelle. ───
    #    Le reste (inondation, argile, NDVI, mixité, vacance, démographie)
    #    utilise des défauts neutres de zone tant que Georisques/INSEE/NDVI
    #    ne sont pas branchés (roadmap Section VII — non construit).
    esg = compute_esg_score(
        dpe_note=dpe,
        inondation_risk=0.10,
        argile_risk=0.10,
        ndvi=0.40,
        nb_medecins=nb_medecins,
        mixite_sociale_score=50.0,
        vacance_logement_pct=5.0,
        pop_growth_5y_pct=1.0,
        zone_cfg=zone_cfg,
    )

    # ── 4. Merton Jump-Diffusion — RÉEL, modèle stochastique calibré ───────
    merton = compute_avm_merton(prix_central=avm["prix_estime_eur"], zone_cfg=zone_cfg)

    # ── 5. Score spatial — PROXY dérivé de l'ajustement hédonique réel.
    #    Ce n'est PAS le modèle SAR/GWR complet prévu au roadmap (non construit).
    score_spatial = max(0.0, min(100.0, 50.0 + avm["total_ajustement_pct"]))

    # ── 6. Régime marché — RÉEL, tendance DVF 180j vs 180j précédents
    #    (remplace l'ancien biais fixe 0.58 trouvé le 10/07/2026).
    regime_bull_prob, regime_source = _compute_regime_bull_prob()

    # ── 7. Score composite [0-1000] ────────────────────────────────────────
    gexscore = compute_gexscore(
        score_spatial=score_spatial,
        score_frontalier=frontalier["score_frontalier"],
        score_esg=esg["esg_score"],
        regime_bull_prob=regime_bull_prob,
        zone_cfg=zone_cfg,
    )

    # ── 8. Deal Alert ───────────────────────────────────────────────────────
    deal = None
    if req.prix_annonce and req.prix_annonce > 0:
        deal_raw = is_deal_alert(
            prix_annonce=req.prix_annonce,
            avm_hedonique=avm["prix_estime_eur"],
            gexscore=gexscore["gexscore"],
            zone_cfg=zone_cfg,
        )
        eco = deal_raw["potentiel_nego_eur"] if deal_raw["is_deal_alert"] else round(avm["prix_estime_eur"] - req.prix_annonce)
        deal = {
            "is_deal": deal_raw["is_deal_alert"],
            "discount_pct": deal_raw["discount_pct"],
            "economie_potentielle_eur": eco,
        }

    log.info(
        f"[ESTIMATE] {commune_nom} {req.surface_m2}m2 DPE={dpe} -> "
        f"{avm['prix_estime_eur']}EUR score={gexscore['gexscore']} "
        f"(frontalier_timeout={frontalier_timed_out})"
    )

    return JSONResponse({
        "status": "ok",
        "version": VERSION,
        "zone_id": req.zone_id,
        "commune": commune_nom,
        "data_source": "DVF reel DGFiP 2014-2025",
        "gexscore": {
            "score": gexscore["gexscore"],
            "grade": gexscore["grade"],
            "action": gexscore["action"],
            "composants": gexscore["composants"],
        },
        "avm": {
            "prix_estime_eur": avm["prix_estime_eur"],
            "prix_m2_estime": avm["prix_m2_estime"],
            "prix_m2_zone_median_dvf": prix_m2_zone,
            "ajustement_dpe_pct": avm["ajustements"]["dpe"],
            "ajustements_detail_pct": avm["ajustements"],
            "surface_m2": req.surface_m2,
        },
        "merton": merton,
        "frontalier": frontalier,
        "esg": {
            "esg_score": esg["esg_score"],
            "esg_grade": esg["esg_grade"],
        },
        "deal_alert": deal,
        "data_quality_notes": {
            "frontalier": "timeout_fallback_neutre" if frontalier_timed_out else "temps_reel_osrm_osm_overpass",
            "esg": "partiel_dpe_reel_reste_defaut_zone_neutre_georisques_insee_ndvi_non_brancres",
            "score_spatial": "proxy_derive_avm_hedonique_pas_sar_gwr_complet",
            "regime_marche": regime_source,
        },
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)