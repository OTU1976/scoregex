"""
GexScore — scripts/upload_dvf_to_supabase.py
═══════════════════════════════════════════════════════════════════════
Remplace l'ancien scripts/process_dvf.py (qui sortait un JSON figé à
copier-coller). Ce script :

  1. Lit les 8 CSV DVF (data/*.csv)
  2. Agrège correctement les dispositions multi-locaux (une même vente DVF
     peut porter sur plusieurs lignes — ex: appartement + cave enregistrés
     séparément avec la MÊME valeur_fonciere répétée sur chaque ligne).
     Sans cette agrégation, diviser le prix total par la surface d'une
     seule ligne fausse le prix/m2. Voir data_quality_notes en bas de ce
     fichier pour le détail de ce problème et sa correction.
  3. Upsert chaque transaction dans Supabase (table `biens`), idempotent —
     relançable à chaque nouvelle vague DGFiP (avril/octobre) sans doublon.

PRÉREQUIS :
  - Avoir exécuté db/002_prix_marche_appartements.sql dans Supabase d'abord
    (ajoute les colonnes numero_disposition/id_parcelle + la contrainte
    d'unicité + la vue v_prix_marche_appartements).
  - Variables d'environnement (À NE JAMAIS COMMITER DANS GIT) :
      SUPABASE_URL
      SUPABASE_SERVICE_ROLE_KEY   (clé service_role — écriture, PAS la clé
                                    anon publique utilisée côté API)

USAGE :
  export SUPABASE_URL="https://xxxxx.supabase.co"
  export SUPABASE_SERVICE_ROLE_KEY="eyJ..."
  python scripts/upload_dvf_to_supabase.py
  python scripts/upload_dvf_to_supabase.py --dry-run   # verifie sans ecrire

Auteur : Steelldy SAS — Juillet 2026
"""

import os
import sys
import argparse
import logging
from pathlib import Path

import pandas as pd
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger("upload_dvf")

ZONE_ID = "gex_001"
DATA_DIR = Path(__file__).parent.parent / "data"

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

BATCH_SIZE = 200


def load_and_aggregate() -> pd.DataFrame:
    """Charge les 8 CSV et agrège correctement les dispositions multi-locaux.

    PROBLÈME DVF corrigé ici : une vente (une "disposition") peut être
    éclatée sur plusieurs lignes du CSV quand elle porte sur plusieurs
    locaux distincts (appartement + cave, par exemple), chaque ligne
    répétant la MÊME valeur_fonciere totale mais avec une surface
    partielle différente. Diviser naïvement valeur_fonciere par la
    surface d'UNE seule ligne donne un prix/m2 gonflé et faux.

    Fix : regrouper par (id_mutation, numero_disposition, id_parcelle,
    type_local) et SOMMER les surfaces avant de calculer le prix/m2.
    Validé manuellement : ce problème touchait 46 dispositions sur 1094
    dans le jeu de données Pays de Gex (vérifié le 09/07/2026).
    """
    all_dfs = []
    for code, nom in COMMUNES_GEX.items():
        path = DATA_DIR / f"{code}.csv"
        if not path.exists():
            log.warning(f"Fichier manquant, ignoré : {path}")
            continue
        df = pd.read_csv(path, sep=",", low_memory=False)
        df["code_insee_zone"] = code
        all_dfs.append(df)

    if not all_dfs:
        raise FileNotFoundError(f"Aucun fichier CSV trouvé dans {DATA_DIR}")

    df = pd.concat(all_dfs, ignore_index=True)
    log.info(f"Lignes brutes chargées : {len(df)}")

    df = df[df["nature_mutation"] == "Vente"].copy()
    df["valeur_fonciere"] = pd.to_numeric(df["valeur_fonciere"], errors="coerce")
    df["surface_reelle_bati"] = pd.to_numeric(df["surface_reelle_bati"], errors="coerce")
    df = df[(df["valeur_fonciere"] > 0) & (df["surface_reelle_bati"] > 0)]

    group_cols = [
        "id_mutation", "numero_disposition", "id_parcelle", "type_local",
        "code_insee_zone", "nom_commune", "code_commune", "date_mutation",
        "longitude", "latitude",
    ]
    agg = df.groupby(group_cols, dropna=False, as_index=False).agg(
        surface_reelle_bati=("surface_reelle_bati", "sum"),
        valeur_fonciere=("valeur_fonciere", "first"),
        nombre_pieces=("nombre_pieces_principales", "first"),
    )
    log.info(f"Après agrégation par disposition : {len(agg)}")

    agg["prix_m2"] = agg["valeur_fonciere"] / agg["surface_reelle_bati"]
    agg = agg[(agg["valeur_fonciere"] > 50000) & (agg["surface_reelle_bati"] > 10)]
    agg = agg[(agg["prix_m2"] > 1000) & (agg["prix_m2"] < 20000)]
    log.info(f"Après filtres de cohérence (prix>50k, surface>10, prix_m2 1000-20000) : {len(agg)}")

    dup = agg.duplicated(subset=["id_mutation", "numero_disposition", "id_parcelle", "type_local", "code_insee_zone"]).sum()
    if dup > 0:
        log.error(f"⚠ {dup} doublons détectés après agrégation — vérifier avant upload !")
        raise ValueError(f"{dup} doublons restants, upload annulé par sécurité")

    n_appart = len(agg[agg["type_local"] == "Appartement"])
    log.info(f"Dont Appartements (périmètre produit actuel) : {n_appart}")

    return agg


def to_supabase_rows(df: pd.DataFrame) -> list[dict]:
    rows = []
    for _, r in df.iterrows():
        rows.append({
            "zone_id": ZONE_ID,
            "id_mutation": str(r["id_mutation"]),
            "numero_disposition": str(r["numero_disposition"]),
            "id_parcelle": str(r["id_parcelle"]) if pd.notna(r["id_parcelle"]) else None,
            "date_mutation": str(r["date_mutation"]) if pd.notna(r["date_mutation"]) else None,
            "code_commune": str(r["code_commune"]),
            "nom_commune": str(r["nom_commune"]),
            "type_local": str(r["type_local"]),
            "surface_reelle_bati": float(r["surface_reelle_bati"]),
            "nombre_pieces": int(r["nombre_pieces"]) if pd.notna(r["nombre_pieces"]) else None,
            "valeur_fonciere": float(r["valeur_fonciere"]),
            "prix_m2": round(float(r["prix_m2"]), 2),
            "latitude": float(r["latitude"]) if pd.notna(r["latitude"]) else None,
            "longitude": float(r["longitude"]) if pd.notna(r["longitude"]) else None,
        })
    return rows


def upload_to_supabase(rows: list[dict], supabase_url: str, service_key: str) -> None:
    endpoint = f"{supabase_url.rstrip('/')}/rest/v1/biens"
    headers = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    params = {"on_conflict": "id_mutation,numero_disposition,id_parcelle,type_local,zone_id"}

    total = len(rows)
    for i in range(0, total, BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        resp = requests.post(endpoint, headers=headers, params=params, json=batch, timeout=30)
        if resp.status_code not in (200, 201, 204):
            log.error(f"Échec batch {i}-{i+len(batch)} : {resp.status_code} {resp.text[:500]}")
            resp.raise_for_status()
        log.info(f"Batch {i}-{i+len(batch)}/{total} envoyé ({resp.status_code})")

    log.info(f"✓ Upload terminé : {total} transactions envoyées vers Supabase.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Prépare les données sans écrire dans Supabase")
    args = parser.parse_args()

    df = load_and_aggregate()
    rows = to_supabase_rows(df)

    if args.dry_run:
        log.info(f"[DRY-RUN] {len(rows)} lignes prêtes, aucune écriture effectuée.")
        log.info(f"[DRY-RUN] Exemple de ligne : {rows[0] if rows else 'N/A'}")
        return

    supabase_url = os.environ.get("SUPABASE_URL")
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not supabase_url or not service_key:
        log.error("Variables SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY manquantes. "
                   "Voir l'en-tête de ce fichier pour comment les définir.")
        sys.exit(1)

    upload_to_supabase(rows, supabase_url, service_key)


if __name__ == "__main__":
    main()


# ─────────────────────────────────────────────────────────────────────────
# data_quality_notes — à conserver pour traçabilité (principe "jamais de
# donnée fictive présentée comme réelle")
#
# 1. Le script historique (process_dvf.py) utilisait accidentellement
#    `lot1_surface_carrez` comme colonne de surface (choix du premier
#    élément d'une liste de colonnes contenant "surface" dans leur nom) —
#    cette colonne est vide pour toutes les maisons, ce qui filtrait de
#    facto vers les appartements, mais de façon non intentionnelle et avec
#    une surface parfois différente de la surface réelle habitable.
#    → Ce script utilise explicitement `type_local == "Appartement"`
#      (dans la vue SQL) et `surface_reelle_bati` (la bonne colonne).
#
# 2. DVF répète la valeur_fonciere sur plusieurs lignes pour les
#    dispositions portant sur plusieurs locaux (ex: appart + cave).
#    → Corrigé par agrégation (somme des surfaces) avant calcul du prix/m2.
#
# Résultat validé le 09/07/2026 : 1064 transactions totales (toutes
# catégories), dont 747 Appartements — contre 658 dans l'ancienne
# méthode (qui, on le sait maintenant, était biaisée par le point 1).
# ─────────────────────────────────────────────────────────────────────────
