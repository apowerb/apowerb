# Tokens d'une conversation BI

`replay.py` rejoue hors ligne la conversation filmée pour la démo BI (agent
« BI Assistant », gabarit `forecasting_agent`, DEV, 14/10/2026) et mesure chaque
requête envoyée au modèle : consigne, déclarations d'outils, historique.

```bash
uv pip install sentencepiece          # facultatif : sans lui, caractères seulement
python bench/bi_tokens/replay.py --targeted
```

- Le modèle est un script qui refait les appels d'outils enregistrés
  (`scenario.json`, données fictives). L'agent est construit par `to_agent`,
  la requête passe par la conversion LiteLLM d'ADK : on mesure ce qui part.
- `tool_bi_list_dashboard_templates` et `tool_bi_query_dashboard` tournent pour
  de vrai sur les valeurs enregistrées ; les autres outils renvoient leur
  réponse enregistrée.
- `tokens` (tokenizer Gemini local) dépasse `llm_usage.input_tokens` mesuré en
  DEV de 9 à 16 % : il compare deux versions, il ne facture rien.
- `--targeted` ajoute une ESTIMATION : la question, si le modèle ne demande que
  le KPI ROAS par canal (`component_ids`), ce qu'un vrai modèle peut ne pas faire.

`tests/test_bi_token_budget.py` rejoue le même scénario en CI et borne les
caractères envoyés.
