"""
GexScore — api/events.py
═══════════════════════════════════════════════════════════════════════
Logging RGPD-compliant des événements comportementaux + consentement.

CONSTRUIT le 27/07/2026 (FEU VERT Helen — "OUI, pour la construction").
Contexte explicite d'Helen : usage STRICTEMENT interne ("je ne veux
appeler PERSONNE... c'est juste pour NOUS, tous les deux") pour nourrir
de futurs moteurs de décryptage comportemental. Point vérifié avec elle
le 27/07/2026 : le RGPD s'applique au TRAITEMENT de données personnelles,
pas à leur exposition externe — un usage strictement interne ne dispense
donc PAS du mécanisme de double consentement (Art. 7.4 RGPD : le
consentement doit être libre, spécifique, éclairé, univoque, et JAMAIS
pré-coché ; les lignes directrices CNIL sur les traceurs/traitements
comportementaux — délibération n°2020-091 du 17/09/2020 — appliquent le
même principe à tout traitement, interne ou externe).

Ce module remplace les TODO log-only du design initial (docs/events.py,
jamais committé dans le repo réel) par de VRAIS appels Supabase REST,
authentifiés par le JWT de l'utilisateur (RLS, jamais la clé anon seule)
— même pattern que api/main.py::_supabase_headers_for_user.

Sécurité d'identité (IMPORTANT) : client_id n'est JAMAIS envoyé par ce
module dans les payloads d'insertion. Il est rempli par Postgres
lui-même via `DEFAULT auth.uid()` sur events_comportementaux.client_id
et rgpd_consents.client_id (migrations du 27/07/2026) — donc même un bug
dans ce fichier ne pourrait jamais écrire un événement/consentement au
nom d'un autre utilisateur. Le JWT n'est jamais décodé "à la main" ici
(anti-pattern de sécurité : un JWT non vérifié peut être forgé avec
n'importe quel `sub`) — c'est PostgREST/Supabase Auth qui le vérifie.

Garde-fou déjà en place côté base (à ne jamais contourner) : le trigger
check_consent_before_event() (AFTER INSERT sur events_comportementaux)
calcule consent_verified selon l'état RÉEL du consentement en base.
Si absent : l'événement est quand même stocké (traçabilité), mais
consent_verified=false — il ne doit JAMAIS être inclus dans un futur
agrégat/moteur de décryptage comportemental tant que ce flag est false.
Ce module ne duplique jamais cette logique côté API (source de vérité
unique = la base).

Auteur : Steelldy SAS — Juillet 2026
"""
import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import requests
from fastapi import HTTPException
from pydantic import BaseModel

log = logging.getLogger("gexscore.events")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")


# ── Modèles ─────────────────────────────────────────────────────────────────

class ConsentRequest(BaseModel):
    """Double opt-in RGPD — deux cases à cocher SÉPARÉES côté UI.
    'usage_essentiel' peut être vrai par défaut à l'affichage (base légale
    = exécution du contrat, pas de consentement RGPD requis au sens
    strict). 'analyse_comportementale' ne doit JAMAIS être pré-coché dans
    l'UI (Art. 7.4 RGPD) — cette règle est de la responsabilité de
    scoregex_app.py, ce modèle ne fait que transporter la valeur reçue."""
    usage_essentiel: bool = True
    analyse_comportementale: bool
    cgu_version: str = "1.0.0"
    ip_address: Optional[str] = None   # Hashé avant stockage, jamais en clair


class EventLog(BaseModel):
    """Un événement comportemental à logger. Pas de champ client_id ici :
    toujours déduit du JWT côté Postgres (DEFAULT auth.uid()), jamais
    fourni par l'appelant (voir docstring module)."""
    zone_id: str = "gex_001"
    event_type: str
    bien_id: Optional[UUID] = None
    metadata: dict = {}


EVENT_TYPES = {
    "recherche_estimation":   "Utilisateur a lancé une estimation",
    "bien_consulte":          "Utilisateur a consulté le détail d'un bien",
    "simulateur_budget":      "Utilisateur a utilisé le simulateur budget frontalier",
    "deal_alert_clicked":     "Utilisateur a cliqué sur un Deal Alert",
    "rapport_telecharge":     "Utilisateur a téléchargé un rapport PDF",
    "abonnement_souscrit":    "Utilisateur a souscrit un abonnement payant",
    "abonnement_resilie":     "Utilisateur a résilié son abonnement",
}


# ── Anonymisation ─────────────────────────────────────────────────────────────

def hash_ip(ip_address: Optional[str]) -> Optional[str]:
    """SHA-256, irréversible — minimisation RGPD (Art. 5.1.c). Utile
    uniquement pour détecter des doublons/abus, jamais pour identifier
    une personne."""
    if not ip_address:
        return None
    return hashlib.sha256(ip_address.encode()).hexdigest()


def bucketize_budget(budget_eur: Optional[float]) -> Optional[str]:
    """Tranche plutôt que montant exact — minimisation RGPD. Inutile de
    savoir qu'un client a EXACTEMENT 437 000 EUR de budget, seulement
    qu'il est dans la tranche 400-500k — suffisant pour un futur moteur
    de décryptage comportemental, bien moins sensible en cas de fuite."""
    if budget_eur is None:
        return None
    tranches = [
        (0, 250_000, "0-250k"),
        (250_000, 350_000, "250-350k"),
        (350_000, 450_000, "350-450k"),
        (450_000, 600_000, "450-600k"),
        (600_000, 800_000, "600-800k"),
        (800_000, float("inf"), "800k+"),
    ]
    for lo, hi, label in tranches:
        if lo <= budget_eur < hi:
            return label
    return "non_renseigne"


def _headers(authorization: Optional[str]) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Authentification requise pour cette action (Authorization: Bearer <token>)")
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        raise HTTPException(status_code=503, detail="Supabase non configuré côté API")
    return {"apikey": SUPABASE_ANON_KEY, "Authorization": authorization, "Content-Type": "application/json"}


# ── Fonctions principales (réellement branchées sur Supabase) ───────────────

async def record_consent(req: ConsentRequest, authorization: Optional[str]) -> dict:
    """INSERT réel dans rgpd_consents (deux lignes : essentiel +
    comportemental). client_id jamais envoyé (DEFAULT auth.uid() côté
    Postgres, migration rgpd_consents_client_id_default_auth_uid,
    27/07/2026)."""
    ip_hash = hash_ip(req.ip_address)
    headers = _headers(authorization)
    headers["Prefer"] = "return=representation"
    rows_to_insert = [
        {
            "consent_type": "usage_essentiel",
            "consent_given": req.usage_essentiel,
            "consent_text_version": req.cgu_version,
            "ip_address_hash": ip_hash,
        },
        {
            "consent_type": "analyse_comportementale",
            "consent_given": req.analyse_comportementale,
            "consent_text_version": req.cgu_version,
            "ip_address_hash": ip_hash,
        },
    ]
    resp = None
    try:
        resp = requests.post(
            f"{SUPABASE_URL.rstrip('/')}/rest/v1/rgpd_consents",
            headers=headers,
            json=rows_to_insert,
            timeout=8,
        )
        resp.raise_for_status()
    except requests.HTTPError:
        detail = resp.text[:300] if resp is not None else "erreur inconnue"
        status = resp.status_code if resp is not None else 502
        log.error(f"Échec enregistrement consentement ({status}) — {detail}")
        raise HTTPException(status_code=status if status in (401, 403) else 502, detail=f"Échec enregistrement consentement : {detail}")
    except Exception as e:
        log.error(f"Échec enregistrement consentement ({e})")
        raise HTTPException(status_code=502, detail=f"Échec enregistrement consentement : {e}")

    log.info(f"[CONSENT] comportemental={req.analyse_comportementale} cgu_v={req.cgu_version}")
    return {
        "status": "consent_recorded",
        "analyse_comportementale_active": req.analyse_comportementale,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }


async def log_event(event: EventLog, authorization: Optional[str]) -> dict:
    """INSERT réel dans events_comportementaux. Le trigger SQL
    check_consent_before_event() applique automatiquement consent_verified
    selon l'état RÉEL du consentement en base — ce module ne décide
    jamais lui-même si le consentement est valide (source de vérité
    unique = la base, jamais dupliquée côté API).

    Best-effort et JAMAIS bloquant pour l'utilisateur : un échec de
    logging ne doit jamais faire échouer l'action principale (ex.
    /estimate) — voir l'appel dans api/main.py, toujours enveloppé
    dans un try/except qui avale l'erreur après l'avoir loggée."""
    safe_metadata = dict(event.metadata)
    if "budget_eur" in safe_metadata:
        safe_metadata["budget_max_tranche"] = bucketize_budget(safe_metadata.pop("budget_eur"))

    headers = _headers(authorization)
    headers["Prefer"] = "return=representation"
    payload = {
        "zone_id": event.zone_id,
        "event_type": event.event_type,
        "metadata": safe_metadata,
    }
    if event.bien_id:
        payload["bien_id"] = str(event.bien_id)

    resp = None
    try:
        resp = requests.post(
            f"{SUPABASE_URL.rstrip('/')}/rest/v1/events_comportementaux",
            headers=headers,
            json=payload,
            timeout=8,
        )
        resp.raise_for_status()
        rows = resp.json()
        consent_verified = rows[0]["consent_verified"] if rows else None
    except requests.HTTPError:
        detail = resp.text[:300] if resp is not None else "erreur inconnue"
        log.error(f"Échec logging événement '{event.event_type}' ({resp.status_code if resp is not None else '?'}) — {detail}")
        return {"status": "logging_failed", "event_type": event.event_type}
    except Exception as e:
        log.error(f"Échec logging événement '{event.event_type}' ({e})")
        return {"status": "logging_failed", "event_type": event.event_type}

    return {
        "status": "logged",
        "event_type": event.event_type,
        "consent_verified": consent_verified,
        "anonymized": "budget_eur" in event.metadata,
    }
