"""
GexScore - scripts/check_health_alert.py
=============================================================================
Alerte automatique si le pipeline Supabase tombe en repli JSON.

Pourquoi ce script existe en dehors de l'API (api/main.py) :
  L'API tourne sur Vercel (serverless). Un cold start reinitialise toute
  variable en memoire du process Python - il est donc impossible de suivre
  fiablement "depuis combien de temps est-on en repli ?" a l'interieur meme
  de l'API. Ce suivi doit vivre a l'exterieur, dans un processus qui persiste
  son etat entre executions. Ce script fait ca :

  1. Appelle GET {API_URL}/health
  2. Lit le champ checks.prix_dvf (ex: "ok (8 communes, source=supabase_live)")
     et en extrait la source active (supabase_live / json_snapshot_local /
     fallback_minimal / ERREUR).
  3. Compare a un fichier d'etat JSON persiste dans le repo
     (.monitor/repli_state.json, commite par le workflow GitHub Actions
     appelant ce script - voir .github/workflows/monitor-fallback.yml).
  4. Si la source n'est PAS "supabase_live" depuis plus d'ALERT_THRESHOLD_S
     secondes (defaut 3600 = 1h) ET qu'aucune alerte n'a deja ete envoyee
     pour cet episode : envoie un webhook (ALERT_WEBHOOK_URL) et marque
     l'episode comme alerte.
  5. Si la source redevient "supabase_live" : reinitialise l'etat (nouvel
     episode possible au prochain repli).

  Le webhook envoie un payload compatible Slack "incoming webhook"
  ({"text": "..."}), egalement compatible tel quel avec la plupart des
  services qui acceptent ce format (Discord via un relais, Mattermost,
  n8n/Zapier en mode "catch raw JSON", ntfy.sh, etc.). Configurer
  ALERT_WEBHOOK_URL avec l'URL de son choix.

USAGE (voir aussi .github/workflows/monitor-fallback.yml) :
  export API_HEALTH_URL="https://scoregex.vercel.app/health"
  export ALERT_WEBHOOK_URL="https://hooks.slack.com/services/..."   # optionnel
  export STATE_PATH=".monitor/repli_state.json"                      # optionnel
  python scripts/check_health_alert.py

Auteur : Steelldy SAS - Juillet 2026
"""

import os
import re
import sys
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger("check_health_alert")

DEFAULT_API_HEALTH_URL = "https://scoregex.vercel.app/health"
DEFAULT_STATE_PATH = ".monitor/repli_state.json"
DEFAULT_ALERT_THRESHOLD_S = 3600  # 1h
HTTP_TIMEOUT_S = 10

# NB : lus dynamiquement (pas de constante figee a l'import) pour que le
# script reagisse a l'environnement au moment de l'execution, pas au moment
# de l'import - important pour les tests, et plus robuste en general.

SOURCE_RE = re.compile(r"source=([a-zA-Z_]+)")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def load_state(state_path: Path) -> dict:
    if state_path.exists():
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning(f"Etat illisible ({e}) - reinitialisation")
    return {"non_live_since": None, "alerted": False, "last_source": None}


def save_state(state: dict, state_path: Path) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def fetch_source(api_health_url: str) -> str:
    """Retourne la source active ('supabase_live', 'json_snapshot_local',
    'fallback_minimal') ou 'unreachable' / 'unknown' en cas d'echec."""
    try:
        resp = requests.get(api_health_url, timeout=HTTP_TIMEOUT_S)
        resp.raise_for_status()
        body = resp.json()
        prix_dvf_check = body.get("checks", {}).get("prix_dvf", "")
        match = SOURCE_RE.search(prix_dvf_check)
        if match:
            return match.group(1)
        log.warning(f"Champ prix_dvf inattendu, source non detectee : {prix_dvf_check!r}")
        return "unknown"
    except Exception as e:
        log.error(f"Echec appel {api_health_url} ({e})")
        return "unreachable"


def send_webhook(message: str, webhook_url: Optional[str]) -> bool:
    if not webhook_url:
        log.warning("ALERT_WEBHOOK_URL non configure - alerte NON envoyee (log uniquement) : " + message)
        return False
    try:
        resp = requests.post(webhook_url, json={"text": message}, timeout=HTTP_TIMEOUT_S)
        resp.raise_for_status()
        log.info("Webhook envoye avec succes")
        return True
    except Exception as e:
        log.error(f"Echec envoi webhook ({e})")
        return False


def main() -> int:
    api_health_url = os.environ.get("API_HEALTH_URL", DEFAULT_API_HEALTH_URL)
    alert_webhook_url = os.environ.get("ALERT_WEBHOOK_URL")
    state_path = Path(os.environ.get("STATE_PATH", DEFAULT_STATE_PATH))
    alert_threshold_s = int(os.environ.get("ALERT_THRESHOLD_S", str(DEFAULT_ALERT_THRESHOLD_S)))

    source = fetch_source(api_health_url)
    state = load_state(state_path)
    now = now_utc()
    is_live = (source == "supabase_live")

    event = {
        "ts": now.isoformat(),
        "event": "health_check",
        "source": source,
        "api_health_url": api_health_url,
    }

    if is_live:
        if state.get("non_live_since"):
            log.info(f"Source revenue a supabase_live - fin de l'episode de repli. {json.dumps(event)}")
        # NB : last_checked_at change a CHAQUE run, meme quand rien d'autre ne
        # bouge. C'est deliberre : GitHub desactive automatiquement les
        # workflows programmes (cron) apres 60 jours sans commit dans le repo
        # (regle GitHub, verifiee - s'applique a tout le repo, pas juste a ce
        # workflow. Ca desactiverait aussi dvf-reminder.yml). Sans ce champ,
        # quand tout va bien (le cas nominal), l'etat ne changerait jamais et
        # ce workflow ne commiterait plus jamais -> il se desactiverait tout
        # seul apres 60 jours, PENDANT que tout va bien, et resterait
        # silencieux le jour ou un vrai repli survient. Ce champ garantit un
        # commit a chaque run (~toutes les 20 min), donc une activite repo
        # continue, donc jamais de desactivation automatique.
        state = {
            "non_live_since": None,
            "alerted": False,
            "last_source": source,
            "last_checked_at": now.isoformat(),
        }
        save_state(state, state_path)
        print(json.dumps({**event, "alert_fired": False, "episode_active": False}))
        return 0

    # Source non-live (repli JSON, fallback minimal, injoignable, ou inconnue)
    if not state.get("non_live_since"):
        state["non_live_since"] = now.isoformat()
        state["alerted"] = False
        log.warning(f"Nouveau debut d'episode de repli detecte (source={source}). {json.dumps(event)}")
    state["last_source"] = source
    state["last_checked_at"] = now.isoformat()  # voir commentaire ci-dessus (anti auto-disable 60j)

    elapsed_s = (now - parse_iso(state["non_live_since"])).total_seconds()
    alert_fired = False

    if elapsed_s >= alert_threshold_s and not state.get("alerted"):
        depuis = parse_iso(state["non_live_since"]).strftime("%d/%m/%Y %H:%M UTC")
        message = (
            "ScoreGex : le pipeline de prix est en repli depuis plus d'1h "
            f"(source actuelle={source}, en repli depuis {depuis}). "
            f"Verifier Supabase ({api_health_url})."
        )
        alert_fired = send_webhook(message, alert_webhook_url)
        state["alerted"] = True
        log.warning(f"Seuil de {alert_threshold_s}s depasse - alerte declenchee (envoyee={alert_fired})")
    else:
        log.info(f"En repli depuis {elapsed_s:.0f}s (seuil {alert_threshold_s}s, deja alerte={state.get('alerted')})")

    save_state(state, state_path)
    print(json.dumps({
        **event,
        "alert_fired": alert_fired,
        "episode_active": True,
        "non_live_since": state["non_live_since"],
        "elapsed_s": round(elapsed_s),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
