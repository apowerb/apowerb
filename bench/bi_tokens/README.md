# Tokens d'une conversation BI

`replay.py` rejoue hors ligne des conversations avec un agent BI (« BI
Assistant », gabarit `forecasting_agent`) et mesure chaque requête envoyée au
modèle — consigne, déclarations d'outils, historique — puis vérifie que
l'information utile y est toujours.

```bash
uv pip install sentencepiece                 # facultatif : sans lui, caractères seulement
python bench/bi_tokens/replay.py --estimates # tableau par appel + contrôles qualité
python bench/bi_tokens/replay.py --estimates --compare bench/bi_tokens/results/main.json
python bench/bi_tokens/replay.py --estimates --save <levier>   # résultat versionné
```

Sortie non nulle si un contrôle qualité échoue.

## Scénarios (`scenarios/*.json`)

| Scénario | Ce qu'il exerce | Origine |
|---|---|---|
| `creation` | tableau depuis un CSV joint : import, liste des gabarits, création | DEV 14/10, enregistré |
| `question` | « quel canal a le meilleur ROAS ? » depuis le mini-chat (5 requêtes) | DEV 14/10, enregistré |
| `dashboard_chat` | la question puis une relance (CA hebdomadaire) dans la même session | relance construite |
| `multi_turn` | création, question et relance dans une seule conversation | tours DEV enchaînés |
| `forecast` | prévision à 3 mois depuis le CSV joint : coup d'œil au fichier, tableau de prévision, widget | synthétique |
| `main_chat_spec` | depuis le chat principal : lecture du tableau spec v2 par `tool_get_dashboard_data`, puis ROAS par canal (échouait avant le correctif #384 : lecture vide) | DEV 16/10, appels construits |
| `question_targeted` | ESTIMATION : la question si le modèle ne demande que le KPI ROAS | variante scriptée |

Un tour vient de `scenario.json` (`"recorded"`) ou décrit ses `steps` : les
appels d'outils du modèle, sa réponse, et `measured_input_tokens` quand le tour
a été mesuré en DEV. `facts` (expressions régulières) doivent figurer dans ce
que le modèle voit : au dernier appel du tour, ou à une étape précise.
`env` pose des variables le temps du rejeu (`AGENT_DASHBOARD_ID`) ; `known_failure`
décrit un défaut connu : le scénario est affiché en échec sans faire échouer le
banc, et le test CI exige qu'il échoue encore (à retirer une fois corrigé).

- Le modèle est un script qui refait ces appels. L'agent est construit par
  `to_agent`, la requête passe par la conversion LiteLLM d'ADK : on mesure ce
  qui part.
- Outils LIVE (vrai code) : `tool_bi_list_dashboard_templates`,
  `tool_bi_query_dashboard`, `tool_get_dashboard_data` (sur les valeurs enregistrées) et
  `read_uploaded_file` (sur le CSV de la démo, régénéré à chaque passage :
  13 554 lignes, graine fixe). Les autres renvoient leur réponse enregistrée.
- `tokens` (tokenizer Gemini local) dépasse `llm_usage.input_tokens` mesuré en
  DEV de 9 à 16 % : il compare deux versions, il ne facture rien.

## Qualité

À chaque appel du modèle :

- `declared` : chaque outil appelé par le script est déclaré dans la requête ;
- `tool_ok` : aucun outil LIVE ne répond `success: false` / `status: error` ;
- `grounded` : chaque nombre et lien `/bi/…` de la réponse finale d'un tour
  figure dans ce que le modèle voit en l'écrivant ;
- `empty_reply` : le tour se termine sans aucun texte (budget épuisé en raisonnement,
  par exemple) ; en échec, compté dans `quality.empty_replies`, et `grounded` n'est pas
  évalué sur du vide, où il passerait faute de nombre à vérifier ;
- `facts` : les expressions du scénario y figurent aussi ;
- `undeclared_in_instruction` : outils cités par la consigne mais non déclarés.
  `--compare` marque ❌ un scénario où cette liste s'allonge.

Limite : le modèle étant scripté, ces contrôles disent que l'information
nécessaire reste à sa portée, pas qu'un vrai modèle ferait les mêmes choix.
Un changement qui compte sur un nouveau comportement du modèle (argument
d'outil, consigne élaguée) se juge au mode réel.

## Mode réel (`dev_usage.py`)

Rejouer à la main les messages `user` d'un scénario sur l'agent BI en DEV, puis
lire leur coût dans `llm_usage` (lecture seule) :

```bash
DATABASE_URL=… python bench/bi_tokens/dev_usage.py --schema th2agent_dev --session dashboard-chat-<uuid>
```

`total_tokens` est ce que somme la jauge client, cache compris.

## Résultats

`results/*.json` : un fichier par état mesuré (`main` = avant les leviers).
`RESULTS.md` : le tableau avant/après de chaque levier.

`tests/test_bi_token_budget.py` rejoue les scénarios en CI : contrôles qualité
verts et historique borné.
