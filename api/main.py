import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
import os

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

app = FastAPI(
    title="ScoreGex API",
    description="Quantitative Real Estate Intelligence — Pays de Gex",
    version="3.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "https://scoregex.com").split(","),
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

class EstimateRequest(BaseModel):
    lat: float
    lon: float
    surface_m2: float
    dpe_note: Optional[str] = "D"
    prix_annonce: Optional[float] = None
    zone_id: Optional[str] = "gex_001"

@app.get("/")
async def root():
    return {"status": "ok", "platform": "ScoreGex", "version": "3.0.0", "message": "Quantitative Real Estate Intelligence — Pays de Gex"}

@app.get("/health")
async def health():
    return {"status": "ok", "version": "3.0.0"}

@app.post("/estimate")
async def estimate(req: EstimateRequest):
    prix_m2_median = 3850.0
    prix_estime = prix_m2_median * req.surface_m2
    gexscore = 720.0
    deal = None
    if req.prix_annonce:
        discount = (prix_estime - req.prix_annonce) / prix_estime * 100
        deal = {"is_deal": discount > 5, "discount_pct": round(discount, 1)}
    return JSONResponse({
        "status": "ok",
        "zone_id": req.zone_id,
        "gexscore": {"score": gexscore, "grade": "AA", "action": "BUY — nego < 5%"},
        "avm": {"prix_estime_eur": round(prix_estime), "prix_m2_estime": prix_m2_median},
        "deal_alert": deal
    })
