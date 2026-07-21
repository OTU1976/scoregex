"""
scripts/bodacc_sync.py — Distressed Asset Scanner : synchronisation BODACC
═══════════════════════════════════════════════════════════════════════════
Ajouté le 21/07/2026 (point 8/9 demandé par Helen : feu vert explicite).
Interroge l'API publique BODACC (bodacc-datadila.opendatasoft.com, gratuite,
sans clé — vérifiée en direct le 18/07/2026 et le 21/07/2026) pour les 8
communes du Pays de Gex, et écrit les annonces légales (créations,
radiations, procédures collectives...) dans Supabase (table bodacc_avis).

Codes réels du champ "familleavis" (vérifiés via facet=familleavis, PAS
supposés) : dpc, modification, creation, radiation, collective (= procédures
collectives : liquidation/redressement/sauvegarde judiciaire), vente,
immatriculation, divers, conciliation, retablissement_professionnel,
inconnue. La vue v_bodacc_distressed_recent (Supabase) filtre sur
"collective" et "radiation" des 12 derniers mois pour le scanner.

Exécuté par .github/workflows/bodacc-sync.yml (scheduled quotidien).
"""
import os
import sys
import time
import requests

BODACC_BASE = "https://bodacc-datadila.opendatasoft.com/api/records/1.0/search/"

# 8 communes du Pays de Gex — filtre par "ville" exact (plus fiable que le
# code postal, qui est partagé entre plusieurs communes dans ce secteur).
COMMUNES = [
    "Cessy", "Ferney-Voltaire", "Gex", "Ornex",
    "Prévessin-Moëns", "Saint-Genis-Pouilly", "Sergy", "Thoiry",
]

ROWS_PAR_PAGE = 100  # limite de l'API opendatasoft v1
MAX_PAGES_PAR_COMMUNE = 200  # garde-fou : 200*100 = 20 000 avis max/commune

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_ANON_KEY = os.environ["SUPABASE_ANON_KEY"]


def fetch_commune(ville: str) -> list[dict]:
    rows = []
    start = 0
    for _ in range(MAX_PAGES_PAR_COMMUNE):
        params = {
            "dataset": "annonces-commerciales",
            "refine.ville": ville,
            "rows": ROWS_PAR_PAGE,
            "start": start,
        }
        resp = requests.get(BODACC_BASE, params=params, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        records = payload.get("records", [])
        rows.extend(records)
        nhits = payload.get("nhits", 0)
        start += ROWS_PAR_PAGE
        if start >= nhits or not records:
            break
        time.sleep(0.15)
    return rows


def to_row(rec: dict) -> dict | None:
    f = rec.get("fields", {})
    avis_id = f.get("id")
    if not avis_id:
        return None
    return {
        "id": avis_id,
        "commune": f.get("ville"),
        "code_postal": f.get("cp"),
        "commercant": f.get("commercant"),
        "familleavis": f.get("familleavis"),
        "familleavis_lib": f.get("familleavis_lib"),
        "typeavis_lib": f.get("typeavis_lib"),
        "date_parution": f.get("dateparution"),
        "tribunal": f.get("tribunal"),
        "url_complete": f.get("url_complete"),
        "registre": f.get("registre"),
    }


def upsert_batch(rows: list[dict]) -> None:
    if not rows:
        return
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/bodacc_avis",
        headers={
            "apikey": SUPABASE_ANON_KEY,
            "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
        params={"on_conflict": "id"},
        json=rows,
        timeout=30,
    )
    resp.raise_for_status()


def main() -> None:
    total = 0
    for ville in COMMUNES:
        print(f"[{ville}] interrogation BODACC...")
        raw = fetch_commune(ville)
        parsed = [x for x in (to_row(r) for r in raw) if x is not None]
        print(f"[{ville}] {len(raw)} avis BODACC, {len(parsed)} valides")
        for i in range(0, len(parsed), 500):
            upsert_batch(parsed[i:i + 500])
        total += len(parsed)
    print(f"TOTAL synchronisé : {total} avis BODACC (8 communes Pays de Gex)")
    if total == 0:
        print("::error::Aucun avis synchronisé — vérifier l'API BODACC ou le réseau")
        sys.exit(1)


if __name__ == "__main__":
    main()
