-- =============================================================================
-- SCEI — Normalisation deterministe de dbo.Commandes.StatutGlobal
-- =============================================================================
--
-- Probleme (live 2026-05-20, David) : l'agent recorder (LLM) ecrivait parfois
-- des valeurs hors vocabulaire dans StatutGlobal :
--   * 'OK' (1 cas) — confusion avec le vocabulaire LIGNE (Situation = OK/NOK) ;
--   * 'ORDER_NOT_FOUND' (1 cas) — ancien statut, desormais fusionne.
-- Le prompt interdit pourtant deja ces valeurs, mais un LLM reste non
-- deterministe. Le client refuse de voir ces libelles au dashboard.
--
-- Vocabulaire CIBLE (decision David 2026-05-20) — 3 valeurs, rien d'autre :
--   conforme | non_conforme | non_rapproche
-- ORDER_NOT_FOUND (commande absente de PMI) est FUSIONNE dans non_rapproche.
--
-- Solution : garde-fou DETERMINISTE en base. Un trigger normalise toute
-- valeur hors vocabulaire a l'ecriture (INSERT/UPDATE). On NE bloque PAS
-- (un CHECK rejetterait l'INSERT => AR perdu) ; on normalise.
--   OK -> conforme | NOK -> non_conforme | ORDER_NOT_FOUND -> non_rapproche
--   variantes de casse -> minuscule.
-- Une valeur totalement inconnue est laissee telle quelle (ELSE) — choix
-- assume : ne pas inventer un mapping, ne pas perdre la ligne.
--
-- Anti-recursion : IF TRIGGER_NESTLEVEL() > 1 RETURN (protege aussi contre
-- une activation future de RECURSIVE_TRIGGERS, OFF par defaut, ou un 2e
-- trigger sur la table). Collation BIN dans le WHERE pour detecter les
-- variantes de casse malgre la collation French_CI_AS de la base.
--
-- Idempotent (DROP IF EXISTS + CREATE). A executer sur SuiviAR (SCEI_PROD)
-- avec un compte privilegie (le pool applicatif n'a pas les droits DDL).
-- =============================================================================

-- Nettoyage de l'existant (valeurs ecrites avant la pose du trigger)
UPDATE dbo.Commandes SET StatutGlobal = 'conforme'      WHERE StatutGlobal = 'OK';
UPDATE dbo.Commandes SET StatutGlobal = 'non_rapproche' WHERE StatutGlobal = 'ORDER_NOT_FOUND';
GO

IF OBJECT_ID('dbo.trg_Commandes_normalize_statut', 'TR') IS NOT NULL
    DROP TRIGGER dbo.trg_Commandes_normalize_statut;
GO

CREATE TRIGGER dbo.trg_Commandes_normalize_statut
ON dbo.Commandes
AFTER INSERT, UPDATE
AS
BEGIN
    SET NOCOUNT ON;
    IF TRIGGER_NESTLEVEL() > 1 RETURN;

    UPDATE c
    SET StatutGlobal = CASE UPPER(LTRIM(RTRIM(c.StatutGlobal)))
        WHEN 'OK'                     THEN 'conforme'
        WHEN 'NOK'                    THEN 'non_conforme'
        WHEN 'ORDER_NOT_FOUND'        THEN 'non_rapproche'
        WHEN 'ORDER_NOT_FOUND_IN_PMI' THEN 'non_rapproche'
        WHEN 'CONFORME'               THEN 'conforme'
        WHEN 'NON_CONFORME'           THEN 'non_conforme'
        WHEN 'NON_RAPPROCHE'          THEN 'non_rapproche'
        ELSE c.StatutGlobal
    END
    FROM dbo.Commandes c
    INNER JOIN inserted i ON c.ID = i.ID
    WHERE c.StatutGlobal IS NOT NULL
      AND c.StatutGlobal COLLATE Latin1_General_BIN
          NOT IN ('conforme', 'non_conforme', 'non_rapproche');
END;
GO
