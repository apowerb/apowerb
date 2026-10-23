# Glossaire métier (généré)

> Généré par `python3 scripts/glossaire.py` depuis les docstrings de `src/apowerb/models.py`, `src/apowerb/schema`.
> Ne pas éditer ce fichier : corriger la docstring ou le `Field(description=...)` dans le code, puis régénérer.
> « (pas de docstring) » marque un modèle sans définition : c'est une dette à combler.

## src/apowerb/models.py

- **Sender** : Valeurs : `USER`, `SYSTEM`.
- **UserRole** : Valeurs : `ADMIN`, `USER`.
- **Status** : Valeurs : `PENDING`, `COMPLETED`, `FAILED`.
- **User** : (pas de docstring)
- **Transaction** : Track all credit transactions (purchases, usage, refunds)
- **CreditPurchase** : Track credit purchase sessions
- **Integration** : Stores third-party integration tokens per user (GitHub, Google, etc.)
- **WebhookSubscription** : Stores Microsoft Graph (and future provider) webhook subscriptions.
- **WebhookLog** : Backlog + execution log for webhook-triggered agent runs.
- **Notification** : User notifications for webhook events, system alerts, etc.
- **BIItemType** : Valeurs : `CHART`, `DATA`, `DASHBOARD`.
- **BIItemStatus** : Valeurs : `ACTIVE`, `ARCHIVED`, `DRAFT`, `DELETED`.
- **BusinessIntelligence** : (pas de docstring)
- **BIForecastSnapshot** : Instantané d'une réponse de prévision, pour le suivi en boucle fermée (contrat étape 5 §3).
- **BIForecastAlert** : Reservation de notification de rupture de prevision (contrat etape 7 SS2c) : une ligne par (chart_id, group_key, level_key, date) deja notifiee, pour ne jamais renvoyer deux fois la meme alerte a chaque affichage du widget. `group_key`/`level_key` normalisent `None` en chaine vide -- deux NULL ne sont jamais egaux dans une UniqueConstraint (Postgres comme SQLite), donc les laisser nullable romprait le dedoublonnage pour toute serie sans hierarchie.
- **BIDashboardDigest** : Abonnement d'un proprietaire a un resume periodique (et aux alertes KPI) d'un tableau de bord spec v2 : une ligne par (dashboard_id, owner_email).
- **OAuthState** : One-time CSRF state tokens for OAuth authorisation flows.
- **SharedConversation** : (pas de docstring)
- **LlmUsage** : One row per completed LLM model turn (see ``apowerb.core.agent_helpers.usage_recorder``). Best-effort accounting: rows can be missing on DB hiccups, never guaranteed exhaustive — used for cost/usage reporting, not billing-grade audit. Rows are lost only on DB failure or a process kill mid-write; the write task's reference is held in ``usage_recorder._pending_writes`` (a done-callback removes it once the INSERT completes) so it cannot be silently garbage-collected before landing.
- **BugReport** : Un défaut signalé depuis l'application, avec de quoi le reproduire.
- **BugReportEvent** : Ce qui est arrivé à un signalement, dans l'ordre.

## src/apowerb/schema/adk_runner_schema.py

- **MessagePart** : (pas de docstring)
- **NewMessage** : (pas de docstring)
- **RunADKAgentRequest** : Schema for running an ADK agent.
- **UpdateADKAgentSessionRequest** : Schema for updating an ADK agent session.
- **CreateADKAgentSessionRequest** : Schema for creating an ADK agent session.
- **DeleteADKAgentSessionRequest** : Schema for deleting an ADK agent session.
- **GenerateTitleRequest** : Schema for auto-generating a conversation title from its first message.

## src/apowerb/schema/agent_schema.py

- **McpServerConfig** : Configuration for a single MCP server attached to an agent.
- **AgentCreateSchema** : Schema for creating a new agent. Attributes: name: str - The name of the agent. model: str - The model used by the agent. description: str - A brief description of the agent. instruction: str - Instructions for the agent's behavior. tools: list[str] - A list of tool paths used by the agent. input_schema: dict | None - The input schema for the agent (optional). output_schema: dict | None - The output schema for the agent (optional). organization_id: str - The ID of the organization the agent belongs to. project_id: str - The ID of the project the agent is associated with. owner_id: str - The ID of the owner of the agent. tags: list[str] | None - A list of tags associated with the agent (optional).

## src/apowerb/schema/api_key_schema.py

- **ApiKeyCreateSchema** : Schema for creating a saved API key.

## src/apowerb/schema/bug_report_schema.py

- **BugSeverity** : Ce que l'utilisateur ne pouvait plus faire, pas « à quel point il est agacé ». Valeurs : `BLOCKER`, `MAJOR`, `MINOR`, `COSMETIC`.
- **BugReportStatus** : Valeurs : `NEW`, `TRIAGED`, `ISSUE_CREATED`, `DUPLICATE`, `REJECTED`, `RESOLVED`.
- **ApiCallEntry** : Un appel d'API tel que le client l'a vu.
- **ConsoleEntry** : (pas de docstring)
- **NavigationStep** : Un écran traversé avant le défaut.
- **LastAction** : La dernière chose que l'utilisateur a faite avant que ça casse.
- **ClientContext** : L'état de l'application au moment du défaut.
- **BugReportCreate** : Ce qu'envoie le bouton « Signaler un problème ».
- **BugReportSummary** : (pas de docstring)
- **BugReportListResponse** : (pas de docstring)
- **BugReportCreated** : Réponse à l'envoi.
- **AreaOption** : Une entrée du menu déroulant des fonctionnalités.
- **BugReportUpdate** : (pas de docstring)

## src/apowerb/schema/data_lake_schema.py

- **BoardConfigSchema** : Configuration for creating/connecting to a storage board.
- **PinWriteSchema** : Schema for writing a pin to the board.
- **PinReadSchema** : Schema for reading a pin (used when query params are not enough).
- **FilterConditionSchema** : Schema for a single filter condition.
- **AggregationConfigSchema** : Schema for a single aggregation configuration.
- **DataProcessSchema** : Schema for processing a dataset (filter + aggregate).
- **DataLakeProcessSchema** : Schema for processing data directly from a data lake pin.

## src/apowerb/schema/forecast_schema.py

- **ForecastRequestSchema** : Body of POST /api/v1/forecast — relayed to th2forecast almost verbatim.
- **ForecastInterpretSchema** : Body of POST /api/v1/forecast/interpret — free text to dated events.

## src/apowerb/schema/hub_schema.py

- **HubPublishSchema** : Schema for publishing an agent to the Hub.
- **HubCloneSchema** : Schema for cloning an agent from the Hub.

## src/apowerb/schema/notification_schema.py

- **NotificationResponse** : A single notification as returned by the API.
- **NotificationListResponse** : Paginated list of notifications.
- **UnreadCountResponse** : Badge count for unread notifications.

## src/apowerb/schema/share_schema.py

- **ShareMessageIn** : (pas de docstring)
- **ShareCreateRequest** : (pas de docstring)
- **ShareCreateResponse** : (pas de docstring)
- **SharedConversationResponse** : (pas de docstring)
- **ShareListItem** : (pas de docstring)

## src/apowerb/schema/skill_schema.py

- **SkillCreateSchema** : (pas de docstring)
- **SkillUpdateSchema** : (pas de docstring)

## src/apowerb/schema/tool_config_schema.py

- **ToolConfigCreateSchema** : Schema for creating a new tool_config.

## src/apowerb/schema/usage_schema.py

- **UsageTotals** : Aggregated usage over a window (whole scope, one agent, ...).
- **PerAgentUsage** : Aggregated usage for one agent (all its models merged).
- **SeriesPoint** : One time bucket -- a calendar day (``YYYY-MM-DD``) or an hour (``YYYY-MM-DDTHH:00``), always in Europe/Paris. Only non-empty buckets are returned; the front-end fills the gaps.
- **PerModelUsage** : (pas de docstring)
- **PerSourceUsage** : (pas de docstring)
- **HeatmapCell** : Usage by weekday x hour, Europe/Paris. ``dow``: 0 = Monday.
- **UsageSummaryResponse** : (pas de docstring)
- **PerSessionUsage** : (pas de docstring)
- **TurnProfilePoint** : Average prompt size at turn N of an invocation.
- **PerToolUsage** : Prompt growth attributable to one tool.
- **TopInvocation** : (pas de docstring)
- **UsageDrivers** : What makes the tokens burn. All fields degrade to empty/zero on rows written before the drivers instrumentation (``invocation_id`` and ``tool_names`` are NULL there) -- the UI must say "no instrumented data on this window", not show an error.
- **AgentUsageDetailResponse** : (pas de docstring)
- **QuotaStatusResponse** : Quota mensuel de tokens sur le modèle thaink2 mutualisé.

## src/apowerb/schema/webhook_schema.py

- **WebhookSubscriptionCreate** : Request to create a new webhook subscription.
- **WebhookSubscriptionUpdate** : Request to update an existing webhook subscription (all fields optional).
- **WebhookSubscriptionResponse** : Response after creating a subscription.
- **WebhookSubscriptionList** : List of subscriptions.
- **MicrosoftGraphNotification** : A single notification from Microsoft Graph.
- **MicrosoftGraphNotificationPayload** : The full notification payload from Microsoft Graph.
- **GmailPubSubMessage** : The inner message from a Pub/Sub push delivery.
- **GmailPubSubNotification** : Full Pub/Sub push notification payload.
