# th2agent — vision produit et cadre de travail

> À lire avant toute décision d'architecture, de packaging ou d'API publique.
> Source : réunion Farid ↔ David du 28/07/2026. État du code vérifié le 28/07/2026.

## 1. Le modèle : open core

thaink2 vend **du sur-mesure** (plateforme + composante métier custom — le modèle SCEI),
cible PME. th2agent n'est pas le produit vendu : c'est **le levier de visibilité** et
l'outil qui rend le sur-mesure rapide à livrer.

Deux portes d'entrée à construire, dans cet ordre :

| Priorité | Porte | Contenu | Modèle économique |
|---|---|---|---|
| **1** | **Get started** — Studio hébergé | l'app telle quelle, SaaS | freemium : LLM mutualisé acheté ~5 €/M tokens, revendu ~10 €/M, coupon d'entrée ~20 € |
| **2** | **Self-host** | pypi + images Docker + Helm chart + docker-compose + CLI + doc | open source, référence assumée = **n8n** (community edition) |

**Le Studio d'abord.** On le finalise, on l'ouvre en freemium, on communique. Le volet
self-host vient ensuite — Farid le décrit comme « la composante la plus challengeante ».

## 2. La ligne open source / commercial

Modèle n8n : la version communautaire est complète **sauf l'entreprise**.

- **Reste commercial** : SSO / SAML / LDAP, la partie logs, la partie usage/consommation.
  (dans les termes de Farid : « version sans auth advanced »)
- **Part en OSS** : le cœur agentique, les tools, l'ETL, l'observabilité, le RAG.

Périmètre OSS arrêté : `th2agent`, `th2agent-front`, `th2etl`, `th2pulse`, `th2rag`.

⚠️ **Conséquence directe pour le code** : tout ce qui touche à l'auth avancée, au logging
et à l'usage doit rester **désactivable par feature flag**, jamais entrelacé au cœur.
Le repo contient déjà le bon réflexe (`auth_basic_enabled`, `auth_register_enabled`,
`scheduler_enabled`) — le prolonger, ne pas le contourner.

## 3. Monétisation — décision NON tranchée

Farid n'a pas arbitré, il doit en discuter avec Anis. Trois options sur la table :
vendre des **tokens**, vendre du **crédit**, ou monétiser aussi **l'usage de fonctionnalités
et de tools**.

**Ne pas implémenter le modèle de facturation tant que ce n'est pas tranché.**
Ce qui est sûr et déjà acté : consommation comptée **par mois**, adossée à Stripe.

## 4. État réel du code (vérifié le 28/07/2026)

Ce qui existe et fonctionne :

- **Quota tokens** — `core/usage_quota.py`, `helpers/quota_guard.py`.
  Par utilisateur, en tokens, mois calendaire Europe/Paris, refus net en 402
  `QUOTA_EXCEEDED` avant le début du flux. Plafond : `DEFAULT_LLM_MONTHLY_TOKEN_QUOTA`
  (1 000 000 par défaut) surchargeable par plan via `DEFAULT_LLM_PLAN_QUOTAS`.
- **Comptabilité** — `llm_usage` + `helpers/model_pricing.py` (⚠️ tarif cache = 10 % input).
  Documenté *best-effort* : garde-fou oui, **source de vérité d'une facture non**.
- **Stripe** — `billing/` complet : packages de crédits, checkout, balance, transactions,
  customer portal, webhook signé.
- **Packaging** — PR **#311** (library importable : 4 verrous levés — config exigée à
  l'import, DDL de 5 stores au niveau module, dépendance au CWD, entry point mort) et
  **#312** (LICENSE MIT, métadonnées, `requires-python >=3.12`, et `build.yaml` qui vérifie
  en CI que le wheel s'installe et s'importe dans un venv nu sous `env -i`).
  **Ouvertes, mergeables, jamais relues.** #312 est empilée sur #311 : merger #311 d'abord.

**Le chaînon manquant, identifié par Farid en réunion :** l'achat Stripe crédite
`User.credits`, et `resolve_quota()` lit `User.plan`. **Rien ne relie les deux** — aucun
code n'écrit `User.plan`, et aucune consommation ne débite `User.credits`. Les deux moitiés
existent et ne se parlent pas. C'est le vrai travail restant côté Studio.

Ce qui **n'existe pas encore** (contrairement à ce que le schéma pourrait laisser croire) :
Dockerfile, docker-compose, Helm chart, repo de configs self-host, doc d'installation.
Sur PyPI, seul `th2etl` est publié — `th2agent` n'y est pas.

## 5. Règles pour toute session travaillant ici

1. **Ne rien publier sur PyPI.** Farid s'en occupe personnellement et a demandé d'attendre
   son feu vert ; il doit d'abord basculer la propriété des paquets de son compte perso vers
   l'organisation TH2 et activer le billing. David a de son côté tranché « pas de publication
   pour le moment ». `#312` ne contient volontairement **aucun** `publish.yaml` — ne pas en
   ajouter. Publier th2agent = ouvrir le code du produit commercial : c'est une décision
   produit, pas une étape de packaging.
2. **Toute API ou tout module nouveau est un futur artefact public.** Nommage, signatures et
   valeurs par défaut engagent. Pas de valeur codée en dur propre à un client dans le cœur
   (cf. SCEI : le métier vit dans le template et la DB, pas dans l'app).
3. **L'import doit rester inerte.** #311 a levé les 4 verrous qui rendaient th2agent
   inimportable (config exigée à l'import, DDL au niveau module, dépendance au CWD, entry
   point mort). Toute régression sur ce point casse le packaging : la CI `build.yaml` de #312
   la verrouille, ne pas la contourner.
4. **Licence : MIT**, alignée sur th2etl et th2pulse. Farid a évoqué « s'inspirer de n8n » —
   or n8n n'est pas MIT mais sous licence *sustainable use*. La contradiction n'est pas
   tranchée ; ne pas changer la licence sans arbitrage explicite de Farid.
5. Le déploiement de référence reste `iac/deploy.sh` sur VM. Les défauts de `configs/paths.py`
   reproduisent le comportement historique — ne pas les modifier sans vérifier les VM.
6. Le travail se fait sur **DAVE_OVH_VM** (worktrees + `.venv` de `~/th2ai/th2agent`), pas en
   local. Baseline de la suite : **103 failed / 40 errors / ~2224 passed**, en excluant
   `tests/test_audio_stream_router.py` (fige sur un futex) et `tests/test_integration_auth.py`
   (ne collecte pas via `PYTHONPATH=src`) — deux anomalies pré-existantes.

## 6. Écosystème

| Composant | GitHub | PyPI | Rôle |
|---|---|---|---|
| `th2agent` | privé | ❌ | cœur agentique (ce repo) |
| `th2agent-app` | privé | ❌ | front Next.js, extraction en paquets en cours |
| `th2pulse` | **public**, MIT | ❌ | observabilité OTel — le modèle de packaging à répliquer |
| `th2etl` | privé | ✅ **publié** | ETL / orchestration |
| `th2rag` | *n'existe pas encore* | ❌ | RAG — à extraire (ce n'est plus th2llm qui le porte) |

Communauté à venir : serveur Discord (créé par Farid) + GitHub issues, souscriptions
Discord / GitHub Enterprise / Docker Hub / PyPI à prendre. Les PR externes sont acceptées ou
refusées à la discrétion de l'équipe.
