import logging
import json
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger("scoregex.api")

app = FastAPI(
    title="ScoreGex API",
    description="Quantitative Real Estate Intelligence — Pays de Gex",
    version="3.1.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "https://scoregex.com").split(","),
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# Chargement des vrais prix DVF calibres
PRIX_DVF = {
    "01071": {"commune": "Cessy",             "prix_m2_median": 4959},
    "01160": {"commune": "Ferney-Voltaire",   "prix_m2_median": 4951},
    "01173": {"commune": "Gex",               "prix_m2_median": 4674},
    "01313": {"commune": "Prevessin-Moens",   "prix_m2_median": 5543},
    "01281": {"commune": "Ornex",             "prix_m2_median": 5269},
    "01354": {"commune": "Saint-Genis-Pouilly","prix_m2_median": 4875},
    "01401": {"commune": "Sergy",             "prix_m2_median": 3480},
    "01419": {"commune": "Thoiry",            "prix_m2_median": 4958},
}
PRIX_ZONE_DEFAULT = 4900  # Median zone Gex calibre DVF reel

COMMUNES_GEX = {v["commune"]: k for k, v in PRIX_DVF.items()}

class EstimateRequest(BaseModel):
    lat: float
    lon: float
    surface_m2: float
    dpe_note: Optional[str] = "D"
    prix_annonce: Optional[float] = None
    zone_id: Optional[str] = "gex_001"
    commune: Optional[str] = None

def get_prix_m2(commune: Optional[str]) -> int:
    if commune:
        for code, data in PRIX_DVF.items():
            if data["commune"].lower() in commune.lower():
                return data["prix_m2_median"]
    return PRIX_ZONE_DEFAULT

def compute_dpe_ajustement(dpe: str) -> float:
    ajustements = {"A": 0.08, "B": 0.04, "C": 0.0, "D": -0.05,
                   "E": -0.12, "F": -0.20, "G": -0.28}
    return ajustements.get(dpe.upper(), -0.05)

def compute_gexscore(prix_m2_estime: float, prix_m2_zone: float, dpe: str) -> dict:
    ratio = prix_m2_estime / prix_m2_zone
    dpe_scores = {"A": 100, "B": 85, "C": 70, "D": 55, "E": 40, "F": 20, "G": 5}
    dpe_score = dpe_scores.get(dpe.upper(), 55)
    score_raw = (ratio * 600) + (dpe_score * 4)
    score = round(min(1000, max(0, score_raw)), 0)
    if score >= 850: grade, action = "AAA", "BUY — bien exceptionnel"
    elif score >= 700: grade, action = "AA", "BUY — nego < 5%"
    elif score >= 550: grade, action = "A", "WATCH — nego 8-10%"
    elif score >= 350: grade, action = "BB", "CAUTION — audit requis"
    else: grade, action = "CCC", "AVOID"
    return {"score": score, "grade": grade, "action": action}

@app.get("/")
async def root():
    return {
        "status": "ok",
        "platform": "ScoreGex",
        "version": "3.1.0",
        "message": "Quantitative Real Estate Intelligence — Pays de Gex",
        "data_source": "DVF reel 2014-2025 — DGFiP Open Data",
        "nb_transactions_calibration": 658
    }

@app.get("/health")
async def health():
    return {"status": "ok", "version": "3.1.0"}

@app.get("/prix-marche")
async def prix_marche():
    return {
        "status": "ok",
        "source": "DVF reel DGFiP 2014-2025",
        "communes": PRIX_DVF
    }

@app.post("/estimate")
async def estimate(req: EstimateRequest):
    prix_m2_zone = get_prix_m2(req.commune)
    commune_nom = req.commune or "Pays de Gex"
    dpe = req.dpe_note or "D"
    adj_dpe = compute_dpe_ajustement(dpe)
    prix_m2_estime = round(prix_m2_zone * (1 + adj_dpe))
    prix_estime = round(prix_m2_estime * req.surface_m2)
    gexscore = compute_gexscore(prix_m2_estime, prix_m2_zone, dpe)
    deal = None
    if req.prix_annonce and req.prix_annonce > 0:
        discount = (prix_estime - req.prix_annonce) / prix_estime * 100
        deal = {
            "is_deal": discount > 5,
            "discount_pct": round(discount, 1),
            "economie_potentielle_eur": round(prix_estime - req.prix_annonce)
        }
    log.info(f"[ESTIMATE] {commune_nom} {req.surface_m2}m2 DPE={dpe} -> {prix_estime}EUR score={gexscore['score']}")
    return JSONResponse({
        "status": "ok",
        "version": "3.1.0",
        "zone_id": req.zone_id,
        "commune": commune_nom,
        "data_source": "DVF reel DGFiP 2014-2025",
        "gexscore": gexscore,
        "avm": {
            "prix_estime_eur": prix_estime,
            "prix_m2_estime": prix_m2_estime,
            "prix_m2_zone_median_dvf": prix_m2_zone,
            "ajustement_dpe_pct": round(adj_dpe * 100, 1),
            "surface_m2": req.surface_m2
        },
        "deal_alert": deal
    })
