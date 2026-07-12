-- =============================================================================
-- GexScore -- db/004_estimations_sauvegardees.sql
-- Table pour le Dashboard : estimations sauvegardees par utilisateur,
-- protegees par Supabase Auth (auth.uid()) + Row Level Security.
--
-- ⚠️ DEJA EXECUTEE EN DIRECT sur le projet SCOREGEX (gxisdfjfbmuajaotltjl)
-- le 12/07/2026 via le connecteur Supabase MCP. Ce fichier est fourni pour
-- ta documentation / control de version (le coller dans le repo Git), PAS
-- a re-executer -- il est idempotent (IF NOT EXISTS partout) donc le
-- rejouer ne casserait rien, mais ce n'est pas necessaire.
--
-- Authentification : contrairement a la toute premiere version de ce
-- fichier (jamais deployee), celle-ci utilise Supabase Auth reel. Un
-- utilisateur cree un compte (email + mot de passe) depuis l'app, recoit
-- un JWT, et la RLS (auth.uid() = user_id) garantit qu'il ne voit et ne
-- modifie QUE ses propres lignes. Aucun acces anonyme.
-- =============================================================================

CREATE TABLE IF NOT EXISTS estimations_sauvegardees (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL DEFAULT auth.uid() REFERENCES auth.users(id) ON DELETE CASCADE,
    commune             VARCHAR(100),
    adresse             VARCHAR(255),
    lat                 DOUBLE PRECISION,
    lon                 DOUBLE PRECISION,
    surface_m2          NUMERIC,
    dpe_note            VARCHAR(1),
    prix_estime_eur     NUMERIC,
    prix_m2_estime      NUMERIC,
    gexscore            NUMERIC,
    grade               VARCHAR(5),
    prix_annonce_eur    NUMERIC,
    is_deal             BOOLEAN,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE estimations_sauvegardees IS
    'Estimations sauvegardees depuis le Dashboard, une ligne par bien. '
    'Isolation par utilisateur via user_id + RLS (auth.uid()) -- '
    'authentification Supabase Auth reelle.';

CREATE INDEX IF NOT EXISTS idx_estimations_user_id
    ON estimations_sauvegardees (user_id);

CREATE INDEX IF NOT EXISTS idx_estimations_created_at
    ON estimations_sauvegardees (created_at DESC);

ALTER TABLE estimations_sauvegardees ENABLE ROW LEVEL SECURITY;

-- (select auth.uid()) plutot que auth.uid() nu : recommandation officielle
-- Supabase pour eviter une reevaluation par ligne (lint de performance
-- "auth_rls_initplan"), trouve et corrige lors du deploiement direct.

DROP POLICY IF EXISTS select_own_estimations ON estimations_sauvegardees;
CREATE POLICY select_own_estimations
    ON estimations_sauvegardees
    FOR SELECT
    TO authenticated
    USING ((select auth.uid()) = user_id);

DROP POLICY IF EXISTS insert_own_estimations ON estimations_sauvegardees;
CREATE POLICY insert_own_estimations
    ON estimations_sauvegardees
    FOR INSERT
    TO authenticated
    WITH CHECK ((select auth.uid()) = user_id);

DROP POLICY IF EXISTS delete_own_estimations ON estimations_sauvegardees;
CREATE POLICY delete_own_estimations
    ON estimations_sauvegardees
    FOR DELETE
    TO authenticated
    USING ((select auth.uid()) = user_id);

DO $$
DECLARE
    n_total INT;
BEGIN
    SELECT COUNT(*) INTO n_total FROM estimations_sauvegardees;
    RAISE NOTICE 'Migration 004 terminee. Lignes existantes : %', n_total;
END $$;
