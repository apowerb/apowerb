-- Trace l'AR métier vers le webhook_log qui l'a produit
--
-- Décision live 2026-05-20 (David) : depuis le dashboard SCEI, l'opérateur
-- doit pouvoir ouvrir la PJ d'une AR dans un nouvel onglet. Le bouton
-- "Voir PJ" lit cette colonne pour construire l'URL
-- /api/webhooks/logs/<id>/attachments/<filename>. PR 3c côté frontend.
--
-- Idempotent (IF NOT EXISTS). ADD COLUMN INT NULL sur SQL Server 2012+
-- est metadata-only (pas de table rewrite). L'index filtré est créé
-- SANS ONLINE=ON car SCEI tourne sur SQL Server Standard Edition
-- (ONLINE=ON est exclusif Enterprise/Azure). Sur 97 lignes le verrou
-- Sch-M dure < 1s, négligeable.
--
-- À exécuter une seule fois sur SuiviAR (SCEI_PROD).

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE Name = N'WebhookLogId'
      AND Object_ID = Object_ID(N'dbo.Commandes')
)
BEGIN
    ALTER TABLE dbo.Commandes ADD WebhookLogId INT NULL;
END;
GO

-- Index filtré : 7000+ lignes attendues à terme, on ne veut pas scanner
-- celles qui resteront NULL (= ARs antérieurs au 20/05).
IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = N'IX_Commandes_WebhookLogId'
      AND object_id = Object_ID(N'dbo.Commandes')
)
BEGIN
    CREATE INDEX IX_Commandes_WebhookLogId
        ON dbo.Commandes(WebhookLogId)
        WHERE WebhookLogId IS NOT NULL;
END;
GO
