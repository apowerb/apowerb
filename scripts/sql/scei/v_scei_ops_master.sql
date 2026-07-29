-- Vue maître Ops SCEI : 1 ligne = 1 AR métier (dbo.Commandes)
--
-- Versionnée 2026-05-20 (PR 3c.1) après audit live SCEI_PROD : la vue
-- vivait directement sur prod sans script source dans le repo. La
-- définition initiale (du 12/05, cf project_scei_dashboard_master_view)
-- a été dumpée depuis ``sys.sql_modules`` et reproduite à l'identique
-- ci-dessous.
--
-- Le seul ajout de cette PR : ``c.WebhookLogId`` au SELECT, pour que
-- le dashboard puisse construire l'URL ``/api/webhooks/logs/<id>/
-- attachments/<filename>`` et afficher le bouton "Voir PJ".
-- Tout le reste est inchangé (mêmes colonnes, mêmes jointures, mêmes
-- agrégats LignesCommande).
--
-- Idempotent via ``CREATE OR ALTER VIEW`` (SQL Server 2016+, supporté
-- sur SuiviAR).

CREATE OR ALTER VIEW dbo.v_scei_ops_master AS
SELECT
    c.ID, c.ID AS mail_uid,
    c.NumeroCommande, c.Societe, c.FournisseurCode, c.FournisseurNom,
    c.Commanditaire, c.EmailExpediteur, c.DateCommande, c.DateReceptionAR,
    CAST(c.DateReceptionAR AT TIME ZONE 'UTC' AT TIME ZONE 'Romance Standard Time' AS DATE) AS jour_reception,
    c.StatutGlobal, c.Traite, c.TraiteLe, c.Decision,
    DATEDIFF(MINUTE, c.DateReceptionAR, ISNULL(c.TraiteLe, SYSUTCDATETIME())) AS age_minutes,
    DATEDIFF(MINUTE, c.DateReceptionAR, c.TraiteLe) AS cycle_minutes,
    CASE WHEN c.StatutGlobal IN ('conforme','non_conforme','non_rapproche') THEN 1 ELSE 0 END AS is_classified,
    CASE WHEN c.StatutGlobal='conforme' THEN 1 ELSE 0 END AS is_conforme,
    CASE WHEN c.StatutGlobal='non_conforme' THEN 1 ELSE 0 END AS is_non_conforme,
    CASE WHEN c.StatutGlobal IN ('non_rapproche') THEN 1 ELSE 0 END AS is_non_rapproche,
    CASE WHEN c.StatutGlobal IN ('non_conforme','non_rapproche') THEN 1 ELSE 0 END AS is_anomalie,
    CASE WHEN c.Traite=0 AND DATEDIFF(MINUTE,c.DateReceptionAR,SYSUTCDATETIME())>60 THEN 1 ELSE 0 END AS is_backlog,
    ISNULL(lc.nb_lignes_ecart,0) AS nb_lignes_ecart,
    ISNULL(lc.nb_ecart_prix,0) AS nb_ecart_prix,
    ISNULL(lc.nb_ecart_qte,0) AS nb_ecart_qte,
    ISNULL(lc.nb_ecart_date,0) AS nb_ecart_date,
    ISNULL(lc.nb_ligne_absente_erp,0) AS nb_ligne_absente_erp,
    -- PR 3c.1 (2026-05-20): expose la FK vers webhook_logs (Postgres)
    -- pour permettre l'action "Voir PJ" depuis le dashboard. NULL pour
    -- tout AR antérieur au 20/05 (capture body+PJ démarrée à cette date).
    c.WebhookLogId
FROM dbo.Commandes c
LEFT JOIN (
    SELECT NumeroCommande, Societe,
        COUNT(*) AS nb_lignes_ecart,
        SUM(CASE WHEN TypeEcart='ecart_prix' THEN 1 ELSE 0 END) AS nb_ecart_prix,
        SUM(CASE WHEN TypeEcart='ecart_qte'  THEN 1 ELSE 0 END) AS nb_ecart_qte,
        SUM(CASE WHEN TypeEcart='ecart_date' THEN 1 ELSE 0 END) AS nb_ecart_date,
        SUM(CASE WHEN TypeEcart IN ('ligne_absente_erp','LINE_NOT_IN_PO') THEN 1 ELSE 0 END) AS nb_ligne_absente_erp
    FROM dbo.LignesCommande
    WHERE TypeEcart IS NOT NULL
    GROUP BY NumeroCommande, Societe
) lc ON lc.NumeroCommande=c.NumeroCommande AND lc.Societe=c.Societe;
GO
