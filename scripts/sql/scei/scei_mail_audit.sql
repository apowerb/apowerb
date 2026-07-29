-- =============================================================================
-- SCEI Mail Audit — audit trail of every notification attempt (real or dry-run)
-- =============================================================================
--
-- Run this DDL on the SCEI SuiviAR database using a privileged account
-- (NOT the `tool_run_sql_suiviar` app pool, which lacks ALTER TABLE — by design).
--
-- Idempotent: re-running is safe.
--
-- Schema rationale (cf. PR #174 + cadrage 12/05 + critique 2026-05-18) :
--   * Audit row inserted BEFORE the Graph POST (Status = 'pending'), updated
--     to 'sent' / 'failed' / 'refused' / 'dry_run' afterwards. No row = no
--     send actually attempted.
--   * Mode column separates dry-run rows from real sends so the PR #150 BI
--     views (`v_suiviar_*`) can filter `Mode = 'live'` for KPI counts.
--   * BodyHash (sha256) lets us detect duplicate notifications without
--     storing the full body (PII).
--   * CommandeId is a logical reference to dbo.Commandes(Id) — not declared
--     as FK because the table schema may evolve independently and we don't
--     want this audit table to break on a Commandes refactor.
-- =============================================================================

IF NOT EXISTS (
    SELECT 1
    FROM   sys.tables
    WHERE  name = 'scei_mail_audit' AND schema_id = SCHEMA_ID('dbo')
)
BEGIN
    CREATE TABLE dbo.scei_mail_audit (
        Id            INT             IDENTITY(1,1) NOT NULL PRIMARY KEY,
        CommandeId    NVARCHAR(36)    NULL,
        SentTo        NVARCHAR(255)   NOT NULL,
        Subject       NVARCHAR(255)   NOT NULL,
        BodyHash      VARCHAR(64)     NOT NULL,
        Status        VARCHAR(20)     NOT NULL,    -- pending|sent|failed|refused|dry_run
        Mode          VARCHAR(10)     NOT NULL,    -- live|dry_run
        ErrorMessage  NVARCHAR(500)   NULL,
        CreatedAt     DATETIME2(0)    NOT NULL CONSTRAINT DF_scei_mail_audit_CreatedAt DEFAULT (GETUTCDATE()),
        CONSTRAINT CK_scei_mail_audit_Status
            CHECK (Status IN ('pending', 'sent', 'failed', 'refused', 'dry_run')),
        CONSTRAINT CK_scei_mail_audit_Mode
            CHECK (Mode IN ('live', 'dry_run'))
    );
    PRINT 'Created table dbo.scei_mail_audit';
END
ELSE
BEGIN
    PRINT 'dbo.scei_mail_audit already exists — skipping CREATE';
END;
GO

-- Indexes ---------------------------------------------------------------------

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = 'IX_scei_mail_audit_CreatedAt' AND object_id = OBJECT_ID('dbo.scei_mail_audit')
)
BEGIN
    CREATE INDEX IX_scei_mail_audit_CreatedAt
        ON dbo.scei_mail_audit (CreatedAt DESC);
    PRINT 'Created index IX_scei_mail_audit_CreatedAt';
END;
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = 'IX_scei_mail_audit_Status_Mode' AND object_id = OBJECT_ID('dbo.scei_mail_audit')
)
BEGIN
    CREATE INDEX IX_scei_mail_audit_Status_Mode
        ON dbo.scei_mail_audit (Status, Mode);
    PRINT 'Created index IX_scei_mail_audit_Status_Mode';
END;
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = 'IX_scei_mail_audit_CommandeId' AND object_id = OBJECT_ID('dbo.scei_mail_audit')
)
BEGIN
    CREATE INDEX IX_scei_mail_audit_CommandeId
        ON dbo.scei_mail_audit (CommandeId)
        WHERE CommandeId IS NOT NULL;
    PRINT 'Created filtered index IX_scei_mail_audit_CommandeId';
END;
GO

-- =============================================================================
-- Grant SELECT/INSERT to the app pool used by `tool_run_sql_suiviar`.
-- Adjust the principal name to match the actual SCEI app login.
--
-- Example (uncomment and adapt after confirming the login name):
--   GRANT SELECT, INSERT, UPDATE ON dbo.scei_mail_audit TO [thaink2_app];
-- =============================================================================
