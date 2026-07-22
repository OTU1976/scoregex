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
  - Type de bien (16/07/2026 — bug critique corrigé, cf. cas réel 522 Rue de
    Rogeland, Gex) : jusqu'ici /estimate utilisait TOUJOURS le prix médian
    Appartement, même pour une maison (une maison à Gex était donc évaluée
    avec un prix/m2 d'appartement — mécaniquement faux). Le champ
    `type_bien` route maintenant vers `v_prix_marche_appartements` ou
    `v_prix_marche_maisons` (265 transactions Maison réelles 2025, dont 198
    avec surface_terrain connue — échantillon plus petit et plus récent que
    les appartements, signalé honnêtement via `nb_transactions_dvf` et
    `data_quality_notes.type_bien_maturite`).
  - Surface du terrain (17/07/2026 — FEU VERT explicite d'Helen) :
    surface_terrain_m2 est maintenant intégré comme un VRAI ajustement de
    prix (Maison uniquement), via un coefficient RÉELLEMENT calibré par
    régression sur les 196/198 transactions Maison réelles avec terrain
    connu (effets fixes commune + contrôle taille du bien ; méthodologie
    complète dans config/zones/001_pays_de_gex.yaml, clé
    hedonic_betas.terrain_surface_beta = 0.152, t=4.84, n=196). La
    référence de comparaison est la surface_terrain médiane RÉELLE de la
    commune (vue Supabase v_prix_marche_maisons.surface_terrain_mediane),
    repli sur une médiane poolée réelle (792.5 m²) si indisponible.
    ⚠️ Cet ajustement passe par le MÊME plafond ±35% que les autres (voir
    point suivant) — si les autres pénalités l'ont déjà saturé, l'effet
    visible sur le prix final peut être nul, et c'est signalé tel quel.
  - Garde-fou d'ajustement hédonique (16/07/2026) : les pénalités
    Frontalier (bruit, distance Genève, désert médical) pouvaient se
    cumuler en exp() sans limite et faire chuter un prix de plus de 50%
    (observé : -56.6% sur le cas réel ci-dessus). Un plafond ±35% est
    maintenant appliqué (`ajustement_plafonne` signalé si actif) — ce n'est
    pas un coefficient de marché calibré, c'est un garde-fou d'ingénierie
    contre un artefact de compounding log-linéaire.
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
  - Fraîcheur des données DVF : la date de la dernière transaction connue
    (`derniere_transaction`, exposée par la vue Supabase
    `v_prix_marche_appartements`) est renvoyée telle quelle. Si Supabase
    n'est pas la source active (repli JSON/fallback) ou si le champ est
    absent de la source active, `donnees_dvf_a_jour_au` est `null` — ne
    JAMAIS inventer une date de fraîcheur.

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
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml
import requests
import sentry_sdk
from fastapi import FastAPI, HTTPException, Header
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
    compute_spatial_score_geostat,
    valider_dpe_step_log_monotone,
)
# AJOUTÉ le 22/07/2026 — signal secondaire XGBoost (feu vert Helen, voir
# engine/xgboost_divergence.py pour la méthodologie complète et les
# limites honnêtes). Import isolé + jamais fatal : si xgboost ou le
# fichier modèle sont absents, get_xgboost_status()/compute_divergence_xgboost()
# retournent disponible=False plutôt que de faire planter l'API — voir
# _load_once() dans ce module.
from engine.xgboost_divergence import (
    compute_divergence_xgboost,
    get_status as get_xgboost_status,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger("gexscore.api")

VERSION = "3.15.0"

# ── Sentry SDK — AJOUTÉ le 22/07/2026 (Helen a créé le compte et fourni le
# DSN via steelldy.sentry.io). Initialisé AVANT la création de l'app FastAPI
# (requis par le SDK pour instrumenter correctement les routes).
#
# ÉCART VOLONTAIRE par rapport au snippet quickstart Sentry (celui affiché
# sur la page "Getting Started" copiée par Helen) : le quickstart met
# `send_default_pii=True`, ce qui envoie par défaut les headers de requête
# (dont `Authorization`/`Cookie`), l'IP, et le corps de requête à Sentry.
# Or nos endpoints /estimations/* reçoivent un JWT Supabase réel dans le
# header Authorization (voir _supabase_headers_for_user ci-dessous) — l'envoyer
# tel quel à un sous-traitant tiers (même RGPD-compliant, hébergement `.de.`
# choisi par Helen = UE, bon réflexe) serait une fuite de secret
# d'authentification dans les rapports d'erreur, pas juste un souci de
# confidentialité. Donc : send_default_pii=False, ET un hook before_send qui
# retire explicitement Authorization/Cookie/apikey de tout évènement envoyé,
# en garde-fou supplémentaire même si send_default_pii venait à être
# réactivé par erreur plus tard.
SENTRY_DSN = os.getenv("SENTRY_DSN")


def _sentry_before_send(event: dict, hint: dict) -> Optional[dict]:
    """Retire les secrets d'authentification (JWT Supabase, cookies, clés
    API) de tout évènement avant envoi à Sentry — jamais de credential dans
    un rapport d'erreur, même si send_default_pii est actif."""
    request = event.get("request")
    if request and isinstance(request.get("headers"), dict):
        for h in list(request["headers"].keys()):
            if h.lower() in ("authorization", "cookie", "apikey", "x-api-key"):
                request["headers"][h] = "[retire_avant_envoi_sentry]"
    return event


if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        release=f"scoregex-api@{VERSION}",
        environment=os.getenv("VERCEL_ENV", "production"),
        send_default_pii=False,   # RGPD + anti-fuite JWT (voir commentaire ci-dessus)
        before_send=_sentry_before_send,
        traces_sample_rate=0.1,   # 10% des transactions (perf monitoring) — évite le volume/coût plein
    )
    log.info("Sentry SDK initialise (DSN configure, PII desactivee, before_send actif)")
else:
    log.warning("SENTRY_DSN non configure — erreurs de production NON remontees a Sentry")


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Remplace l'ancien `@app.on_event("startup")` (déprécié par FastAPI,
    voir DeprecationWarning constatée lors des tests le 22/07/2026 — corrigé
    dans la foulée plutôt que laissé trainer). Envoie un message info (PAS
    une erreur) à Sentry à chaque démarrage à froid, pour vérifier en direct
    que le SDK transmet bien — visible dans l'onglet Issues de Sentry,
    niveau "info", jamais compté comme une vraie erreur de production."""
    if SENTRY_DSN:
        sentry_sdk.capture_message(
            f"ScoreGex API v{VERSION} démarrée — SDK Sentry vérifié en direct.",
            level="info",
        )
    yield


app = FastAPI(
    title="GexScore API",
    description=(
        "API B2B d'évaluation immobilière quantitative — Pays de Gex.\n\n"
        "Moteur : AVM Hédonique + Merton Jump-Diffusion + Score Frontalier CHF/EUR.\n"
        "Steelldy SAS."
    ),
    version=VERSION,
    lifespan=_lifespan,
    # DESACTIVE le 16/07/2026 (decision Helen) : /docs et /redoc exposaient
    # une console interactive publique ("Try it out") permettant a n'importe
    # qui d'appeler POST /estimate -- le vrai moteur GexScore/AVM -- gratuit-
    # ement et sans limite, en contournant totalement l'app Streamlit et les
    # futurs tarifs B2B. openapi_url desactive aussi : sinon le schema JSON
    # complet (tous les endpoints + schemas de donnees) restait recuperable
    # directement meme sans l'interface Swagger/ReDoc. A reactiver le jour ou
    # un vrai systeme de cles API B2B (avec quota/facturation) sera construit.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
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
# NB : ces deux filets n'ont pas de `derniere_transaction` fiable -> le champ
# est absent (`.get()` renvoie None), et `donnees_dvf_a_jour_au` reste `null`
# dans la réponse plutôt que d'afficher une date non vérifiée.
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
    fallback local prend alors le relais (voir load_prix_dvf).

    Inclut `derniere_transaction` (date de la transaction DVF la plus
    récente connue pour la commune) — champ ajouté au select le 10/07/2026
    pour exposer l'indicateur de fraîcheur des données côté /estimate."""
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        log.warning("SUPABASE_URL/SUPABASE_ANON_KEY non configurés — utilisation du fallback local")
        return None
    try:
        resp = requests.get(
            f"{SUPABASE_URL.rstrip('/')}/rest/v1/v_prix_marche_appartements",
            headers={"apikey": SUPABASE_ANON_KEY, "Authorization": f"Bearer {SUPABASE_ANON_KEY}"},
            params={"select": "code_commune,nom_commune,prix_m2_median,nb_transactions,derniere_transaction"},
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
                "derniere_transaction": r.get("derniere_transaction"),
            }
            for r in rows
        }
        log.info(f"Prix chargés depuis Supabase (live) : {len(data)} communes")
        return data
    except Exception as e:
        log.error(f"Échec requête Supabase ({e}) — repli sur fallback local")
        return None


GEORISQUES_BASE = "https://www.georisques.gouv.fr/api/v1"


def _georisques_scores(lat: Optional[float], lon: Optional[float], rayon_m: int = 1000) -> dict:
    """ESG RÉEL (inondation + argile) — ajouté le 21/07/2026 (point 5/9 - feu
    vert Helen). API Géorisques (BRGM/MTE), publique, gratuite, sans clé.
    Vérifiée en direct le 21/07/2026 :
      - argile : /api/v1/rga?latlon=lon,lat -> {"codeExposition": "1|2|3",
        "exposition": "Exposition faible|moyenne|forte"} (classification BRGM
        standard retrait-gonflement des argiles a1/a2/a3).
      - inondation : /api/v1/gaspar/azi?latlon=lon,lat -> liste des Atlas de
        Zones Inondables (AZI) recensés dans le rayon. Absence de donnée
        officielle de risque gradué "national" au-delà des atlas -> le
        nombre de zones AZI dans le rayon sert de proxy réel (pas un
        placeholder), honnêtement documenté comme tel.

    Retourne les défauts neutres existants (0.10/0.10) si l'API est
    injoignable ou si les champs attendus sont absents — jamais un score
    deviné, l'ESG reste dégradé proprement plutôt que de planter."""
    result = {"inondation_risk": 0.10, "argile_risk": 0.10, "source": "defaut_neutre_georisques_indisponible"}
    if lat is None or lon is None:
        return result
    latlon = f"{lon},{lat}"
    try:
        r_argile = requests.get(
            f"{GEORISQUES_BASE}/rga", params={"latlon": latlon, "rayon": rayon_m}, timeout=4,
        )
        r_argile.raise_for_status()
        code = r_argile.json().get("codeExposition")
        if code is not None:
            result["argile_risk"] = max(0.0, min(1.0, (int(code) - 1) / 2.0))
    except Exception as e:
        log.warning(f"Géorisques /rga indisponible ({e}) — argile_risk reste au défaut neutre")

    try:
        r_azi = requests.get(
            f"{GEORISQUES_BASE}/gaspar/azi", params={"latlon": latlon, "rayon": rayon_m}, timeout=4,
        )
        r_azi.raise_for_status()
        n_zones = r_azi.json().get("results", 0)
        result["inondation_risk"] = max(0.0, min(1.0, n_zones / 2.0))
    except Exception as e:
        log.warning(f"Géorisques /gaspar/azi indisponible ({e}) — inondation_risk reste au défaut neutre")

    result["source"] = "reel_georisques_brgm"
    return result


def _spatial_local_estimate(lat: Optional[float], lon: Optional[float]) -> Optional[dict]:
    """Score spatial RÉEL — ajouté le 18/07/2026 (point 5/9 demandé par Helen).

    Appelle la fonction SQL/PostGIS spatial_local_estimate() (déployée le
    18/07/2026 sur Supabase) via RPC PostgREST : moyenne locale du prix/m2
    pondérée par un noyau gaussien de distance sur les transactions DVF
    géocodées (colonne biens.geom, peuplée le 18/07/2026 — elle était vide
    à 100% avant ce correctif malgré PostGIS installé).

    Retourne None si Supabase indisponible ou pas assez de voisins dans le
    rayon — l'appelant (endpoint /estimate) doit alors retomber honnêtement
    sur le proxy hédonique existant, jamais deviner une valeur."""
    if not SUPABASE_URL or not SUPABASE_ANON_KEY or lat is None or lon is None:
        return None
    try:
        resp = requests.post(
            f"{SUPABASE_URL.rstrip('/')}/rest/v1/rpc/spatial_local_estimate",
            headers={
                "apikey": SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
                "Content-Type": "application/json",
            },
            json={"p_lon": lon, "p_lat": lat},
            timeout=4,
        )
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            return None
        return rows[0] if isinstance(rows, list) else rows
    except Exception as e:
        log.warning(f"spatial_local_estimate RPC indisponible ({e}) — repli sur proxy hédonique")
        return None


_prix_cache_maisons: dict = {"data": None, "loaded_at": 0.0, "source": None}


def _load_prix_maisons_from_supabase() -> Optional[dict]:
    """Équivalent de _load_prix_from_supabase() mais pour v_prix_marche_maisons
    (ajoutée le 16/07/2026). Retourne None si Supabase n'est pas configuré,
    injoignable, ou si la vue est vide — AUCUN repli JSON n'existe pour les
    maisons (pas d'ancien snapshot process_dvf.py côté maisons), donc
    l'appelant doit traiter None honnêtement plutôt que de retomber sur les
    prix Appartement (ce qui reproduirait exactement le bug corrigé ici)."""
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        log.warning("SUPABASE_URL/SUPABASE_ANON_KEY non configurés — prix Maison indisponibles")
        return None
    try:
        resp = requests.get(
            f"{SUPABASE_URL.rstrip('/')}/rest/v1/v_prix_marche_maisons",
            headers={"apikey": SUPABASE_ANON_KEY, "Authorization": f"Bearer {SUPABASE_ANON_KEY}"},
            params={"select": "code_commune,nom_commune,prix_m2_median,nb_transactions,surface_terrain_mediane,derniere_transaction"},
            timeout=5,
        )
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            log.warning("Supabase a répondu mais v_prix_marche_maisons est vide")
            return None
        data = {
            str(r["code_commune"]): {
                "commune": r["nom_commune"],
                "prix_m2_median": r["prix_m2_median"],
                "nb_transactions": r["nb_transactions"],
                "surface_terrain_mediane": r.get("surface_terrain_mediane"),
                "derniere_transaction": r.get("derniere_transaction"),
            }
            for r in rows
        }
        log.info(f"Prix Maison chargés depuis Supabase (live) : {len(data)} communes")
        return data
    except Exception as e:
        log.error(f"Échec requête Supabase v_prix_marche_maisons ({e})")
        return None


def load_prix_dvf_maisons() -> Optional[dict]:
    """Source de prix Maison — Supabase uniquement (pas de fallback JSON,
    voir docstring de _load_prix_maisons_from_supabase). Retourne None si
    indisponible : l'appelant doit alors le signaler explicitement, jamais
    utiliser silencieusement les prix Appartement à la place."""
    now = time.time()
    if _prix_cache_maisons["data"] is not None and (now - _prix_cache_maisons["loaded_at"]) < PRIX_CACHE_TTL_SECONDS:
        return _prix_cache_maisons["data"]

    data = _load_prix_maisons_from_supabase()
    _prix_cache_maisons["data"] = data
    _prix_cache_maisons["loaded_at"] = now
    _prix_cache_maisons["source"] = "supabase_live" if data else "indisponible"
    return data


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
            # GARDE-FOU ajouté le 21/07/2026 (chantier recalibrage DPE) :
            # valide au CHARGEMENT (une fois par zone, grâce au lru_cache
            # ci-dessus — pas à chaque requête) que la courbe DPE partagée
            # ET chaque courbe type-spécifique éventuelle (maison/
            # appartement) sont bien monotones. Échoue vite et fort plutôt
            # que de servir silencieusement un score économiquement
            # incohérent (ex. un G mieux valorisé qu'un F). Voir
            # engine/score_gexscore.py::valider_dpe_step_log_monotone pour
            # l'historique complet de ce garde-fou (un vrai bug de ce type
            # a été détecté et bloqué pendant ce chantier, avant tout
            # déploiement).
            betas = cfg.get("hedonic_betas", {})
            if "dpe_step_log" in betas:
                valider_dpe_step_log_monotone(betas["dpe_step_log"], label=f"{zone_id}.dpe_step_log")
            for type_key, step_log in (betas.get("dpe_step_log_by_type") or {}).items():
                valider_dpe_step_log_monotone(step_log, label=f"{zone_id}.dpe_step_log_by_type.{type_key}")
            return cfg
    raise HTTPException(status_code=404, detail=f"Zone '{zone_id}' non trouvée")


def get_prix_m2(commune: Optional[str], type_bien: str = "appartement"):
    """Retourne (prix_m2_median, nom_commune, derniere_transaction,
    nb_transactions, source_maison_indisponible, surface_terrain_mediane).

    `type_bien` == "maison" route vers v_prix_marche_maisons (16/07/2026 —
    corrige le bug où une maison était systématiquement évaluée avec le
    prix médian Appartement, ex. 522 Rue de Rogeland/Gex : 4179 EUR/m2
    Appartement utilisé au lieu de ~5381 EUR/m2 Maison réel). Si les
    données Maison sont indisponibles, on NE RETOMBE PAS silencieusement
    sur les prix Appartement (ce serait reproduire le bug) : on retourne le
    défaut de zone et `source_maison_indisponible=True`, à signaler
    explicitement dans data_quality_notes.

    `derniere_transaction`/`nb_transactions` sont None/0 si la source
    active ne les expose pas — jamais de valeur inventée.

    `surface_terrain_mediane` (ajouté le 17/07/2026, pour l'ajustement
    foncier — voir compute_avm_hedonique) : médiane RÉELLE de la commune,
    exposée par la vue Supabase v_prix_marche_maisons. None pour un
    appartement, ou si la donnée Maison est indisponible pour cette
    commune — l'appelant doit alors utiliser le repli de zone
    (hedonic_betas.terrain_reference_defaut_m2), jamais une valeur
    inventée à la volée."""
    is_maison = (type_bien or "appartement").lower() == "maison"

    if is_maison:
        prix_dvf = load_prix_dvf_maisons()
        if prix_dvf is None:
            return PRIX_ZONE_DEFAULT, (commune or "Pays de Gex"), None, 0, True, None
        if commune:
            for code, d in prix_dvf.items():
                if d["commune"].lower() in commune.lower():
                    terrain_med = d.get("surface_terrain_mediane")
                    terrain_med = float(terrain_med) if terrain_med not in (None, "") else None
                    return d["prix_m2_median"], d["commune"], d.get("derniere_transaction"), d.get("nb_transactions", 0), False, terrain_med
        return PRIX_ZONE_DEFAULT, (commune or "Pays de Gex"), None, 0, True, None

    prix_dvf = load_prix_dvf()
    if commune:
        for code, d in prix_dvf.items():
            if d["commune"].lower() in commune.lower():
                return d["prix_m2_median"], d["commune"], d.get("derniere_transaction"), d.get("nb_transactions", 0), False, None
    return PRIX_ZONE_DEFAULT, (commune or "Pays de Gex"), None, 0, False, None


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
    # Ajoutés le 16/07/2026 (fix bug critique maison évaluée en appartement) :
    type_bien: Optional[str] = "appartement"    # "appartement" | "maison" — route vers le bon jeu DVF
    # Ajusté le 17/07/2026 (FEU VERT Helen) : intégré comme vrai ajustement
    # de prix pour type_bien="maison" (coefficient régressé, voir YAML +
    # compute_avm_hedonique). Ignoré si type_bien="appartement".
    surface_terrain_m2: Optional[float] = None


class EstimationSaveRequest(BaseModel):
    """Payload du Dashboard (bouton 'Enregistrer au dashboard').

    Aucun champ d'identité ici — l'utilisateur est identifié par son JWT
    Supabase Auth (header Authorization), vérifié par Supabase lui-même
    (PostgREST) et appliqué via RLS (auth.uid() = user_id) côté base de
    données. Voir db/004_estimations_sauvegardees.sql. La colonne user_id
    est remplie automatiquement par la base (DEFAULT auth.uid()) — l'API ne
    la définit jamais elle-même, pour qu'aucun code applicatif ne puisse
    usurper l'identité d'un autre utilisateur."""
    commune: Optional[str] = None
    adresse: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    surface_m2: Optional[float] = None
    dpe_note: Optional[str] = None
    prix_estime_eur: Optional[float] = None
    prix_m2_estime: Optional[float] = None
    gexscore: Optional[float] = None
    grade: Optional[str] = None
    prix_annonce_eur: Optional[float] = None
    is_deal: Optional[bool] = None


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
    par /estimate (qui ferait des appels réseau externes).

    NB : c'est aussi l'endpoint interrogé par le monitoring externe
    (.github/workflows/monitor-fallback.yml) qui alerte si `prix_dvf` reste
    en repli (source != supabase_live) plus d'1h — voir ce workflow pour le
    détail, l'état de repli ne peut pas être suivi ici en mémoire (Vercel
    serverless = pas d'état persistant entre invocations)."""
    checks = {}
    try:
        get_zone_config("gex_001")
        checks["zone_config"] = "ok"
    except Exception as e:
        checks["zone_config"] = f"ERREUR: {e}"
    try:
        prix = load_prix_dvf()
        checks["prix_dvf_appartements"] = f"ok ({len(prix)} communes, source={_prix_cache['source']})"
    except Exception as e:
        checks["prix_dvf_appartements"] = f"ERREUR: {e}"
    try:
        prix_maisons = load_prix_dvf_maisons()
        if prix_maisons is None:
            checks["prix_dvf_maisons"] = "indisponible (Supabase non configuré ou v_prix_marche_maisons vide)"
        else:
            checks["prix_dvf_maisons"] = f"ok ({len(prix_maisons)} communes, source={_prix_cache_maisons['source']})"
    except Exception as e:
        checks["prix_dvf_maisons"] = f"ERREUR: {e}"
    try:
        prob, source = _compute_regime_bull_prob()
        checks["regime_marche"] = f"ok (prob={prob:.3f}, source={source})"
    except Exception as e:
        checks["regime_marche"] = f"ERREUR: {e}"
    # AJOUTÉ le 22/07/2026 (intégration Sentry) — jamais masquer si le
    # monitoring d'erreurs est réellement actif ou non en production.
    checks["sentry"] = "ok (DSN configure, PII desactivee)" if SENTRY_DSN else "non_configure (SENTRY_DSN absent)"
    # AJOUTÉ le 22/07/2026 — signal secondaire XGBoost. Honnête sur le R²
    # CV réel (0.31, plus faible que l'hédonique 0.89 — feature set plus
    # pauvre, voir engine/xgboost_divergence.py) plutôt que de le masquer.
    try:
        xgb_status = get_xgboost_status()
        if xgb_status.get("disponible"):
            checks["xgboost_divergence"] = (
                f"ok (n={xgb_status['n_entrainement']}, R2_cv={xgb_status['r2_cv']}, "
                f"seuil={xgb_status['seuil_divergence_pct']*100:.1f}%)"
            )
        else:
            checks["xgboost_divergence"] = f"indisponible ({xgb_status.get('erreur')})"
    except Exception as e:
        checks["xgboost_divergence"] = f"ERREUR: {e}"
    return {"status": "ok", "version": VERSION, "checks": checks}


@app.get("/prix-marche")
async def prix_marche():
    prix_maisons = load_prix_dvf_maisons()
    return {
        "status": "ok",
        "source": "DVF reel DGFiP 2014-2025",
        "communes": load_prix_dvf(),
        # Ajouté le 16/07/2026 — None si Supabase indisponible, jamais un
        # objet vide fabriqué pour "faire joli".
        "communes_maisons": prix_maisons,
        "communes_maisons_note": (
            "265 transactions Maison reelles 2025 (dont 198 avec surface_terrain), "
            "echantillon plus petit et plus recent que les appartements 2014-2025"
        ) if prix_maisons else "indisponible",
    }


@app.post("/estimate")
async def estimate(req: EstimateRequest):
    zone_cfg = get_zone_config(req.zone_id)
    type_bien = (req.type_bien or "appartement").lower()
    if type_bien not in ("appartement", "maison"):
        raise HTTPException(status_code=422, detail=f"type_bien invalide : '{req.type_bien}' (attendu 'appartement' ou 'maison')")
    prix_m2_zone, commune_nom, derniere_transaction, nb_transactions_dvf, maison_indisponible, terrain_mediane_commune = get_prix_m2(req.commune, type_bien)
    dpe = (req.dpe_note or "D").upper()
    age_bien = (2026 - req.annee_construction) if req.annee_construction else 20  # 20 = neutre (0% ajust.)

    # Référence foncière pour l'ajustement terrain (17/07/2026) — priorité à
    # la médiane RÉELLE de la commune (vue Supabase), repli sur la médiane
    # poolée réelle de zone si indisponible pour cette commune. Jamais de
    # valeur inventée à la volée — voir get_prix_m2() et compute_avm_hedonique().
    surface_terrain_ref = terrain_mediane_commune or zone_cfg["hedonic_betas"].get("terrain_reference_defaut_m2")

    # ── 1. Score Frontalier — RÉEL, OSRM + OSM Overpass en direct ──────────
    frontalier, frontalier_timed_out = _frontalier_with_timeout(req.lat, req.lon, zone_cfg)
    t_gva_min = frontalier["raw"]["t_gva_min"]
    nb_medecins = frontalier["raw"]["nb_medecins"] or 0
    desert_medical = (nb_medecins == 0)
    bruit_score = frontalier["detail"]["score_bruit"]

    # ── 2. AVM Hédonique — RÉEL, prix DVF + coefficients calibrés ──────────
    #    prix_m2_zone est maintenant le médian Maison ou Appartement selon
    #    type_bien (16/07/2026) — voir get_prix_m2().
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
        surface_terrain_m2=(req.surface_terrain_m2 if type_bien == "maison" else None),
        surface_terrain_ref_m2=surface_terrain_ref,
        type_bien=type_bien,
    )

    # ── 2bis. Signal secondaire XGBoost (challenger model, feu vert Helen
    #    22/07/2026) — jamais utilisé pour recalculer avm["prix_estime_eur"],
    #    uniquement un flag informatif de désaccord entre deux modèles
    #    indépendants (voir engine/xgboost_divergence.py, méthodologie SR
    #    11-7). N'interrompt jamais /estimate en cas d'erreur/indisponibilité.
    try:
        xgb_divergence = compute_divergence_xgboost(
            prix_m2_hedonique=avm["prix_m2_estime"],
            surface_m2=req.surface_m2,
            age_bien=age_bien,
            annee_mutation=2026,
            dpe_note=dpe,
            code_commune=commune_nom,
            type_bien=type_bien,
        )
    except Exception as e:
        xgb_divergence = {"disponible": False, "erreur": f"exception non geree: {e!r}"}
    if xgb_divergence.get("flag_divergence"):
        log.warning(
            f"[XGBOOST_DIVERGENCE] {commune_nom} {req.surface_m2}m2 DPE={dpe} : "
            f"hedonique={avm['prix_m2_estime']}EUR/m2 vs xgboost={xgb_divergence['prix_m2_xgboost']}EUR/m2 "
            f"(divergence={xgb_divergence['divergence_pct']}%, seuil={xgb_divergence['seuil_divergence_pct']}%)"
        )
        if SENTRY_DSN:
            sentry_sdk.capture_message(
                f"Divergence XGBoost/hedonique : {commune_nom} {xgb_divergence['divergence_pct']}% "
                f"(seuil {xgb_divergence['seuil_divergence_pct']}%)",
                level="warning",
            )

    # ── 3. ESG — PARTIEL mais amélioré le 21/07/2026 (point 5/9 - feu vert
    #    Helen). Inondation + argile sont maintenant RÉELS (API Géorisques
    #    BRGM/MTE en direct, cf. _georisques_scores()). NDVI/mixité/vacance/
    #    démographie restent des défauts neutres de zone (INSEE Filosofi et
    #    Sentinel-2 NDVI pas encore branchés — prochains chantiers).
    georisques = _georisques_scores(req.lat, req.lon)
    esg = compute_esg_score(
        dpe_note=dpe,
        inondation_risk=georisques["inondation_risk"],
        argile_risk=georisques["argile_risk"],
        ndvi=0.40,
        nb_medecins=nb_medecins,
        mixite_sociale_score=50.0,
        vacance_logement_pct=5.0,
        pop_growth_5y_pct=1.0,
        zone_cfg=zone_cfg,
    )

    # ── 4. Merton Jump-Diffusion — RÉEL, modèle stochastique calibré ───────
    merton = compute_avm_merton(prix_central=avm["prix_estime_eur"], zone_cfg=zone_cfg)

    # ── 5. Score spatial — RÉEL quand assez de transactions géocodées à
    #    proximité (moyenne locale du prix/m2 pondérée par noyau gaussien de
    #    distance, cf. spatial_local_estimate() sur Supabase/PostGIS),
    #    repli honnête sur le proxy hédonique sinon. Ajouté le 18/07/2026
    #    (point 5/9 demandé par Helen). Ce n'est toujours PAS une GWR
    #    multivariée complète (roadmap) — c'est un lissage spatial univarié
    #    réel, pas une simple dérivation de l'ajustement hédonique.
    score_spatial_proxy = max(0.0, min(100.0, 50.0 + avm["total_ajustement_pct"]))
    geostat_raw = _spatial_local_estimate(req.lat, req.lon)
    score_spatial_geostat = None
    if geostat_raw:
        try:
            score_spatial_geostat = compute_spatial_score_geostat(
                prix_m2_local_pondere=float(geostat_raw["prix_m2_pondere"]) if geostat_raw.get("prix_m2_pondere") is not None else None,
                prix_m2_median_zone=prix_m2_zone,
                n_voisins=int(geostat_raw.get("n_voisins") or 0),
            )
        except (TypeError, ValueError):
            score_spatial_geostat = None
    if score_spatial_geostat is not None:
        score_spatial = score_spatial_geostat
        spatial_source = f"geostat_reel_noyau_gaussien_{int(geostat_raw.get('n_voisins'))}_voisins_800m"
    else:
        score_spatial = score_spatial_proxy
        spatial_source = "proxy_derive_avm_hedonique_pas_assez_de_voisins_geocodes"

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
            "type_bien": type_bien,
            "nb_transactions_dvf": nb_transactions_dvf,
            "donnees_dvf_a_jour_au": derniere_transaction,
            "ajustement_dpe_pct": avm["ajustements"]["dpe"],
            "ajustements_detail_pct": avm["ajustements"],
            "total_ajustement_pct": avm["total_ajustement_pct"],
            "total_ajustement_brut_pct": avm.get("total_ajustement_brut_pct", avm["total_ajustement_pct"]),
            "ajustement_plafonne": avm.get("ajustement_plafonne", False),
            "surface_m2": req.surface_m2,
            "surface_terrain_m2": req.surface_terrain_m2,
        },
        "merton": merton,
        "signal_secondaire_xgboost": xgb_divergence,
        "frontalier": frontalier,
        "esg": {
            "esg_score": esg["esg_score"],
            "esg_grade": esg["esg_grade"],
        },
        "deal_alert": deal,
        "data_quality_notes": {
            "frontalier": "timeout_fallback_neutre" if frontalier_timed_out else "temps_reel_osrm_osm_overpass",
            "esg": f"partiel_dpe_et_georisques_reels_{georisques['source']}_reste_neutre_insee_ndvi_non_branches",
            "score_spatial": spatial_source,
            "regime_marche": regime_source,
            "donnees_dvf_a_jour_au": "reel_vue_supabase" if derniere_transaction else "indisponible_source_active_ne_l_expose_pas",
            "type_bien_maturite": (
                "maison_donnees_indisponibles_prix_defaut_zone_utilise" if (type_bien == "maison" and maison_indisponible)
                else "maison_echantillon_reduit_2025_uniquement_265_transactions_dont_198_avec_terrain" if type_bien == "maison"
                else "appartement_echantillon_large_2014_2025"
            ),
            "surface_terrain": (
                (
                    "ajustement_calibre_applique_beta_0152_regression_reelle_n196"
                    if type_bien == "maison" else
                    "non_applique_type_bien_appartement"
                ) if req.surface_terrain_m2 else "non_fournie"
            ),
            "ajustement_hedonique": "plafonne_a_35pct_garde_fou_ingenierie" if avm.get("ajustement_plafonne") else "non_plafonne",
        },
    })


def _supabase_headers_for_user(authorization: Optional[str]) -> dict:
    """Construit les headers pour un appel Supabase exécuté AU NOM de
    l'utilisateur final (RLS via son propre JWT), et non plus avec la clé
    anon seule. `authorization` doit valoir "Bearer <jwt>" — c'est le JWT
    renvoyé par Supabase Auth au moment de la connexion côté Streamlit.
    C'est ce JWT (pas la clé anon) qui porte l'identité auth.uid() utilisée
    par les policies RLS de estimations_sauvegardees. Sans lui : 401 net."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Authentification requise — connecte-toi (Authorization: Bearer <token>)")
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        raise HTTPException(status_code=503, detail="Supabase non configuré côté API")
    return {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": authorization,
        "Content-Type": "application/json",
    }


@app.post("/estimations/sauvegarder")
async def sauvegarder_estimation(req: EstimationSaveRequest, authorization: Optional[str] = Header(None)):
    """Enregistre une estimation dans le Dashboard (table
    estimations_sauvegardees). user_id est rempli automatiquement côté base
    (DEFAULT auth.uid()) à partir du JWT transmis — jamais fourni par le
    client, pour qu'aucun appel ne puisse écrire dans le compte d'un autre
    utilisateur (la RLS bloquerait de toute façon une tentative de ce genre,
    mais on ne lui laisse même pas l'occasion)."""
    headers = _supabase_headers_for_user(authorization)
    headers["Prefer"] = "return=representation"
    resp = None
    try:
        resp = requests.post(
            f"{SUPABASE_URL.rstrip('/')}/rest/v1/estimations_sauvegardees",
            headers=headers,
            json=req.model_dump(exclude_none=True),
            timeout=8,
        )
        resp.raise_for_status()
        rows = resp.json()
        return {"status": "ok", "id": rows[0]["id"] if rows else None}
    except requests.HTTPError:
        detail = resp.text[:300] if resp is not None else "erreur inconnue"
        status = resp.status_code if resp is not None else 502
        log.error(f"Échec sauvegarde estimation ({status}) — {detail}")
        raise HTTPException(status_code=status if status in (401, 403) else 502, detail=f"Échec sauvegarde Supabase : {detail}")
    except Exception as e:
        log.error(f"Échec sauvegarde estimation ({e})")
        raise HTTPException(status_code=502, detail=f"Échec sauvegarde Supabase : {e}")


@app.get("/estimations")
async def lister_estimations(authorization: Optional[str] = Header(None)):
    """Liste les estimations sauvegardées de l'utilisateur authentifié.
    Aucun paramètre d'identité côté requête : la RLS (auth.uid() = user_id)
    filtre automatiquement selon le JWT transmis — impossible de lister les
    biens d'un autre utilisateur même en modifiant l'appel côté client."""
    headers = _supabase_headers_for_user(authorization)
    resp = None
    try:
        resp = requests.get(
            f"{SUPABASE_URL.rstrip('/')}/rest/v1/estimations_sauvegardees",
            headers=headers,
            params={"select": "*", "order": "created_at.desc"},
            timeout=8,
        )
        resp.raise_for_status()
        return {"status": "ok", "estimations": resp.json()}
    except requests.HTTPError:
        detail = resp.text[:300] if resp is not None else "erreur inconnue"
        status = resp.status_code if resp is not None else 502
        log.error(f"Échec listage estimations ({status}) — {detail}")
        raise HTTPException(status_code=status if status in (401, 403) else 502, detail=f"Échec lecture Supabase : {detail}")
    except Exception as e:
        log.error(f"Échec listage estimations ({e})")
        raise HTTPException(status_code=502, detail=f"Échec lecture Supabase : {e}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
