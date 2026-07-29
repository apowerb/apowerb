-- Vue maître Ops SCEI : 1 ligne = 1 ligne de LignesCommande
--
-- Versionnée 2026-05-20 (PR 3c.1) — même histoire que v_scei_ops_master :
-- la définition initiale vivait sur SCEI_PROD sans script source. Dump
-- depuis sys.sql_modules + ajout de ``c.WebhookLogId``.
--
-- ``c.WebhookLogId`` est dupliqué sur chaque ligne d'une même commande
-- (toutes les lignes d'un AR partagent le même webhook d'origine). Le
-- dashboard expose le bouton "Voir PJ" en bout de chaque ligne ; comme
-- toutes les lignes d'un AR pointent vers le même log_id, c'est cohérent.

CREATE OR ALTER VIEW dbo.v_scei_ops_master_lignes AS
SELECT
    l.ID, l.NumeroCommande, l.Societe, l.NumeroLigne,
    l.Reference, l.Quantite, l.Prix, l.Ecart, l.Situation,
    l.RefFournisseur, l.QuantiteAR, l.QuantiteERP,
    l.PrixAR, l.PrixERP, l.DateLivraisonAR, l.DateLivraisonERP,
    CASE WHEN l.TypeEcart='LINE_NOT_IN_PO' THEN 'ligne_absente_erp' ELSE l.TypeEcart END AS TypeEcart,
    l.DateLigne,
    c.DateReceptionAR, c.StatutGlobal, c.FournisseurNom, c.FournisseurCode, c.Commanditaire,
    -- PR 3c.1 (2026-05-20): FK vers webhook_logs pour action "Voir PJ".
    c.WebhookLogId
FROM dbo.LignesCommande l
INNER JOIN dbo.Commandes c ON c.NumeroCommande=l.NumeroCommande AND c.Societe=l.Societe;
GO
