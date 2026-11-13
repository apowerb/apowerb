# Nœuds de sortie du Workflow Studio

*Recherche du 22/10/2026. Base : `apowerb/apowerb` main `09bd626` et `apowerb/apowerb-ui` main `e09b256` (après #172).*

Un nœud de sortie, c'est l'endroit où va le résultat d'un workflow : une réponse à qui l'a lancé, un fichier, un message, une ligne dans un tableau ou une base, un tableau de bord. Ce document compare ce que proposent neuf outils du marché à ce qu'apowerb sait déjà faire. Il en tire un premier lot pensé pour un utilisateur métier : Farid enchaîne un agent d'analyse puis un agent de prévision des ventes, et veut recevoir le résultat sous une forme qu'il peut ouvrir, garder et transmettre.

Légende des sources :
- **[V]** : vérifié sur la page de documentation officielle ;
- **[R]** : vu dans un extrait de recherche, sans que la page ait été ouverte ;
- **[S]** : supposé, d'après une connaissance générale non vérifiée.

Une cellule [S] ou [R] ne doit pas être citée comme un fait.

## 1. Comparatif

| Catégorie | n8n | Make | Zapier | Power Automate | Dify | Langflow | Flowise | Pipedream | Activepieces |
|---|---|---|---|---|---|---|---|---|---|
| Réponse au webhook | Respond to Webhook [V] | Webhook response [V] | réponse de Catch Hook [S] | Response (Request) [R] | — | — | — | `$.respond()` [V] | Return Response [S] |
| Réponse au chat ou à l'appelant | (Chat Trigger) [S] | — | — | — | Answer (chatflow) [V], Output, ex-End (workflow) [V] | Chat Output [V] | Direct Reply (Agentflow v2) [V] | — | — |
| Retour vers un workflow parent | Execute Sub-workflow : la sortie du dernier nœud [V] | Return output [S] | Return From a Sub-Zap [R] | Respond to a PowerApp or flow [R] | Output, utilisable comme outil [V] | — | Execute Flow [V] | Invoke Workflow [S] | sous-flux via webhook [S] |
| Fichier CSV / tableur | Convert to File (CSV, XLSX, ODS) [V] | Create CSV [S] | Formatter [S] | Create CSV table, qui produit du texte et non un fichier [V] | code ou outil [S] | — | Custom Function [V] | code [S] | code [S] |
| Fichier JSON / texte / HTML | Convert to File (JSON, Text, HTML, RTF, ICS) [V] | — | — | Create HTML table [V] | variable de type file [V] | Chat Output, Message/JSON/Table [V] | — | code [S] | — |
| Fichier PDF / Word | absent de Convert to File [V] | module tiers [S] | appli tierce [S] | — [S] | plugin [S] | — | — | code [S] | — |
| Stockage S3, OneDrive, SharePoint, Drive | nœuds dédiés [S] | modules [S] | actions [S] | SharePoint, OneDrive [S] | via outil ou HTTP [S] | Save File [S] | — | actions [S] | pièces [S] |
| Mail avec pièce jointe, Teams, Slack | nœuds dédiés [S] | modules [S] | actions [S] | Outlook, Teams [S] | via outil [S] | — | — | actions [S] | pièces [S] |
| Tableurs et tables internes | Data tables, Sheets « Append row » [S] | Data stores [S] | Zapier Tables [S] | Excel Online [S] | — | — | — | — | Tables, créer et mettre à jour des lignes [V] |
| Graphique / BI | — [S] | — | — | Power BI [S] | — | — | — | — | — |

### Limites documentées

- **n8n** :
  - Respond to Webhook ne répond qu'une fois, avec le premier élément.
  - Le webhook doit être réglé sur « Using Respond to Webhook node ».
  - Le HTML est servi dans une iframe sandbox depuis la v1.103.0 [V].
  - Payload de 16 Mio par défaut [R].
- **Make** :
  - sans module de réponse, le webhook répond 200 « Accepted » ;
  - 300 requêtes par 10 s [V].
- **Pipedream** :
  - sans `$.respond()`, l'endpoint répond 400 ;
  - le corps entrant est limité à 512 Ko [V].
- **Power Automate** :
  - réponse sous 120 s ;
  - message de 100 Mo, jusqu'à 1 Go par morceaux [R].
- **Dify** : Output accepte les types `file` et `array[file]` ; plusieurs Output sont fusionnés [V].
- **Flowise** : Direct Reply termine la branche [V].
- **Zapier** : sans Return dans chaque branche, le Zap parent reste « Delayed » [R].

### Motifs communs

1. **La réponse à l'appelant est un nœud explicite.** Sans lui, l'outil renvoie un accusé de réception (Make, n8n) ou une erreur (Pipedream) [V].
2. **La conversion en fichier est séparée de l'envoi.** n8n Convert to File produit un binaire et Power Automate Create CSV table produit du texte ; c'est l'étape suivante qui stocke ou envoie [V].
3. **Le PDF et le Word sont rarement natifs.** Ni n8n, ni Power Automate, ni Dify ne les produisent dans un nœud de base [V pour n8n].
4. **Les outils orientés agents ont des sorties de conversation.** C'est le cas d'Answer et Output chez Dify, de Chat Output chez Langflow et de Direct Reply chez Flowise [V].
5. **Les limites documentées portent surtout sur le temps et le débit**, rarement sur la taille des fichiers.

Sources, consultées le 22/10/2026, sans date de page sauf mention :
- docs.n8n.io : `respondtowebhook`, `converttofile` et `executeworkflow` ;
- help.make.com/webhooks ;
- help.zapier.com, article 32283713627533 ;
- learn.microsoft.com, Power Automate : `data-operations` (ms.date 2026-08-19) et `limits-and-config` ;
- docs.dify.ai : `nodes/answer` et `nodes/output` ;
- docs.langflow.org/chat-input-and-output (v1.12) ;
- docs.flowiseai.com/using-flowise/agentflowv2 ;
- pipedream.com/docs/workflows/building-workflows/triggers (la page annonce la fermeture de Workflows et String au 31/03/2027, contexte non éclairci) ;
- activepieces.com/docs/tables/in-flows.

## 2. Ce qu'apowerb a déjà (lu dans le code)

| Besoin | Où | Remarque |
|---|---|---|
| Résultat du run (panneau Test, appelant) | nœud `output` | Plusieurs sorties sont regroupées par identifiant. |
| Retour vers un workflow parent | `subworkflow` + `output` du workflow appelé | Profondeur 3, cycle détecté. |
| Workflow appelé par un agent comme un outil | trigger `agent_tool` | — |
| Message à soi, mail texte, Teams | nœud `notification` (`app`, `email` SMTP, `teams` webhook chiffré) | Pas de pièce jointe avant ce lot. |
| Écriture en base SQL | nœud `db_write` (#172) | Une ligne, PostgreSQL, connexion enregistrée. |
| Appel HTTP sortant | nœud `http` | Garde SSRF, pas d'authentification. |
| Google Sheets, Google Docs, Teams (chat), Outlook avec pièces jointes, graphiques et tableaux de bord BI | nœud `tool` + outils du catalogue (`tool_write_cells`, `tool_create_document`, `tool_send_message`, `tool_send_outlook_email`, `tool_create_chart`…) | Le graphique s'affiche déjà dans le panneau Test (`ArtifactsBlock`). Les outils à chemin local (`tool_upload_file`, pièces jointes Outlook) ne peuvent pas recevoir un fichier du graphe en sécurité. |

**Ce qui manquait** : produire un fichier (aucun nœud ne fabrique un CSV, un Excel ou un PDF), joindre un fichier à un mail, déposer un fichier dans un stockage du client, répondre de façon synchrone au webhook (la route `POST /api/hooks/workflows/{token}` répond 202 avec `run_id`, le run tourne ensuite), et Slack ou WhatsApp (aucune intégration).

## 3. Priorisation

Note de 1 (faible) à 3 (fort) ; pour l'effort et le risque, 3 = élevé.

| Candidat | Valeur pour Farid | Effort | Risque sécurité | Décision |
|---|---|---|---|---|
| **Créer un fichier** (CSV, Excel, JSON, Markdown, texte, Word, PDF) | 3 : le tableau de prévisions qu'il ouvre dans Excel, le rapport qu'il transmet | 2 | 1 (bornes de taille, ref du propriétaire, formules neutralisées) | **Lot 1** |
| **Pièces jointes du mail** (`notification` email) | 3 : le rapport arrive dans sa boîte | 1 | 2 (refs du propriétaire seulement, plafond total, pas d'octets dans les journaux) | **Lot 1** |
| **Déposer sur OneDrive** | 2 : archive partagée de l'équipe | 1 (outil existant) | 2 (chemin issu de la ref, jamais du graphe ; identité du propriétaire) | **Lot 1** |
| **Téléchargement depuis le panneau Test** | 3 : voir et ouvrir le fichier tout de suite | 1 | 1 (404 opaque, `attachment`, `nosniff`) | **Lot 1** (fait partie du nœud fichier) |
| Réponse synchrone au webhook | 2 (intégrations tierces) | 3 : changement de route, run en mémoire ADK | 2 (temps de réponse, DoS) | Lot 2 : `?wait=<s>` borné, puis 202 si dépassé |
| Google Sheets, ajout de lignes | 2 | 1 | 1 | Déjà possible via le nœud outil (`tool_write_cells`) ; nœud dédié plus tard |
| Graphique ou tableau de bord | 2 | 2 | 1 | Déjà possible via un agent ou un outil BI, rendu dans le panneau Test |
| Google Drive, SharePoint | 2 | 2 : aucun outil d'upload | 2 | Reporté : ajouter d'abord l'upload au catalogue |
| Slack, WhatsApp | 1 | 3 : aucune intégration | 2 | Reporté |
| S3 / stockage objet du client | 1 | 2 | 3 : identifiants à gérer | Reporté |

## 4. Lot 1 livré

1. **`file`, « Créer un fichier »**
   - Configuration : `format` (csv, xlsx, json, markdown, text, docx, pdf), `filename` (gabarit, extension ajoutée), `content` (gabarit), `sheet_name` (xlsx).
   - Contenu attendu :
     - csv et xlsx : une liste d'objets, ou une liste de listes avec l'en-tête en premier ; une chaîne JSON de l'une ou l'autre est acceptée ;
     - docx et pdf : du Markdown simple (titres, listes ; tableaux en Word seulement), au plus 200 000 caractères, 2 000 lignes (le rendu PDF coûte surtout à la ligne) et, pour les tableaux, 63 colonnes (limite de Word) et 20 000 cellules au total.
   - Sortie : `{name, ref, size, content_type, format, rows}` ; `{{file1}}` désigne le fichier entier, à joindre ou à déposer.
   - Bornes des tableaux csv/xlsx : 10 000 lignes, 1 000 colonnes et 500 000 cellules (des objets aux clés toutes différentes multiplient les colonnes), vérifiées avant de produire la moindre cellule.
   - Bornes des autres formats : une valeur non textuelle est écrite en JSON indenté, après contrôle de sa profondeur (50 niveaux) ; le texte est produit morceau par morceau et abandonné dès qu'il dépasse 5 Mio (200 000 caractères pour Word/PDF). Les tableaux Markdown et les lignes sont comptés avant le rendu Word/PDF, sur le texte même que le rendu reçoit : caractères de contrôle déjà retirés, toute fin de ligne comptée (\r compris).
   - Tout fichier : 5 Mio au plus, contenu vide refusé ; lecture, contrôles et rendu hors de la boucle d'événements.
   - 20 fichiers au plus par run, reprises comprises : « Reprendre ici » et la reprise après une décision comptent les fichiers déjà produits (rejoués, ils ne sont pas reproduits). Ce n'est pas un quota par compte.
   - Cellules : en CSV, une cellule qui commence par `=`, `+`, `@` ou `-` (hors nombre) est préfixée d'une apostrophe (injection de formules) ; en Excel, toute cellule texte est typée texte, donc jamais évaluée, et garde sa valeur exacte. Les caractères de contrôle sont retirés.
   - PDF : police DejaVu livrée avec matplotlib, car l'image de production n'a pas de police système.
   - Stockage : une ref `att_` du propriétaire, sous `workflow_generated/`. Les types produits ne sont **pas** ajoutés aux envois de fichiers (`store`).
2. **Pièces jointes du mail**
   - `notification.attachments` reçoit une ref, la sortie d'un nœud `file` ou une liste ; email seulement.
   - Bornes : 5 fichiers et 10 Mio au total.
   - Les refs sont résolues pour le propriétaire du run seulement. Les journaux citent les noms de fichiers, jamais leur contenu.
3. **`file_save`, « Déposer sur OneDrive »**
   - Configuration : `provider` (`onedrive`), `file`, `folder` (relatif, sans `..`, revérifié après rendu), `conflict` (rename, replace, fail).
   - Dépôt par l'intégration Microsoft du propriétaire, sous son identité. Le chemin local vient de la ref résolue.
   - Sortie : `{provider, name, path, web_url}`. En cas d'échec, le message de l'outil reste dans les journaux du serveur (il peut citer des chemins).
4. **Téléchargement** : route `GET /api/workflows/files/{ref}`, réservée au propriétaire (404 opaque sinon), servie en pièce jointe avec `nosniff`. Bouton dans le panneau Test.

Chaque nœud a ses règles de validation identiques côté moteur et côté studio. Ses codes d'erreur sont traduits en anglais et en français dans le panneau d'exécution, et il est listé parmi les effets du « Test up to here ».

## 5. Hypothèses et suites

- **Hypothèse** : `TH2_ATTACHMENT_ROOT` est persistant en production, comme pour les pièces jointes des triggers mail. Sinon, un fichier produit disparaît au redémarrage et son lien de téléchargement renvoie 404.
- **Prérequis avant la production : purge et quota.** Rien ne supprime encore `workflow_generated/` et aucun quota par compte ne borne son volume (un workflow planifié peut produire 20 fichiers de 5 Mio à chaque exécution). Il faut une rétention, par exemple 30 jours alignée sur celle des entrées de run, et un quota de stockage par propriétaire.
- **Échec SMTP silencieux, préexistant** : `send_email` retombe sur un faux envoi journalisé quand le relais échoue. Le nœud compte alors l'envoi comme fait, pièces jointes comprises. C'est à corriger à part.
- **Lot 2** :
  - réponse synchrone au webhook (`?wait=`) ;
  - nœud Google Sheets dédié ;
  - upload Google Drive et SharePoint dans le catalogue, puis `file_save` multi-stockage ;
  - pièces jointes Teams.
