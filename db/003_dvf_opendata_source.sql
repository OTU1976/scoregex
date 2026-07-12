-- =============================================================================
-- GexScore -- db/003_dvf_opendata_source.sql
-- Migration additive : support de la source DVF+ open-data (Cerema/DGALN)
-- en complement de la source DVF brute (DGFiP CSV) existante.
--
-- Pourquoi cette migration :
-- L'ancienne source (data/*.csv -> scripts/upload_dvf_to_supabase.py) fournit
-- des lignes brutes par disposition/parcelle, dedupliquees sur
-- (id_mutation, numero_disposition, id_parcelle, type_local, zone_id).
-- La nouvelle source (API DVF+ open-data du Cerema, endpoint
-- /dvf_opendata/mutations/) fournit des lignes DEJA agregees au niveau de
-- la mutation (une ligne = une transaction complete, un ou plusieurs
-- locaux/parcelles deja sommes cote Cerema). Elle n'a pas de concept de
-- "numero_disposition" ni de parcelle unique -- donc l'ancienne cle
-- d'unicite ne s'applique pas a ces nouvelles lignes.
--
-- Principe : migration PUREMENT ADDITIVE.
--   - Aucune colonne existante n'est supprimee ou renommee.
--   - Aucune contrainte existante n'est supprimee.
--   - La vue v_prix_marche_appartements n'est PAS modifiee : elle continue
--     de fonctionner sans changement car les nouvelles lignes respectent
--     exactement le meme contrat de colonnes (type_local, code_commune,
--     valeur_fonciere, prix_m2, date_mutation, etc.) que les lignes
--     existantes.
--   - Les deux sources peuvent coexister dans la table `biens` sans
--     collision : l'ancienne cle d'unicite continue de proteger les
--     lignes historiques (numero_disposition/id_parcelle renseignes),
--     la nouvelle colonne idopendata + son index unique partiel protegent
--     les nouvelles lignes (numero_disposition NULL).
--
-- A executer APRES 001_init_multitenant.sql et 002_prix_marche_appartements.sql
-- (celle-ci est idempotente -- IF NOT EXISTS partout -- rejouable sans risque).
--
-- Usage :
--   Coller dans l'editeur SQL Supabase, ou :
--   psql -U postgres -d gexscore -f db/003_dvf_opendata_source.sql
--
-- Auteur : Steelldy SAS -- Juillet 2026
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. Nouvelle colonne : identifiant stable de mutation cote DVF+ open-data
--    (champ `idopendata` de l'API Cerema -- identique a `idmutinvar` dans
--    les echantillons verifies -- string hash stable, pas un entier auto-
--    incremente cote Cerema donc pas de risque de collision entre deux
--    mutations differentes).
-- -----------------------------------------------------------------------------

ALTER TABLE biens ADD COLUMN IF NOT EXISTS idopendata VARCHAR(64);

COMMENT ON COLUMN biens.idopendata IS
    'Identifiant de mutation issu de l''API DVF+ open-data du Cerema '
    '(champ idopendata / idmutinvar). NULL pour les lignes historiques '
    'issues du pipeline DVF brut (data/*.csv). Cle d''upsert pour '
    'scripts/sync_dvf_opendata.py.';

-- -----------------------------------------------------------------------------
-- 2. Index unique PARTIEL sur idopendata (uniquement pour les lignes qui
--    l'ont renseigne). Un index partiel plutot qu'une contrainte UNIQUE
--    classique : les lignes historiques ont idopendata = NULL, et NULL
--    n'est jamais egal a NULL en SQL -- une contrainte UNIQUE classique
--    laisserait deja passer un nombre illimite de NULL, donc techniquement
--    un index unique normal fonctionnerait aussi, mais le index PARTIEL
--    (WHERE idopendata IS NOT NULL) est plus explicite sur l'intention et
--    legerement plus efficace (n'indexe pas les lignes historiques).
-- -----------------------------------------------------------------------------

CREATE UNIQUE INDEX IF NOT EXISTS idx_biens_idopendata_unique
    ON biens (idopendata)
    WHERE idopendata IS NOT NULL;

-- -----------------------------------------------------------------------------
-- 3. Rendre numero_disposition et id_parcelle explicitement nullable.
--    Ces colonnes n'existent pas dans le schema de base (001) -- elles ont
--    ete ajoutees par 002_prix_marche_appartements.sql (non disponible en
--    texte dans cette session, cf. note honnete ci-dessous). On applique
--    DROP NOT NULL de maniere defensive et idempotente : si la colonne
--    est deja nullable (ou si NOT NULL n'a jamais ete pose), l'instruction
--    ne fait rien et ne leve pas d'erreur.
--
--    /!\ NOTE DE TRANSPARENCE : je n'ai pas eu acces au texte exact de
--    002_prix_marche_appartements.sql dans cette session (le fichier n'est
--    ni dans le repo GitHub ni dans les documents archives que j'ai pu
--    consulter). Cette section part du principe raisonnable que ces
--    colonnes existent deja (elles sont utilisees par
--    scripts/upload_dvf_to_supabase.py et api/main.py) mais je n'ai pas pu
--    verifier si elles sont NOT NULL. Le bloc DO ci-dessous verifie l'etat
--    reel avant d'agir, donc c'est sans risque dans tous les cas.
-- -----------------------------------------------------------------------------

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'biens' AND column_name = 'numero_disposition'
          AND is_nullable = 'NO'
    ) THEN
        ALTER TABLE biens ALTER COLUMN numero_disposition DROP NOT NULL;
        RAISE NOTICE 'numero_disposition : contrainte NOT NULL retiree';
    ELSE
        RAISE NOTICE 'numero_disposition : deja nullable ou colonne absente, rien a faire';
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'biens' AND column_name = 'id_parcelle'
          AND is_nullable = 'NO'
    ) THEN
        ALTER TABLE biens ALTER COLUMN id_parcelle DROP NOT NULL;
        RAISE NOTICE 'id_parcelle : contrainte NOT NULL retiree';
    ELSE
        RAISE NOTICE 'id_parcelle : deja nullable ou colonne absente, rien a faire';
    END IF;
END $$;

-- -----------------------------------------------------------------------------
-- 4. Colonne de tracabilite : d'ou vient chaque ligne (utile pour debug /
--    audit qualite donnee, et pour un futur nettoyage si jamais on decide
--    de retirer une des deux sources).
-- -----------------------------------------------------------------------------

ALTER TABLE biens ADD COLUMN IF NOT EXISTS source_pipeline VARCHAR(30);

COMMENT ON COLUMN biens.source_pipeline IS
    'Pipeline d''origine de la ligne : ''dvf_csv_dgfip'' (ancien, '
    'upload_dvf_to_supabase.py) ou ''dvf_opendata_cerema'' (nouveau, '
    'sync_dvf_opendata.py). NULL pour les lignes inserees avant cette '
    'colonne (a considerer comme ''dvf_csv_dgfip'' par defaut).';

-- Backfill raisonnable pour les lignes existantes (elles viennent toutes de
-- l'ancien pipeline, ce champ n'existait pas avant) :
UPDATE biens SET source_pipeline = 'dvf_csv_dgfip'
WHERE source_pipeline IS NULL AND idopendata IS NULL;

-- -----------------------------------------------------------------------------
-- 5. Verification finale
-- -----------------------------------------------------------------------------

DO $$
DECLARE
    n_total INT;
    n_legacy INT;
    n_opendata INT;
BEGIN
    SELECT COUNT(*) INTO n_total FROM biens;
    SELECT COUNT(*) INTO n_legacy FROM biens WHERE idopendata IS NULL;
    SELECT COUNT(*) INTO n_opendata FROM biens WHERE idopendata IS NOT NULL;

    RAISE NOTICE 'Migration 003 terminee.';
    RAISE NOTICE '  Lignes totales dans biens : %', n_total;
    RAISE NOTICE '  Lignes source DVF CSV (legacy) : %', n_legacy;
    RAISE NOTICE '  Lignes source DVF+ open-data (nouvelles, attendu 0 avant premier sync) : %', n_opendata;
    RAISE NOTICE '  Prochaine etape : executer scripts/sync_dvf_opendata.py --dry-run';
END $$;
