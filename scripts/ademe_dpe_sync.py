"""
scripts/ademe_dpe_sync.py — Croisement empirique ADEME "Observatoire des DPE"
═══════════════════════════════════════════════════════════════════════════
Ajouté le 21/07/2026 (point 7/9 demandé par Helen : "OUI, OUI, et OUI" puis
feu vert explicite). Interroge le dataset public ADEME "DPE Logements
existants (depuis juillet 2021)" (data-fair, gratuit, sans clé) pour les
8 communes du Pays de Gex, et écrit les résultats dans Supabase
(table dpe_ademe_reference).

Dataset ADEME vérifié en direct le 21/07/2026 : id réel = "meg-83tjwtg8dyz4vv7h1dqe"
(l'ancien id supposé "dpe-v2-logements-existants" n'existe plus / n'a jamais
été le bon slug — vérifié via /data-fair/api/v1/datasets?q=dpe+existants).

Exécuté par .github/workflows/ademe-dpe-sync.yml (runner GitHub = accès
internet complet, contourne le proxy restreint de l'environnement de
développement où data.ademe.fr est bloqué par l'allowlist).

Usage : SUPABASE_URL=... SUPABASE_ANON_KEY=... python3 scripts/ademe_dpe_sync.py
"""
import os
import sys
import time
import requests
from urllib.parse import urlparse, parse_qs

ADEME_DATASET_ID = "meg-83tjwtg8dyz4vv7h1dqe"
ADEME_BASE = f"https://data.ademe.fr/data-fair/api/v1/datasets/{ADEME_DATASET_ID}/lines"

# 8 communes du Pays de Gex — code INSEE confirmé via Supabase (table biens).
COMMUNES = [
    ("01071", "Cessy"),
    ("01160", "Ferney-Voltaire"),
    ("01173", "Gex"),
    ("01281", "Ornex"),
    ("01313", "Prévessin-Moëns"),
    ("01354", "Saint-Genis-Pouilly"),
    ("01401", "Sergy"),
    ("01419", "Thoiry"),
]

FIELDS = ",".join([
    "numero_dpe", "code_insee_ban", "nom_commune_ban", "etiquette_dpe",
    "etiquette_ges", "type_batiment", "annee_construction",
    "surface_habitable_logement", "_geopoint", "date_etablissement_dpe",
])

# CORRIGÉ le 21/07/2026 avant déploiement : le repo n'a PAS de secret
# SUPABASE_ANON_KEY (vérifié via Settings > Secrets and variables > Actions
# — seuls SUPABASE_URL et SUPABASE_SERVICE_ROLE_KEY existent). Un job CI qui
# écrit des données est de toute façon plus à sa place avec la clé
# service_role (bypasse RLS, usage serveur uniquement) qu'avec la clé anon.
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

MAX_PAGES_PAR_COMMUNE = 30  # garde-fou : 30 * 1000 = 30 000 lignes max/commune


def fetch_commune(code_insee: str) -> list[dict]:
    rows = []
    after = None
    for _ in range(MAX_PAGES_PAR_COMMUNE):
        params = {
            "size": 1000,
            "select": FIELDS,
            "qs": f'code_insee_ban:"{code_insee}"',
        }
        if after:
            params["after"] = after
        resp = requests.get(ADEME_BASE, params=params, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        results = payload.get("results", [])
        rows.extend(results)
        next_url = payload.get("next")
        if not next_url or not results:
            break
        # IMPORTANT (bug attrape avant deploiement, pas en prod) : le
        # curseur "after" renvoye par data-fair est deja URL-encode dans
        # "next". Un simple split("after=") laisse la valeur encodee, et
        # requests la re-encoderait une 2e fois (%2C -> %253C), cassant la
        # pagination silencieusement. On utilise parse_qs pour recuperer la
        # valeur DECODEE, que requests re-encodera correctement une seule
        # fois via params=.
        after = parse_qs(urlparse(next_url).query).get("after", [None])[0]
        if not after:
            break
        time.sleep(0.2)
    return rows


def to_row(r: dict) -> dict | None:
    numero_dpe = r.get("numero_dpe")
    if not numero_dpe:
        return None
    lat, lon = None, None
    geopoint = r.get("_geopoint")
    if geopoint and "," in geopoint:
        try:
            lat_str, lon_str = geopoint.split(",")
            lat, lon = float(lat_str), float(lon_str)
        except ValueError:
            pass
    return {
        "numero_dpe": numero_dpe,
        "code_insee": r.get("code_insee_ban"),
        "nom_commune": r.get("nom_commune_ban"),
        "etiquette_dpe": r.get("etiquette_dpe"),
        "etiquette_ges": r.get("etiquette_ges"),
        "type_batiment": r.get("type_batiment"),
        "annee_construction": r.get("annee_construction"),
        "surface_habitable": r.get("surface_habitable_logement"),
        "latitude": lat,
        "longitude": lon,
        "date_etablissement_dpe": r.get("date_etablissement_dpe"),
    }


def upsert_batch(rows: list[dict]) -> None:
    if not rows:
        return
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/dpe_ademe_reference",
        headers={
            "apikey": SUPABASE_SERVICE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
        params={"on_conflict": "numero_dpe"},
        json=rows,
        timeout=30,
    )
    resp.raise_for_status()


def main() -> None:
    total_synced = 0
    for code_insee, nom in COMMUNES:
        print(f"[{nom} ({code_insee})] interrogation ADEME...")
        raw_rows = fetch_commune(code_insee)
        parsed = [x for x in (to_row(r) for r in raw_rows) if x is not None]
        print(f"[{nom}] {len(raw_rows)} lignes ADEME, {len(parsed)} valides")
        # Upsert par lots de 500 (limite raisonnable pour PostgREST)
        for i in range(0, len(parsed), 500):
            upsert_batch(parsed[i:i + 500])
        total_synced += len(parsed)
    print(f"TOTAL synchronisé : {total_synced} DPE réels (8 communes Pays de Gex)")
    if total_synced == 0:
        print("::error::Aucune ligne synchronisée — vérifier le dataset ADEME ou le réseau")
        sys.exit(1)


if __name__ == "__main__":
    main()
