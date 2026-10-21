# Banc tokens BI — résultats

Tokens d'entrée (tokenizer Gemini local, somme sur les appels du scénario), modèle scripté. Chaque levier ajoute sa ligne : `python bench/bi_tokens/replay.py --estimates --compare bench/bi_tokens/results/main.json`.

## Base : `main` (36ef99e, cœur v0.2.82 ; `main_chat_spec` sur ce680ef, correctif #384)

| Scénario | Tours | Appels | Outils | 1er appel | Total | Mesuré DEV |
|---|---|---|---|---|---|---|
| creation | 1 | 5 | 36 | 17 070 | 104 285 | 98 144 |
| dashboard_chat | 2 | 5 | 36 | 17 113 | 151 177 | – |
| forecast | 1 | 4 | 36 | 17 060 | 108 775 | – |
| multi_turn | 3 | 10 | 36 | 17 070 | 286 522 | – |
| question | 1 | 3 | 36 | 17 113 | 75 227 | 71 617 |
| question_targeted *(estim.)* | 1 | 3 | 36 | 17 113 | 61 315 | 71 617 |
| main_chat_spec | 1 | 3 | 44 | 20 641 | 71 293 | – |

## D1 — outils de chat inutiles retirés des gabarits BI

Avant `main` (36ef99e) → après `d1` (695afa2)

| Scénario | Appels | Outils | 1er appel (tok) | Total avant | Total après | Δ | Qualité |
|---|---|---|---|---|---|---|---|
| creation | 5→5 | 36→26 | 17 070→13 916 | 104 285 | 88 515 | -15.1 % | ✅ |
| dashboard_chat | 5→5 | 36→26 | 17 113→13 959 | 151 177 | 135 407 | -10.4 % | ✅ |
| forecast | 4→4 | 36→26 | 17 060→13 906 | 108 775 | 96 159 | -11.6 % | ✅ |
| multi_turn | 10→10 | 36→26 | 17 070→13 916 | 286 522 | 254 982 | -11.0 % | ✅ |
| question | 3→3 | 36→26 | 17 113→13 959 | 75 227 | 65 765 | -12.6 % | ✅ |
| question_targeted *(estim.)* | 3→3 | 36→26 | 17 113→13 959 | 61 315 | 51 853 | -15.4 % | ✅ |


## D4 — gros CSV lu en en-tête + 20 lignes + nombre de lignes (agents BI)

Avant `d1` (695afa2) → après `d4` (dd43c4d)

| Scénario | Appels | Outils | 1er appel (tok) | Total avant | Total après | Δ | Qualité |
|---|---|---|---|---|---|---|---|
| creation | 5→5 | 26→26 | 13 916→13 916 | 88 515 | 88 515 | +0.0 % | ✅ |
| dashboard_chat | 5→5 | 26→26 | 13 959→13 959 | 135 407 | 135 407 | +0.0 % | ✅ |
| forecast | 4→4 | 26→26 | 13 906→13 906 | 96 159 | 61 164 | -36.4 % | ✅ |
| multi_turn | 10→10 | 26→26 | 13 916→13 916 | 254 982 | 254 982 | +0.0 % | ✅ |
| question | 3→3 | 26→26 | 13 959→13 959 | 65 765 | 65 765 | +0.0 % | ✅ |
| question_targeted *(estim.)* | 3→3 | 26→26 | 13 959→13 959 | 51 853 | 51 853 | +0.0 % | ✅ |

## D5 — `group_by` dans tool_bi_query_dashboard

Avant `d4` (dd43c4d) → après `d5` (4e4fe32)

| Scénario | Appels | Outils | 1er appel (tok) | Total avant | Total après | Δ | Qualité |
|---|---|---|---|---|---|---|---|
| creation | 5→5 | 26→26 | 13 916→13 988 | 88 515 | 88 875 | +0.4 % | ✅ |
| dashboard_chat | 5→5 | 26→26 | 13 959→14 031 | 135 407 | 135 767 | +0.3 % | ✅ |
| forecast | 4→4 | 26→26 | 13 906→13 978 | 61 164 | 61 452 | +0.5 % | ✅ |
| multi_turn | 10→10 | 26→26 | 13 916→13 988 | 254 982 | 255 702 | +0.3 % | ✅ |
| question | 3→3 | 26→26 | 13 959→14 031 | 65 765 | 65 981 | +0.3 % | ✅ |
| question_grouped *(estim.)* | 3 | 26 | 14 031 | – | 51 094 | nouveau | ✅ |
| question_targeted *(estim.)* | 3→3 | 26→26 | 13 959→14 031 | 51 853 | 52 069 | +0.4 % | ✅ |

Coût fixe : +72 tokens par appel (déclaration). Gain si le modèle s'en sert : question 65 981 → 51 094 (−22,6 %), mais seulement −1,9 % face à `component_ids` par canal (question_targeted) : à juger au mode réel.

## D2 — gros résultats d'outils des tours précédents raccourcis (agents BI)

Avant `d5` (4e4fe32) → après `d2` (385abd4)

| Scénario | Appels | Outils | 1er appel (tok) | Total avant | Total après | Δ | Qualité |
|---|---|---|---|---|---|---|---|
| creation | 5→5 | 26→26 | 13 988→13 988 | 88 875 | 88 875 | +0.0 % | ✅ |
| dashboard_chat | 5→5 | 26→26 | 14 031→14 031 | 135 767 | 100 271 | -26.1 % | ✅ |
| forecast | 4→4 | 26→26 | 13 978→13 978 | 61 452 | 61 452 | +0.0 % | ✅ |
| multi_turn | 10→10 | 26→26 | 13 988→13 988 | 255 702 | 200 883 | -21.4 % | ✅ |
| question | 3→3 | 26→26 | 14 031→14 031 | 65 981 | 65 981 | +0.0 % | ✅ |
| question_grouped *(estim.)* | 3→3 | 26→26 | 14 031→14 031 | 51 094 | 51 094 | +0.0 % | ✅ |
| question_targeted *(estim.)* | 3→3 | 26→26 | 14 031→14 031 | 52 069 | 52 069 | +0.0 % | ✅ |

Contre-preuve : compacter aussi le tour en cours fait échouer facts/grounded sur 4 scénarios.

## Consigne — bloc « Dashboard context » seulement avec ses outils

Avant `d2` (385abd4) → après `prompt` (b836389)

| Scénario | Appels | Outils | 1er appel (tok) | Total avant | Total après | Δ | Qualité |
|---|---|---|---|---|---|---|---|
| creation | 5→5 | 26→26 | 13 988→13 598 | 88 875 | 86 925 | -2.2 % | ✅ |
| dashboard_chat | 5→5 | 26→26 | 14 031→13 641 | 100 271 | 98 321 | -1.9 % | ✅ |
| forecast | 4→4 | 26→26 | 13 978→13 588 | 61 452 | 59 892 | -2.5 % | ✅ |
| multi_turn | 10→10 | 26→26 | 13 988→13 598 | 200 883 | 196 983 | -1.9 % | ✅ |
| question | 3→3 | 26→26 | 14 031→13 641 | 65 981 | 64 811 | -1.8 % | ✅ |
| question_grouped *(estim.)* | 3→3 | 26→26 | 14 031→13 641 | 51 094 | 49 924 | -2.3 % | ✅ |
| question_targeted *(estim.)* | 3→3 | 26→26 | 14 031→13 641 | 52 069 | 50 899 | -2.2 % | ✅ |

Outils cités par la consigne mais non déclarés : 5 → 0.

## Cumul main → tous les leviers (D1 + D4 + D5 + D2 + consigne)

Avant `main` (36ef99e) → après `prompt` (b836389)

| Scénario | Appels | Outils | 1er appel (tok) | Total avant | Total après | Δ | Qualité |
|---|---|---|---|---|---|---|---|
| creation | 5→5 | 36→26 | 17 070→13 598 | 104 285 | 86 925 | -16.6 % | ✅ |
| dashboard_chat | 5→5 | 36→26 | 17 113→13 641 | 151 177 | 98 321 | -35.0 % | ✅ |
| forecast | 4→4 | 36→26 | 17 060→13 588 | 108 775 | 59 892 | -44.9 % | ✅ |
| multi_turn | 10→10 | 36→26 | 17 070→13 598 | 286 522 | 196 983 | -31.3 % | ✅ |
| question | 3→3 | 36→26 | 17 113→13 641 | 75 227 | 64 811 | -13.8 % | ✅ |
| question_grouped *(estim.)* | 3 | 26 | 13 641 | – | 49 924 | nouveau | ✅ |
| question_targeted *(estim.)* | 3→3 | 36→26 | 17 113→13 641 | 61 315 | 50 899 | -17.0 % | ✅ |

## D3 — cache Gemini (analyse, rien de changé)

- La jauge client somme `llm_usage.total_tokens` (`usage_quota.py`, `func.sum(LlmUsage.total_tokens)`) ; `total_tokens` vient de `total_token_count` de Gemini, qui inclut les tokens servis depuis le cache (`cached_content_token_count` en est une partie). Le cache baisse la facture Gemini (tokens en cache facturés ~25 %), **pas** la jauge.
- Préfixe stable par appel après les leviers : consigne + déclarations ≈ 13,6k tokens, identique d'un appel à l'autre d'une session : candidat au cache implicite (déjà actif chez Gemini, aléatoire en DEV le 15/10) ou explicite (`gemini_prompt_cache.py`, éteint par défaut).
- Pour que la jauge en profite, il faudrait pondérer le cache dans le quota, p. ex. `total − 0,75 × cached`. ESTIMATION sur `multi_turn` (10 appels) si tout le préfixe est servi par le cache : 196 983 → ~95k comptés. Décision produit/tarif, à prendre par Elom ; à mesurer avec `dev_usage.py` (colonne `cached`) avant.

## Mesure DEV 16/10 — cœur 0.2.84 (pile #378-#385), agent BI Assistant 1421

Mode réel : les 5 scénarios rejoués à la main sur agent-dev (compte e2e, une conversation neuve par scénario, 10:11-10:19Z), coûts lus par `dev_usage.py --schema th2agent_dev --agent-id 1421 --since 2026-10-16T10:07:53Z` (34 appels, 6 sessions, toutes attribuées), qualité lue dans les events ADK de chaque session. `dev_usage.py` validé d'abord contre la base (relevés du matin retrouvés à l'identique ; transaction READ ONLY, une écriture d'essai est refusée).

« Avant » MESURÉ = sessions DEV du 14/10 sur le même agent (cœur 0.2.81) : il n'existe que pour creation et question. Aucun avant n'a été mesuré sous 0.2.82/0.2.83 (la seule tentative a répondu « dashboard vide », bug corrigé par #384). Pour les trois autres, la colonne banc donne l'après INFÉRÉ par le banc hors ligne (qui dépasse le réel de 9 à 16 %).

| Scénario | Session DEV | Appels | Entrée avant (MESURÉ 14/10) | Entrée après | Δ entrée | Total (jauge) après | Cached | Banc après *(INFÉRÉ)* | Réponse correcte |
|---|---|---|---|---|---|---|---|---|---|
| creation | session_1792145303857 | 4 | 82 212 (moy. de 3 créations à 4 appels : 83 394 / 81 512 / 81 730) | 63 526 (2e création : 63 298) | **−22,7 %** | 64 969 | 0 (2e : 31 920) | 86 925 | ✅ tableau créé, KPI cités |
| question | dashboard-chat-3593ae37 | 3 | 71 617 (dashboard-chat-e3ca56c3, 1 + 4 requêtes) | 42 130 | **−41,2 %** | 42 602 | 11 932 | 64 811 | ✅ Email 32,8 |
| dashboard_chat | dashboard-chat-b14af6a2 | 7 | – | 99 761 | – | 101 970 | 50 584 | 98 321 | ✅ Email 32,8 ; semaine du 6 avril, 22 557,15 € |
| multi_turn | session_1792145780770 | 10 | – | 158 022 | – | 160 598 | 48 803 | 196 983 | ✅ 32,8 / 3,6 / 3,0 ; 6 avril, 22 557,15 € |
| forecast | session_1792145909418 | 6 | – | 81 865 | – | 85 070 | 21 768 | 59 892 | ❌ aucune prévision |

Mesures annexes (MESURÉ) :
- Plancher du 1er appel : 14 525-14 576 (14/10) → 11 832-11 884 (−18,5 %), effet D1 + consigne (#383).
- D2 visible : dans multi_turn, l'entrée retombe de 18 710 (fin du tour 1) à 12 959 au début du tour 2.
- question : le modèle fait 1 requête groupée au lieu de 1 + 4 requêtes filtrées par canal ; d'où l'écart réel (−41 %) bien au-delà du banc (−13,8 %), dont le modèle scripté rejouait les 5 requêtes.
- Nouveau : chaque mini-chat commence par `tool_bi_describe_dashboard` (~7k car. de résultat).

Qualité, réserve : le fichier lu n'est pas celui du kit. L'import renvoie `online_store_orders.csv #0cd95e47` et `read_uploaded_file` 901 110 octets, alors que le CSV envoyé fait 908 351 octets (sha256 d44e0d81…). Les KPI (ROAS 32,8175 / 3,5876…) sont identiques au 14/10 : les tableaux reposent sur l'ancien fichier du même nom (INFÉRÉ). Les réponses sont donc jugées contre les faits des scénarios (32,8 / 3,6 / 3,0), pas contre le CSV du kit ; l'écart relevé à la main (ROAS Email ~247, CA ~588k sur le kit) s'explique par ce changement de fichier, pas par la définition du gabarit. Avantage pour la mesure : mêmes données que la référence du 14/10. À traiter à part : une pièce jointe de même nom peut être résolue vers un fichier plus ancien.

### Verdict D5 — le modèle se sert-il de `group_by` / `component_ids` ?

Oui. Sur 7 appels `tool_bi_query_dashboard` (question, dashboard_chat, multi_turn) : `component_ids` 7/7, `group_by` 4/7. Les 2 `group_by: "canal"` réussissent (une requête au lieu de cinq). Les 2 `group_by: "periode"` échouent : `periode` est un filtre `relative_period`, or group_by n'accepte que les filtres `select` (« Unknown filter 'periode' for group_by. Filters: canal. ») ; l'un des deux cite en plus un titre de composant inexistant (« Total Revenue » au lieu de « Revenue »). Coût de ces 2 essais perdus dans dashboard_chat : ~27k d'entrée. Suite proposée : écrire dans la docstring de l'outil que group_by ne prend que les filtres de type select, et lister les ids des composants dans `describe_dashboard`.

### D3 — part du cache (MESURÉ)

`cached` est inclus dans l'entrée (cached ≤ input à chaque appel ; total = input + output). Part en cache : creation 0 % et 50 % (2 créations), question 28 %, dashboard_chat 51 %, multi_turn 31 %, forecast 27 % ; au global 165 007 / 508 602 = **32,4 %** (14/10 : 17 à 66 % selon la session). Cache implicite Gemini, non piloté. Si la jauge pondérait le cache (`total − 0,75 × cached`), ces 6 sessions passeraient de 519 838 à ~396 083 tokens comptés (−23,8 %). Décision tarif/produit : Elom.

### Échec forecast — diagnostic (events + code)

Déroulé : `read_uploaded_file` (aperçu D4, 20 lignes) → `tool_create_forecast_dashboard(file_id, date_var=order_date, target_var=order_amount, frequency=month, horizon=3)` → erreur « Doublons de dates détectés : 2025-10-01, … » → `tool_bi_import_attachment` → `tool_describe_sql` (GROUP BY mensuel) → erreur « Aucune connexion base de données configurée pour cet agent » → même appel avec `dataset_id` → même erreur → `request_user_input`.

Cause : le CSV contient des commandes, donc plusieurs lignes par date ; le moteur de prévision exige une ligne par date (et par groupe), et ni `_load_forecast_rows` ni `frequency=month` n'agrègent. L'agent 1421 n'a pas de connexion SQL pour agréger lui-même. Pas une régression de la pile : `git diff v0.2.81 v0.2.84` ne touche ni `tool_create_forecast_dashboard` ni `_load_forecast_rows` (seul `_async_get_dashboard_data`, #384, change dans ce fichier) ; aucun des 10 outils retirés par D1 n'agrège ; l'outil de prévision lit le fichier lui-même (l'aperçu D4 ne le concerne pas). Le banc ne l'avait pas vu : `forecast.json` rejoue une réponse de prévision construite, sans moteur réel, donc son −44,9 % ne vaut pas pour ce chemin. Suite proposée : agréger par date (somme de la cible) dans le chargement de prévision quand une date se répète, ou l'annoncer dans la docstring.
