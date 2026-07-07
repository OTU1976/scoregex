import pandas as pd
import json
import os

COMMUNES_GEX = {
    "01071": "Cessy",
    "01160": "Ferney-Voltaire",
    "01173": "Gex",
    "01313": "Prevessin-Moens",
    "01281": "Ornex",
    "01354": "Saint-Genis-Pouilly",
    "01401": "Sergy",
    "01419": "Thoiry"
}

data_dir = "data"
all_dfs = []

print("Chargement des 8 fichiers DVF Pays de Gex...")

for code, nom in COMMUNES_GEX.items():
    filepath = os.path.join(data_dir, f"{code}.csv")
    if os.path.exists(filepath):
        df = pd.read_csv(filepath, sep=",", low_memory=False)
        df["commune"] = nom
        df["code_insee"] = code
        all_dfs.append(df)
        print(f"  OK {nom} ({code}) : {len(df)} transactions")
    else:
        print(f"  MANQUANT : {filepath}")

if not all_dfs:
    print("ERREUR : aucun fichier trouve dans data/")
    exit(1)

df = pd.concat(all_dfs, ignore_index=True)
print(f"\nTotal : {len(df)} transactions")
print(f"Colonnes disponibles : {list(df.columns)}")

prix_cols = [c for c in df.columns if "valeur" in c.lower() or "prix" in c.lower()]
surface_cols = [c for c in df.columns if "surface" in c.lower()]
print(f"Colonnes prix : {prix_cols}")
print(f"Colonnes surface : {surface_cols}")

if "nature_mutation" in df.columns:
    df = df[df["nature_mutation"] == "Vente"]

if prix_cols and surface_cols:
    col_prix = prix_cols[0]
    col_surface = surface_cols[0]
    df[col_prix] = pd.to_numeric(df[col_prix], errors="coerce")
    df[col_surface] = pd.to_numeric(df[col_surface], errors="coerce")
    df = df[(df[col_prix] > 50000) & (df[col_surface] > 10)]
    df["prix_m2"] = df[col_prix] / df[col_surface]
    df = df[(df["prix_m2"] > 1000) & (df["prix_m2"] < 20000)]

    resultats = {}
    for code, nom in COMMUNES_GEX.items():
        subset = df[df["code_insee"] == code]
        if len(subset) > 0:
            resultats[code] = {
                "commune": nom,
                "prix_m2_median": round(subset["prix_m2"].median(), 0),
                "prix_m2_moyen": round(subset["prix_m2"].mean(), 0),
                "nb_transactions": len(subset)
            }
            print(f"  {nom} : {resultats[code]['prix_m2_median']} EUR/m2 ({len(subset)} ventes)")

    with open("data/prix_gex.json", "w", encoding="utf-8") as f:
        json.dump(resultats, f, ensure_ascii=False, indent=2)
    print("\nSauvegarde : data/prix_gex.json OK")
