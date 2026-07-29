# SCEI SuiviAR — Vues analytiques

3 vues SQL Server qui alimentent le dashboard SCEI.

## Vues

| Vue | Usage |
|---|---|
| `v_suiviar_daily` | Pilotage quotidien : volume, conformité, breakdown statuts, taux traitement |
| `v_suiviar_top_fournisseurs` | Top fournisseurs par anomalies + breakdown types d'écart (prix/qté/délai/lignes) |
| `v_suiviar_cycle_time` | Temps de cycle réception → traitement par AR (avec `age_minutes` pour backlog) |

Idempotentes (`IF OBJECT_ID DROP + CREATE`). Pas de migration de données.

## Application

### Via MCP Toolbox SCEI_PROD_VM (recommandé)

Depuis SCEI_PROD_VM (VPN OpenVPN actif vers `192.168.1.0/24`) :

```bash
ssh SCEI_PROD_VM
cd /opt/th2agent
# Le MCP Toolbox tourne sur :5000 et a déjà un toolset 'suiviar-tools'.
# Le DDL n'est pas dans le toolset SELECT-only, donc passer par sqlcmd direct :
sqlcmd -S 192.168.1.205,1433 \
       -U <user> -P '<pwd>' \
       -d SuiviAR \
       -i /tmp/scei_analytics_views.sql
```

### Via sqlcmd Linux (Ubuntu)

```bash
# Installer sqlcmd si absent
curl https://packages.microsoft.com/keys/microsoft.asc | sudo apt-key add -
sudo apt install -y mssql-tools18 unixodbc-dev
export PATH=$PATH:/opt/mssql-tools18/bin

# Appliquer
sqlcmd -S 192.168.1.205,1433 -U <user> -P '<pwd>' -d SuiviAR -i scei_analytics_views.sql
```

## Vérification post-déploiement

```sql
USE SuiviAR;
SELECT name FROM sys.views WHERE name LIKE 'v_suiviar_%' ORDER BY name;
-- Doit retourner 3 lignes : v_suiviar_cycle_time, v_suiviar_daily, v_suiviar_top_fournisseurs
```

## Exemples d'usage par l'agent BI

```sql
-- Dernier 30 jours, pilotage chat avec direction
SELECT * FROM dbo.v_suiviar_daily
WHERE jour >= DATEADD(DAY, -30, CAST(GETDATE() AS DATE))
ORDER BY jour DESC;

-- Top 10 fournisseurs en anomalies
SELECT TOP 10 * FROM dbo.v_suiviar_top_fournisseurs
ORDER BY nb_anomalies DESC;

-- Temps de cycle 7 derniers jours
SELECT
    AVG(cycle_minutes)                                                   AS avg_cycle,
    MIN(cycle_minutes)                                                   AS min_cycle,
    MAX(cycle_minutes)                                                   AS max_cycle
FROM dbo.v_suiviar_cycle_time
WHERE TraiteLe IS NOT NULL
  AND DateReceptionAR >= DATEADD(DAY, -7, GETDATE());

-- Backlog (ARs reçus il y a + 60 min, non traités)
SELECT NumeroCommande, FournisseurNom, age_minutes, StatutGlobal
FROM dbo.v_suiviar_cycle_time
WHERE Traite = 0 AND age_minutes > 60
ORDER BY age_minutes DESC;
```

## Rollback

```sql
USE SuiviAR;
DROP VIEW IF EXISTS dbo.v_suiviar_daily;
DROP VIEW IF EXISTS dbo.v_suiviar_top_fournisseurs;
DROP VIEW IF EXISTS dbo.v_suiviar_cycle_time;
```
