"""
GexScore - scripts/sync_dvf_opendata.py
=============================================================================
Synchronise les transactions DVF+ open-data (API Cerema/DGALN) vers Supabase,
sans intervention manuelle : plus besoin de telecharger des CSV a la main sur
explore.data.gouv.fr/immobilier et de les committer dans data/*.csv.

Remplace le telechargement manuel qui precedait scripts/upload_dvf_to_supabase.py.
Ce dernier reste present et fonctionnel (pipeline legacy, CSV DVF bruts) -- les
deux sources coexistent dans la table `biens` (voir db/003_dvf_opendata_source.sql,
colonne `idopendata` + `source_pipeline`).

SOURCE : API "Donnees foncieres" du Cerema / DGALN, flux DVF+ open-data
(acces libre, gratuit, sans cle). Endpoint verifie en direct pendant la
session du 11/07/2026 (deux echantillons reels obtenus : une maison
codtypbien=111 et un appartement codtypbien=121, cf. conversation) :

  GET https://apidf-preprod.cerema.fr/dvf_opendata/mutations/
      ?code_insee=<code>&fields=all&page_size=<n>&idnatmut=1&anneemut_min=2014

C'est la meme source qui alimente https://explore.data.gouv.fr/fr/immobilier
(confirme via https://www.data.gouv.fr/dataservices/api-donnees-foncieres,
flux "DVF+ - Mutations", acces libre).

DIFFERENCE STRUCTURELLE IMPORTANTE avec l'ancien pipeline CSV DVF brut :
cette API renvoie une ligne DEJA AGREGEE par mutation (Cerema a deja fait
le travail de regroupement multi-locaux que l'ancien script faisait
manuellement dans load_and_aggregate()). Pas de "numero_disposition", pas
de parcelle unique -- une mutation peut couvrir plusieurs parcelles
(champ l_idpar, liste).

FILTRE "appartement pur" (verifie sur echantillon reel) :
  nblocapt == 1 AND nblocmai == 0 AND nblocact == 0
  -> mutation qui ne porte QUE sur un seul appartement (une dependance
     type cave/garage est acceptee, nblocdep n'est pas filtre). Plus
     strict que nblocapt > 0 pour eviter de meler des ventes en bloc
     (plusieurs apparts d'un coup) qui fausseraient le prix/m2 unitaire.

LIMITE CONNUE ET ASSUMEE (pas cachee) : cette API ne renvoie PAS de
latitude/longitude dans /dvf_opendata/mutations/ (verifie sur les deux
echantillons -- absent des deux). Les lignes inserees par ce script ont
donc latitude/longitude = NULL. Ce n'est PAS bloquant aujourd'hui : ni
get_prix_m2() ni v_prix_marche_appartements (agregats par commune) dans
l'etat actuel du pipeline n'utilisent les coordonnees par transaction.
Si un usage futur (ex: modele spatial SAR/GWR reel, roadmap Section VII,
non construit a ce stade) en a besoin, il faudra un appel complementaire
a /dvf_opendata/geomutations/ (GeoJSON) -- non fait ici, hors perimetre
de cette livraison, a traiter separement si besoin.

PREREQUIS :
  - Avoir execute db/003_dvf_opendata_source.sql dans Supabase d'abord.
  - Variables d'environnement (A NE JAMAIS COMMITER DANS GIT) :
      SUPABASE_URL
      SUPABASE_SERVICE_ROLE_KEY

USAGE :
  export SUPABASE_URL="https://xxxxx.supabase.co"
  export SUPABASE_SERVICE_ROLE_KEY="eyJ..."
  python scripts/sync_dvf_opendata.py --dry-run   # verifie sans ecrire
  python scripts/sync_dvf_opendata.py             # upload reel

Auteur : Steelldy SAS - Juillet 2026
"""

import os
import sys
import time
import argparse
import logging
from pathlib import Path
from typing import Optional

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger("sync_dvf_opendata")

ZONE_ID = "gex_001"
API_BASE = "https://apidf-preprod.cerema.fr"
MUTATIONS_ENDPOINT = f"{API_BASE}/dvf_opendata/mutations/"

COMMUNES_GEX = {
    "01071": "Cessy",
    "01160": "Ferney-Voltaire",
    "01173": "Gex",
    "01313": "Prevessin-Moens",
    "01281": "Ornex",
    "01354": "Saint-Genis-Pouilly",
    "01401": "Sergy",
    "01419": "Thoiry",
}

PAGE_SIZE = 500
REQUEST_TIMEOUT_S = 60  # AUGMENTE le 16/07/2026 (etait 30s) -- voir MAX_RETRIES ci-dessous
SLEEP_BETWEEN_PAGES_S = 0.3  # etre poli avec une API publique gratuite
ANNEEMUT_MIN = 2014
BATCH_SIZE = 200  # pour l'upload Supabase, meme convention que l'ancien script

# AJOUTE le 16/07/2026 : deux echecs reproductibles en conditions reelles
# (ReadTimeout apres 30s sur apidf-preprod.cerema.fr, deux runs consecutifs
# du 16/07/2026, cf. GitHub Actions run #29509158572) ont montre que cette
# API "preprod" (donc par nature moins stable qu'un environnement de
# production) peut etre lente ou momentanement indisponible. Comme ce
# script tourne sans supervision humaine (2 fois par an via dvf-reminder.yml),
# on ajoute un retry avec backoff exponentiel plutot que de laisser un simple
# ralentissement reseau faire echouer toute la synchronisation.
MAX_RETRIES = 3
RETRY_BACKOFF_BASE_S = 5


def _get_with_retry(url: str, params: Optional[dict], timeout: int) -> "requests.Response":
    """GET avec retry (backoff exponentiel : 5s, 10s, 20s) sur timeout ou
    erreur de connexion. Voir commentaire MAX_RETRIES ci-dessus pour le
    contexte (echecs reproductibles observes le 16/07/2026)."""
    last_exc: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_exc = e
            if attempt < MAX_RETRIES:
                wait_s = RETRY_BACKOFF_BASE_S * (2 ** (attempt - 1))
                log.warning(f"  Tentative {attempt}/{MAX_RETRIES} echouee ({e.__class__.__name__}: {e}), nouvel essai dans {wait_s}s...")
                time.sleep(wait_s)
            else:
                log.error(f"  Echec definitif apres {MAX_RETRIES} tentatives : {e}")
    assert last_exc is not None
    raise last_exc


def fetch_commune_mutations(code_insee: str) -> list[dict]:
    """Recupere TOUTES les mutations (paginees) pour une commune, filtrees
    server-side sur idnatmut=1 (Vente, verifie sur echantillon reel) et
    anneemut_min=2014. Le filtre "appartement pur" est fait client-side
    (voir is_pure_apartment_mutation) car l'API ne permet pas de filtrer
    directement sur nblocapt/nblocmai/nblocact."""
    results = []
    url = MUTATIONS_ENDPOINT
    params = {
        "code_insee": code_insee,
        "fields": "all",
        "page_size": PAGE_SIZE,
        "idnatmut": 1,  # Vente -- verifie idnatmut=1 <-> libnatmut="Vente" sur 2 echantillons reels
        "anneemut_min": ANNEEMUT_MIN,
    }
    page_count = 0
    while url:
        resp = _get_with_retry(url, params if page_count == 0 else None, REQUEST_TIMEOUT_S)
        body = resp.json()
        batch = body.get("results", [])
        results.extend(batch)
        page_count += 1
        log.info(f"  commune {code_insee} : page {page_count}, +{len(batch)} mutations (total {len(results)}/{body.get('count', '?')})")
        url = body.get("next")
        if url:
            time.sleep(SLEEP_BETWEEN_PAGES_S)
    return results


def is_pure_apartment_mutation(r: dict) -> bool:
    """Mutation qui ne porte QUE sur un seul appartement (dependance type
    cave/garage acceptee). Verifie sur echantillon reel Ferney-Voltaire
    (idmutation=5852) : nblocmai=0, nblocapt=1, nblocact=0, nblocdep=1,
    codtypbien="121"/"UN APPARTEMENT" -- exactement ce filtre."""
    try:
        return (
            int(r.get("nblocapt", 0)) == 1
            and int(r.get("nblocmai", 0)) == 0
            and int(r.get("nblocact", 0)) == 0
        )
    except (TypeError, ValueError):
        return False


def extract_nombre_pieces(r: dict) -> Optional[int]:
    """Deduit le nombre de pieces depuis nbapt1pp..nbapt5pp (comptage par
    tranche de pieces pour les appartements de la mutation). Comme on a
    deja filtre nblocapt==1, exactement une tranche doit valoir 1.
    "5" signifie "5 pieces ou plus" (convention Cerema standard, comme DVF)."""
    for n in range(1, 6):
        key = f"nbapt{n}pp"
        try:
            if int(float(r.get(key, 0) or 0)) >= 1:
                return n
        except (TypeError, ValueError):
            continue
    return None


def to_biens_row(r: dict, code_insee: str) -> Optional[dict]:
    """Convertit une mutation DVF+ open-data en ligne pour la table `biens`.
    Retourne None si la ligne ne passe pas les filtres de coherence
    (memes seuils que l'ancien pipeline CSV : valeur>50k, surface>10,
    prix_m2 entre 1000 et 20000 -- pour rester comparable)."""
    idopendata = r.get("idopendata") or r.get("idmutinvar")
    if not idopendata:
        log.warning(f"Mutation sans idopendata/idmutinvar, ignoree : idmutation={r.get('idmutation')}")
        return None

    try:
        valeur_fonciere = float(r["valeurfonc"])
        surface_reelle_bati = float(r["sbatapt"])
    except (KeyError, TypeError, ValueError) as e:
        log.warning(f"Mutation {idopendata} : champ valeurfonc/sbatapt manquant ou invalide ({e}), ignoree")
        return None

    if surface_reelle_bati <= 0:
        log.warning(f"Mutation {idopendata} : sbatapt <= 0 malgre nblocapt==1, ignoree (donnee incoherente)")
        return None

    if not (valeur_fonciere > 50000 and surface_reelle_bati > 10):
        return None

    prix_m2 = round(valeur_fonciere / surface_reelle_bati, 2)
    if not (1000 < prix_m2 < 20000):
        return None

    l_idpar = r.get("l_idpar") or []

    return {
        "zone_id": ZONE_ID,
        "idopendata": str(idopendata),
        "id_mutation": str(idopendata),  # retro-compatibilite : meme valeur que idopendata
        "numero_disposition": None,      # concept inexistant dans cette source (deja agregee)
        "id_parcelle": l_idpar[0] if l_idpar else None,
        "date_mutation": r.get("datemut"),
        "code_commune": code_insee,
        "nom_commune": COMMUNES_GEX.get(code_insee, code_insee),
        "type_local": "Appartement",
        "surface_reelle_bati": surface_reelle_bati,
        "nombre_pieces": extract_nombre_pieces(r),
        "valeur_fonciere": valeur_fonciere,
        "prix_m2": prix_m2,
        "latitude": None,   # non fourni par /dvf_opendata/mutations/ -- voir limite documentee en tete de fichier
        "longitude": None,  # idem
        "source_pipeline": "dvf_opendata_cerema",
    }


def upload_to_supabase(rows: list[dict], supabase_url: str, service_key: str) -> None:
    endpoint = f"{supabase_url.rstrip('/')}/rest/v1/biens"
    headers = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    params = {"on_conflict": "idopendata"}

    total = len(rows)
    for i in range(0, total, BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        resp = requests.post(endpoint, headers=headers, params=params, json=batch, timeout=30)
        if resp.status_code not in (200, 201, 204):
            log.error(f"Echec batch {i}-{i+len(batch)} : {resp.status_code} {resp.text[:500]}")
            resp.raise_for_status()
        log.info(f"Batch {i}-{i+len(batch)}/{total} envoye ({resp.status_code})")

    log.info(f"Upload termine : {total} transactions envoyees vers Supabase (source dvf_opendata_cerema).")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Prepare les donnees sans ecrire dans Supabase")
    args = parser.parse_args()

    all_rows = []
    stats = {"fetched": 0, "pure_apartment": 0, "kept_after_sanity_filters": 0}

    for code_insee, nom in COMMUNES_GEX.items():
        log.info(f"Commune {code_insee} ({nom}) ...")
        mutations = fetch_commune_mutations(code_insee)
        stats["fetched"] += len(mutations)

        apts = [m for m in mutations if is_pure_apartment_mutation(m)]
        stats["pure_apartment"] += len(apts)

        for m in apts:
            row = to_biens_row(m, code_insee)
            if row is not None:
                all_rows.append(row)
        log.info(f"  {nom} : {len(mutations)} mutations Vente >= {ANNEEMUT_MIN} -> {len(apts)} appartements purs")

    stats["kept_after_sanity_filters"] = len(all_rows)

    # Dedup defensif avant upload (l'API ne devrait pas renvoyer de doublons,
    # mais on ne fait jamais confiance a une source externe sans verifier --
    # meme principe que le controle de doublons de l'ancien script).
    seen = set()
    deduped = []
    dup_count = 0
    for row in all_rows:
        key = row["idopendata"]
        if key in seen:
            dup_count += 1
            continue
        seen.add(key)
        deduped.append(row)
    if dup_count:
        log.warning(f"{dup_count} doublons idopendata detectes et retires avant upload")

    log.info(
        f"Resume : {stats['fetched']} mutations recuperees, "
        f"{stats['pure_apartment']} appartements purs (nblocapt=1,nblocmai=0,nblocact=0), "
        f"{stats['kept_after_sanity_filters']} apres filtres de coherence, "
        f"{len(deduped)} lignes finales apres dedup."
    )

    if args.dry_run:
        log.info(f"[DRY-RUN] {len(deduped)} lignes pretes, aucune ecriture effectuee.")
        if deduped:
            log.info(f"[DRY-RUN] Exemple de ligne : {deduped[0]}")
        return 0

    if not deduped:
        log.warning("Aucune ligne a envoyer -- verifier les filtres ou la disponibilite de l'API avant de s'inquieter.")
        return 0

    supabase_url = os.environ.get("SUPABASE_URL")
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not supabase_url or not service_key:
        log.error("Variables SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY manquantes.")
        return 1

    upload_to_supabase(deduped, supabase_url, service_key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
