# Banc suggestions du nœud suivant — 2026-09-23

Modèle `gemini/gemini-2.5-flash`, délai 4.0 s, 44 cas × 3 passages = 132 appels.
Règles : copie vérifiée contre `apowerb-ui src/lib/nextNodeSuggestions.js`.

## Verdict : **NO-GO**

- Top 3 : règles seules 63.6 %, règles + modèle 63.6 % (par passage : 63.6 %, 63.6 %, 63.6 %) → **+0.0 points** (seuil +15).
- Latence : p50 1.501 s, **p95 2.779 s** (seuil < 3 s), max 4.039 s.

## Détail

- Top 1 : règles 31.8 %, règles + modèle 42.4 %.
- Le modèle propose le bon type dans 39.4 % des appels ; 0.88 proposition(s) retenue(s) par réponse en moyenne.
- Réponses utilisables : 97.7 % ; échecs : {'timeout': 3}.
- Branche à câbler juste : 100.0 % (calculée par le serveur, pas par le modèle).
- Configuration, quand le modèle a le bon type : bon agent 100.0 %, lit le nœud source 50.0 %.
- Jetons par appel : 650.2 en entrée, 190.8 en sortie.
- Coût : 0.5864 € les 1 000 → 0.000586 € par appel (grille litellm ; 1 € = 1.1463 $, BCE 2026-09-22).

## Cas

| Cas | Attendu | Règles (top 3) | Modèle (par passage) | Top 3 règles / + modèle |
|---|---|---|---|---|
| `support-triage/trigger1->classifier1` | classifier | agent, classifier, convert | agent · agent · ∅ | ✓ / 3/3 |
| `support-triage/classifier1->agent1` | agent (billing) | output, agent | agent · agent · agent | ✓ / 3/3 |
| `support-triage/classifier1->rag1` | rag (technical) | output, agent | ∅ · ∅ · ∅ | ✗ / 0/3 |
| `support-triage/classifier1->notification1` | notification (other) | output, agent | agent · agent,output · agent,output | ✗ / 0/3 |
| `support-triage/agent1->output1` | output | output, condition, notification | output · output · output | ✓ / 3/3 |
| `support-triage/rag1->agent2` | agent | agent, output | agent,output · agent,output · agent,output | ✓ / 3/3 |
| `support-triage/agent2->output2` | output | output, condition, notification | output · output · output | ✓ / 3/3 |
| `invoice-intake/trigger1->extract1` | extract | agent, classifier, convert | ∅ · ∅ · ∅ | ✗ / 0/3 |
| `invoice-intake/extract1->condition1` | condition | output, condition, notification | output · agent · ∅ | ✓ / 3/3 |
| `invoice-intake/condition1->notification1` | notification (true) | output, agent | output · output · output | ✗ / 0/3 |
| `invoice-intake/condition1->set1` | set (false) | output, agent | output · output · output | ✗ / 0/3 |
| `invoice-intake/set1->output1` | output | output, http, notification | output · output · output | ✓ / 3/3 |
| `lead-qualification/trigger1->agent1` | agent | agent, classifier, convert | agent,output · agent,output · agent,output | ✓ / 3/3 |
| `lead-qualification/agent1->convert1` | convert | output, condition, notification | ∅ · ∅ · ∅ | ✗ / 0/3 |
| `lead-qualification/convert1->condition1` | condition | output, agent, condition | ∅ · ∅ · ∅ | ✓ / 3/3 |
| `lead-qualification/condition1->notification1` | notification (true) | output, agent | ∅ · ∅ · ∅ | ✗ / 0/3 |
| `lead-qualification/condition1->output1` | output (false) | output, agent | ∅ · ∅ · ∅ | ✓ / 3/3 |
| `daily-digest/trigger1->http1` | http | agent, classifier, convert | agent · timeout · agent | ✗ / 0/3 |
| `daily-digest/http1->convert1` | convert | output, convert, notification | agent · agent · agent | ✓ / 3/3 |
| `daily-digest/convert1->agent1` | agent | output, agent, condition | agent,output · agent,output · agent,output | ✓ / 3/3 |
| `daily-digest/agent1->notification1` | notification | output, condition, notification | output · ∅ · output | ✓ / 3/3 |
| `daily-digest/notification1->output1` | output | output | output · output · output | ✓ / 3/3 |
| `faq-answers/trigger1->rag1` | rag | agent, classifier, convert | ∅ · output · ∅ | ✗ / 0/3 |
| `faq-answers/rag1->agent1` | agent | output, agent | agent,output · timeout · agent,output | ✓ / 3/3 |
| `faq-answers/agent1->output1` | output | output, condition, notification | output · output · output | ✓ / 3/3 |
| `contract-review/trigger1->extract1` | extract | agent, classifier, convert | ∅ · ∅ · output | ✗ / 0/3 |
| `contract-review/extract1->router1` | router | output, condition, notification | output · output · output | ✗ / 0/3 |
| `contract-review/router1->agent1` | agent (risky) | output, agent | ∅ · ∅ · ∅ | ✓ / 3/3 |
| `contract-review/router1->output1` | output (standard) | output, agent | output · timeout · output | ✓ / 3/3 |
| `contract-review/agent1->notification1` | notification | output, condition, notification | output · output · output | ✓ / 3/3 |
| `ticket-translation/trigger1->condition1` | condition | agent, classifier, convert | agent · agent · agent | ✗ / 0/3 |
| `ticket-translation/condition1->agent1` | agent (true) | output, agent | agent · agent · agent | ✓ / 3/3 |
| `ticket-translation/condition1->output1` | output (false) | output, agent | output · output · output | ✓ / 3/3 |
| `ticket-translation/agent1->output2` | output | output, condition, notification | output · output · output | ✓ / 3/3 |
| `customer-onboarding/trigger1->set1` | set | agent, classifier, convert | agent · agent · agent | ✗ / 0/3 |
| `customer-onboarding/set1->http1` | http | output, http, notification | agent,output · agent,output · agent | ✓ / 3/3 |
| `customer-onboarding/http1->agent1` | agent | output, convert, notification | output · ∅ · ∅ | ✗ / 0/3 |
| `customer-onboarding/agent1->notification1` | notification | output, condition, notification | output · output · output | ✓ / 3/3 |
| `customer-onboarding/notification1->output1` | output | output | output · output · output | ✓ / 3/3 |
| `order-review/trigger->order` | set | agent, classifier, convert | agent · agent · agent | ✗ / 0/3 |
| `order-review/order->big` | condition | output, http, notification | agent · agent · agent | ✗ / 0/3 |
| `order-review/big->review` | agent (true) | output, agent | agent · agent · agent | ✓ / 3/3 |
| `order-review/big->skip` | output (false) | output, agent | output · output · output | ✓ / 3/3 |
| `order-review/review->done` | output | output, condition, notification | output · output · output | ✓ / 3/3 |
