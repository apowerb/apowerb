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
