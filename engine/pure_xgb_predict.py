"""
engine/pure_xgb_predict.py — Reimplementation MINIMALE, pure Python + stdlib
json (AUCUNE dependance xgboost/numpy a l'execution) du calcul de
xgb.Booster.predict() pour un modele "gbtree" / objective "reg:squarederror",
tel qu'exporte par xgb.Booster.save_model(path.json) (format JSON natif
XGBoost >= 1.6).

CONTEXTE (22/07/2026) : le premier deploiement du signal secondaire XGBoost
(package xgboost==3.2.0 dans api/requirements.txt) a fait exploser la taille
de la fonction serverless Vercel a 846.58 MB (limite : 500 MB) -> ECHEC DE
BUILD sur tous les deploiements suivants, et un hotfix d'urgence (retrait de
xgboost des requirements) a du etre pousse pour restaurer le service (voir
scripts/train_xgboost_divergence.py et le rapport livre a Helen le
22/07/2026 pour le detail complet de l'incident). Cette reimplementation
resout le probleme a la racine : le modele n'a besoin d'etre inference qu'a
la prediction (arithmetique triviale sur des arbres de decision deja
entraines), pas besoin d'embarquer tout le moteur d'entrainement C++
(libxgboost.so, ~300+ MB installes) pour ca.

Cette reimplementation ne sert QUE pour la prediction en production ;
l'entrainement (scripts/train_xgboost_divergence.py, execute localement/hors
Vercel) continue d'utiliser le vrai package xgboost.

VALIDATION (effectuee avant deploiement, voir scripts/validate_pure_xgb.py) :
comparee a xgb.Booster.predict() reel sur 706 lignes d'entrainement + 2003
vecteurs synthetiques/limites -> correspondance quasi-exacte : max abs
diff = 0.0041 EUR/m2, max diff relative = 1.0e-6 (arrondi float32 interne a
XGBoost, sans consequence sur un seuil de divergence exprime en %).
"""
import json


class PureXGBRegressor:
    """Modele gbtree reg:squarederror, format JSON natif XGBoost (>=1.6)."""

    def __init__(self, model_path: str):
        with open(model_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        learner = raw["learner"]
        self.feature_names = learner["feature_names"]
        self.n_features = len(self.feature_names)
        # base_score est serialise comme une chaine "[4.8085664E3]"
        base_score_raw = learner["learner_model_param"]["base_score"]
        self.base_score = float(base_score_raw.strip("[]"))
        gb = learner["gradient_booster"]
        if gb["name"] != "gbtree":
            raise ValueError(f"booster non supporte : {gb['name']!r} (attendu 'gbtree')")
        obj_name = learner["objective"]["name"]
        if obj_name != "reg:squarederror":
            raise ValueError(f"objectif non supporte : {obj_name!r} (attendu 'reg:squarederror')")
        model = gb["model"]
        gbtree_param = model["gbtree_model_param"]
        if int(gbtree_param["num_parallel_tree"]) != 1:
            raise ValueError("num_parallel_tree != 1 non supporte (pas notre cas d'usage)")
        self.trees = []
        for t in model["trees"]:
            self.trees.append({
                "left_children": t["left_children"],
                "right_children": t["right_children"],
                "split_indices": t["split_indices"],
                "split_conditions": t["split_conditions"],
                "default_left": t["default_left"],
            })
        self.num_trees = len(self.trees)

    def _predict_one_tree(self, tree: dict, x: list) -> float:
        node = 0
        left = tree["left_children"]
        right = tree["right_children"]
        split_idx = tree["split_indices"]
        split_cond = tree["split_conditions"]
        default_left = tree["default_left"]
        while left[node] != -1:  # -1 == feuille (right[node] est aussi -1 en pratique)
            fidx = split_idx[node]
            fval = x[fidx]
            if fval is None:
                # valeur manquante : suit default_left (comportement XGBoost natif)
                node = left[node] if default_left[node] else right[node]
            elif fval < split_cond[node]:
                node = left[node]
            else:
                node = right[node]
        return split_cond[node]  # a une feuille, split_conditions == valeur de la feuille

    def predict_one(self, feats: list) -> float:
        """feats : liste ordonnee selon self.feature_names (14 valeurs)."""
        if len(feats) != self.n_features:
            raise ValueError(f"attendu {self.n_features} features, recu {len(feats)}")
        total = self.base_score
        for tree in self.trees:
            total += self._predict_one_tree(tree, feats)
        return total
