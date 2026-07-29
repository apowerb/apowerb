-- Idempotence guard for SCEI AR pipeline.
-- Prevents duplicate AR header rows for the same (NumeroCommande, Societe)
-- on webhook retries (e.g. after a downstream notifier rate-limit).
-- 0 existing duplicates verified 2026-05-20 before creation.
IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = 'UX_Commandes_Numero_Societe'
      AND object_id = OBJECT_ID('dbo.Commandes')
)
EXEC('CREATE UNIQUE INDEX UX_Commandes_Numero_Societe
      ON dbo.Commandes (NumeroCommande, Societe)
      WHERE NumeroCommande IS NOT NULL');
