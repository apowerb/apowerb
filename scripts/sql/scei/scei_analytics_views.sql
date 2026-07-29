-- =============================================================================
-- SuiviAR analytics views - thaink2 x SCEI88
-- =============================================================================
-- 3 vues qui alimentent le dashboard SCEI (volume/conformite/fournisseurs/cycle).
-- Idempotent : IF OBJECT_ID DROP + CREATE.
-- A executer sur SQL Server SCEI (192.168.1.205) base SuiviAR.
-- =============================================================================

USE SuiviAR;
GO

-- -----------------------------------------------------------------------------
-- v_suiviar_daily : pilotage quotidien (volume, conformite, statuts)
-- -----------------------------------------------------------------------------
IF OBJECT_ID('dbo.v_suiviar_daily', 'V') IS NOT NULL DROP VIEW dbo.v_suiviar_daily;
GO

CREATE VIEW dbo.v_suiviar_daily AS
SELECT
    CAST(DateReceptionAR AS DATE)                                                        AS jour,
    COUNT(*)                                                                             AS nb_ars,
    SUM(CASE WHEN StatutGlobal = 'OK'                 THEN 1 ELSE 0 END)                 AS nb_ok,
    SUM(CASE WHEN StatutGlobal = 'NON_CONFORME'       THEN 1 ELSE 0 END)                 AS nb_non_conforme,
    SUM(CASE WHEN StatutGlobal = 'NEEDS_HUMAN_REVIEW' THEN 1 ELSE 0 END)                 AS nb_human_review,
    SUM(CASE WHEN StatutGlobal = 'en_attente'         THEN 1 ELSE 0 END)                 AS nb_en_attente,
    SUM(CASE WHEN Traite = 1                          THEN 1 ELSE 0 END)                 AS nb_traites,
    CAST(
        100.0 * SUM(CASE WHEN StatutGlobal = 'OK' THEN 1 ELSE 0 END)
        / NULLIF(COUNT(*), 0) AS DECIMAL(5,2)
    )                                                                                    AS pct_conformite
FROM dbo.Commandes
WHERE DateReceptionAR IS NOT NULL
GROUP BY CAST(DateReceptionAR AS DATE);
GO

-- -----------------------------------------------------------------------------
-- v_suiviar_top_fournisseurs : agregat par fournisseur + breakdown ecarts lignes
-- -----------------------------------------------------------------------------
IF OBJECT_ID('dbo.v_suiviar_top_fournisseurs', 'V') IS NOT NULL DROP VIEW dbo.v_suiviar_top_fournisseurs;
GO

CREATE VIEW dbo.v_suiviar_top_fournisseurs AS
SELECT
    c.FournisseurCode,
    c.FournisseurNom,
    COUNT(DISTINCT c.ID)                                                                 AS nb_ars,
    SUM(CASE WHEN c.StatutGlobal = 'OK'              THEN 1 ELSE 0 END)                  AS nb_ok,
    SUM(CASE WHEN c.StatutGlobal = 'NON_CONFORME'    THEN 1 ELSE 0 END)                  AS nb_non_conforme,
    SUM(CASE WHEN c.StatutGlobal NOT IN ('OK', 'en_attente') THEN 1 ELSE 0 END)          AS nb_anomalies,
    CAST(
        100.0 * SUM(CASE WHEN c.StatutGlobal NOT IN ('OK', 'en_attente') THEN 1 ELSE 0 END)
        / NULLIF(COUNT(DISTINCT c.ID), 0) AS DECIMAL(5,2)
    )                                                                                    AS pct_anomalie,
    SUM(CASE WHEN lc.TypeEcart = 'PRICE_MISMATCH' THEN 1 ELSE 0 END)                     AS nb_ecart_prix,
    SUM(CASE WHEN lc.TypeEcart = 'QTY_MISMATCH'   THEN 1 ELSE 0 END)                     AS nb_ecart_qte,
    SUM(CASE WHEN lc.TypeEcart = 'DATE_MISMATCH'  THEN 1 ELSE 0 END)                     AS nb_ecart_delai,
    SUM(CASE WHEN lc.TypeEcart = 'LINE_NOT_IN_PO' THEN 1 ELSE 0 END)                     AS nb_line_not_in_po,
    SUM(CASE WHEN lc.TypeEcart = 'LINE_MISSING_ON_AR' THEN 1 ELSE 0 END)                 AS nb_line_missing_on_ar
FROM dbo.Commandes c
LEFT JOIN dbo.LignesCommande lc
       ON lc.NumeroCommande = c.NumeroCommande
      AND ISNULL(lc.Societe, '') = ISNULL(c.Societe, '')
WHERE c.FournisseurNom IS NOT NULL
GROUP BY c.FournisseurCode, c.FournisseurNom;
GO

-- -----------------------------------------------------------------------------
-- v_suiviar_cycle_time : temps de cycle reception -> traitement (en minutes)
-- -----------------------------------------------------------------------------
IF OBJECT_ID('dbo.v_suiviar_cycle_time', 'V') IS NOT NULL DROP VIEW dbo.v_suiviar_cycle_time;
GO

CREATE VIEW dbo.v_suiviar_cycle_time AS
SELECT
    ID,
    NumeroCommande,
    Societe,
    FournisseurCode,
    FournisseurNom,
    Commanditaire,
    DateReceptionAR,
    TraiteLe,
    StatutGlobal,
    Traite,
    -- PR 3c.1 (2026-05-20): FK vers webhook_logs pour le bouton "Voir PJ"
    -- du dashboard. NULL pour les ARs antérieurs au 20/05 (date de
    -- démarrage de la capture body+PJ, cf PR #188).
    WebhookLogId,
    CASE
        WHEN Traite = 1 AND TraiteLe IS NOT NULL
            THEN CAST(DATEDIFF(SECOND, DateReceptionAR, TraiteLe) AS DECIMAL(18,2)) / 60.0
        ELSE NULL
    END                                                                                  AS cycle_minutes,
    CAST(DATEDIFF(SECOND, DateReceptionAR, SYSUTCDATETIME()) AS DECIMAL(18,2)) / 60.0    AS age_minutes,
    -- Lisible: '5j 12h' / '23h 6m' / '45m'
    CASE
        WHEN DATEDIFF(MINUTE, DateReceptionAR, SYSUTCDATETIME()) >= 1440
            THEN CAST(DATEDIFF(MINUTE, DateReceptionAR, SYSUTCDATETIME()) / 1440 AS VARCHAR(10)) + 'j '
               + CAST((DATEDIFF(MINUTE, DateReceptionAR, SYSUTCDATETIME()) % 1440) / 60 AS VARCHAR(10)) + 'h'
        WHEN DATEDIFF(MINUTE, DateReceptionAR, SYSUTCDATETIME()) >= 60
            THEN CAST(DATEDIFF(MINUTE, DateReceptionAR, SYSUTCDATETIME()) / 60 AS VARCHAR(10)) + 'h '
               + CAST(DATEDIFF(MINUTE, DateReceptionAR, SYSUTCDATETIME()) % 60 AS VARCHAR(10)) + 'm'
        ELSE CAST(DATEDIFF(MINUTE, DateReceptionAR, SYSUTCDATETIME()) AS VARCHAR(10)) + 'm'
    END                                                                                  AS age_label
FROM dbo.Commandes
WHERE DateReceptionAR IS NOT NULL;
GO
