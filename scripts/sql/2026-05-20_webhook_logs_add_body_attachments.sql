-- Capture du body + PJ au moment du webhook
-- Live decision 2026-05-20 (David, post incident 2026-05-19 "replay 52 ARs"):
-- les emails sont souvent supprimés/archivés dans les jours qui suivent →
-- Microsoft Graph répond ErrorItemNotFound. Le replay automatique d'un AR
-- en error n'est possible que si on a conservé le contenu nous-mêmes.
--
-- Idempotent (IF NOT EXISTS). ADD COLUMN NULL en PG11+ est metadata-only :
-- pas de table rewrite, pas de lock long. Safe sur prod live.
--
-- À exécuter une fois sur chaque base : SCEI_PROD, OVH_DEV, DAVE_OVH.

ALTER TABLE th2scei.webhook_logs
    ADD COLUMN IF NOT EXISTS email_body_html TEXT,
    ADD COLUMN IF NOT EXISTS email_body_text TEXT,
    ADD COLUMN IF NOT EXISTS attachments JSONB;
