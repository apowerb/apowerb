"""
core/extensions/registry.py
---------------------------
Extension registry for client overlays (e.g. th2customers/acme).

The core NEVER imports client-specific modules. Instead, an overlay package
registers its gates/tools/webhooks/templates/schemas here at startup (via
``init_overlay(registry)``), and the core looks them up dynamically.

Contract pieces:
- CallbackSpec : an ADK *callback builder* (not a plain function), with the
  metadata the core needs to wire it generically — schema/db trigger, ordering
  position, activation flag, and an optional ``required_output_key`` guard.
- tool rebinders : per-request dynamic rebinders ``(agent_name, tools_ids,
  tools_funcs, owner_id) -> list`` (e.g. selecting the right DB write config).
- webhook hooks : named extension points ("outcome", "state_keys",
  "fanout.should_split", …, "initial_state_extras").
- templates / schemas : merged into the core registries.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(eq=False)
class CallbackSpec:
    """An ADK callback builder + the metadata to wire it generically.

    ``builder`` is called as ``builder(output_key, agent_details, **kw)`` and
    returns an ADK callback. Identity-based equality (``eq=False``) so specs are
    hashable and compared by object identity.
    """

    trigger: str                              # output_schema_name, e.g. "IntakePayload"
    builder: Callable[..., Any]               # (output_key, agent_details, **kw) -> ADK callback
    position: str = "head"                    # "head" => prepend (prod-critical ordering)
    flag: str | None = None                   # env flag toggling active vs shadow
    required_output_key: str | None = None    # only wire if agent.output_key == this
    db_trigger: str | None = None             # alt trigger: truthy DB column on the agent


@dataclass(frozen=True)
class ToolPack:
    """Une brique d'outils : des schémas et leurs implémentations, appariés par nom.

    Le noyau résout un outil ``"<module>.<fonction>"`` en cherchant ``<module>``
    dans les packs enregistrés, dans l'ordre. Un pack commercial vit donc dans sa
    propre distribution — le noyau ne le nomme jamais, il le trouve.
    """

    schema_package: str
    portfolio_package: str


#: Le pack du noyau. Le noyau a le droit de nommer *son propre* paquet ; la règle
#: qu'il ne doit jamais enfreindre, c'est de nommer celui de quelqu'un d'autre.
#: Il est pré-enregistré pour que la résolution d'outils marche sans configuration
#: — comportement identique à l'ancien chemin en dur.
CORE_TOOL_PACK = ToolPack(
    schema_package="apowerb.tools_store.schema",
    portfolio_package="apowerb.tools_store.portfolio",
)


@dataclass(frozen=True)
class RouterSpec:
    """Un routeur HTTP apporté par une brique, monté par ``main`` sans être nommé."""

    router: Any
    prefix: str = "/api"
    name: str | None = None


class ExtensionRegistry:
    """Process-wide registry populated by overlays, consulted by the core."""

    def __init__(self) -> None:
        self._callbacks: dict[str, list[CallbackSpec]] = defaultdict(list)
        self._gate_appliers: list[tuple[str, Callable[..., bool]]] = []
        self._tool_rebinders: dict[str, Callable[..., Any]] = {}
        self._webhook_hooks: dict[str, Callable[..., Any]] = {}
        self._templates: list[dict] = []
        self._schemas: dict[str, Any] = {}
        self._tools: dict[str, Callable[..., Any]] = {}
        self._tool_packs: list[ToolPack] = [CORE_TOOL_PACK]
        self._routers: list[RouterSpec] = []
        self._second_factor: Callable[[Any], Any | None] | None = None
        # Who may read another account's sessions. The core has no notion of
        # a superadmin — that table belongs to a commercial brick — so it
        # asks instead of deciding. No brick, no crossing.
        self._supervision_scope: Callable[..., Any] | None = None
        # Ou pointe un lien profond vers Supervision. Meme raison :
        # l'ecran peut ne pas etre installe, le noyau ne devine pas.
        self._supervision_link: Callable[..., str] | None = None
        self._run_guards: list[Callable[..., Any]] = []
        self._default_llm_cap: Callable[..., Any] | None = None
        self._model_observers: list[Callable[..., Any]] = []
        self._bootstrap_hooks: list[Callable[[], Any]] = []
        self._feature_flags: dict[str, Callable[[], Any]] = {}
        # Capacites deja fournies, par nom. Le noyau s'enregistre AVANT les
        # briques (main.py, juste avant load_overlay) : une brique qui apporte
        # la meme capacite est alors ignoree plutot que de doubler. Sans cela
        # une brique restee sur l'ancien decoupage compterait chaque jeton
        # deux fois, en silence -- une jauge fausse est pire qu'absente.
        self._provided: set[str] = set()

    def _claim(self, provides: str | None, quoi: str) -> bool:
        """True si l'appelant peut enregistrer. False si deja fourni."""
        if not provides:
            return True
        if provides in self._provided:
            from logging import getLogger

            getLogger(__name__).info(
                "[EXTENSIONS] %s deja fourni, second enregistrement ignore (%s)",
                provides, quoi,
            )
            return False
        self._provided.add(provides)
        return True

    # -- packs d'outils (une brique = schémas + implémentations appariés) ---
    # Le noyau enregistre le sien au démarrage ; une distribution commerciale
    # enregistre le sien depuis SON propre paquet. Aucun nom de pack tiers
    # n'apparaît jamais dans le code du noyau.
    def register_tool_pack(self, schema_package: str, portfolio_package: str) -> None:
        pack = ToolPack(schema_package=schema_package, portfolio_package=portfolio_package)
        if pack not in self._tool_packs:
            self._tool_packs.append(pack)

    def tool_packs(self) -> list[ToolPack]:
        """Packs dans l'ordre d'enregistrement — le noyau d'abord."""
        return list(self._tool_packs)

    # -- second facteur d'authentification ----------------------------------
    # Le noyau vérifie le mot de passe, puis demande ici s'il reste une étape.
    # Aucun crochet => il délivre les jetons, et c'est le comportement complet
    # du noyau open source. Un crochet peut renvoyer une réponse alternative
    # (un défi TOTP, une redirection SSO) ou None pour laisser passer.
    #
    # Ce point existe parce que le MFA, contrairement aux quatre connexions par
    # fournisseur, n'était pas un bloc à retirer : il avait une branche au
    # milieu du flux de connexion lui-même.
    def register_supervision_scope(self, fn: Callable[..., Any]) -> None:
        """`fn(db, user) -> bool`: may this user supervise across accounts?"""
        self._supervision_scope = fn

    def supervision_scope(self) -> Callable[..., Any] | None:
        return self._supervision_scope

    # Ou l'on regarde une reference digne de supervision. Le noyau tient la
    # reference et l'URL publique ; le chemin appartient a l'ecran, donc a la
    # brique. Aucun crochet => l'alerte part sans bouton, ce qui vaut mieux
    # qu'un bouton vers une page que cette installation ne sert pas.
    def register_supervision_link(self, fn: Callable[..., str]) -> None:
        """`fn(base_url, search) -> str`: where to look at `search`."""
        self._supervision_link = fn

    def supervision_link(self) -> Callable[..., str] | None:
        return self._supervision_link

    def register_second_factor(self, fn: Callable[[Any], Any | None]) -> None:
        self._second_factor = fn

    def second_factor(self) -> Callable[[Any], Any | None] | None:
        return self._second_factor

    # -- gardes d'execution -------------------------------------------------
    # Consultees avant chaque run d'agent. Une garde leve pour refuser (402,
    # 429...). Aucune garde => le run passe.
    #
    # Depuis le 09/09/26 le noyau en enregistre une lui-meme : le plafond du
    # modele mutualise est passe open source avec la jauge (decision produit,
    # voir usage_wiring). Une installation qui veut des runs illimites met
    # DEFAULT_LLM_USER_TOKEN_CAP a 0 -- le plafond se desactive par reglage,
    # plus par absence de brique.
    def register_run_guard(
        self, fn: Callable[..., Any], *, provides: str | None = None
    ) -> None:
        if not self._claim(provides, "run_guard"):
            return
        self._run_guards.append(fn)

    def run_guards(self) -> list[Callable[..., Any]]:
        return list(self._run_guards)

    # -- plafond du modele mutualise ----------------------------------------
    # Le noyau COMPTE la consommation de « thaink2/default » (colonne
    # ``llm_usage.billed_to_thaink2``), la sert a la jauge, ET la plafonne :
    # depuis le 09/09/26 le compteur comme la barre sont open source.
    # ``async fn(db, *, owner_id, plan) -> int | None`` (None = illimite).
    # Le point d'extension reste ouvert : une distribution qui vend des plans
    # y branche son propre bareme, qui remplace celui du noyau.
    def register_default_llm_cap(self, fn: Callable[..., Any]) -> None:
        self._default_llm_cap = fn

    def default_llm_cap(self) -> Callable[..., Any] | None:
        return self._default_llm_cap

    # -- observateurs de reponse LLM ----------------------------------------
    # Une fabrique ``(**contexte) -> callback | None`` appelee a la
    # construction d'un agent. Le noyau en enregistre un : l'ecriture de
    # ``llm_usage``, sans laquelle la jauge afficherait zero pour toujours.
    # Le point reste ouvert a une brique qui observerait autre chose.
    def register_model_observer(
        self, fn: Callable[..., Any], *, provides: str | None = None
    ) -> None:
        if not self._claim(provides, "model_observer"):
            return
        self._model_observers.append(fn)

    def model_observers(self) -> list[Callable[..., Any]]:
        return list(self._model_observers)

    # -- crochets de demarrage ----------------------------------------------
    # Joues par ``main.bootstrap()``. Une brique qui a besoin de sa propre
    # table la cree ici, au demarrage reel — jamais a l'import.
    def register_bootstrap_hook(
        self, fn: Callable[[], Any], *, provides: str | None = None
    ) -> None:
        if not self._claim(provides, "bootstrap_hook"):
            return
        self._bootstrap_hooks.append(fn)

    def bootstrap_hooks(self) -> list[Callable[[], Any]]:
        return list(self._bootstrap_hooks)

    # -- drapeaux de fonctionnalite -----------------------------------------
    # Fusionnes dans la reponse de ``GET /api/config``, que le front interroge
    # pour savoir ce qui est disponible. Une brique s'annonce elle-meme plutot
    # que le noyau ne connaisse son existence : sans brique, la cle n'apparait
    # pas et le front n'affiche pas la fonctionnalite.
    #
    # La valeur est une callable, pas une constante : elle est evaluee a chaque
    # appel. Un drapeau fige a l'import est le defaut qui a deja coute
    # ARTIFACTS_DIR et SECRET_KEY.
    def register_feature_flag(self, nom: str, valeur: Callable[[], Any]) -> None:
        self._feature_flags[nom] = valeur

    def feature_flags(self) -> dict[str, Any]:
        return {nom: fn() for nom, fn in self._feature_flags.items()}

    # -- routeurs HTTP ------------------------------------------------------
    def register_router(self, router: Any, prefix: str = "/api", *, name: str | None = None) -> None:
        self._routers.append(RouterSpec(router=router, prefix=prefix, name=name))

    def routers(self) -> list[RouterSpec]:
        return list(self._routers)

    # -- gate appliers (imperative, ordered) --------------------------------
    # A gate applier is a callable ``(agent_details, output_key, agent_kwargs)
    # -> bool`` that decides whether it applies, builds its callback, and
    # composes ``agent_kwargs["before_agent_callback"]`` itself. The core
    # iterates them in registration order, so an overlay reproduces an exact
    # legacy wiring sequence without the core knowing any client specifics.
    def register_gate_applier(self, name: str, fn: Callable[..., bool]) -> None:
        self._gate_appliers.append((name, fn))

    def gate_appliers(self) -> list[tuple[str, Callable[..., bool]]]:
        return list(self._gate_appliers)

    # -- callbacks (schema/db triggered, registration order preserved) --
    def register_callback(self, spec: CallbackSpec) -> None:
        self._callbacks[spec.trigger].append(spec)

    def callbacks(self, trigger: str) -> list[CallbackSpec]:
        return list(self._callbacks.get(trigger, []))

    def all_callbacks(self) -> list[CallbackSpec]:
        return [s for specs in self._callbacks.values() for s in specs]

    # -- tool rebinders (dynamic, per-request) --
    def register_tool_rebinder(self, name: str, rebinder: Callable[..., Any]) -> None:
        self._tool_rebinders[name] = rebinder

    def tool_rebinders(self) -> dict[str, Callable[..., Any]]:
        return dict(self._tool_rebinders)

    # -- overlay tools (resolvable by name outside th2agent portfolio) --
    def register_tool(self, name: str, fn: Callable[..., Any]) -> None:
        self._tools[name] = fn

    def overlay_tools(self) -> dict[str, Callable[..., Any]]:
        return dict(self._tools)

    # -- webhook hooks (named points) --
    def register_webhook_hook(self, point: str, fn: Callable[..., Any]) -> None:
        self._webhook_hooks[point] = fn

    def webhook_hook(self, point: str) -> Callable[..., Any] | None:
        return self._webhook_hooks.get(point)

    # -- templates & schemas --
    def register_templates(self, templates: list[dict]) -> None:
        seen = {t.get("template_id") for t in self._templates}
        for tpl in templates:
            tid = tpl.get("template_id")
            if tid is not None and tid in seen:
                continue
            self._templates.append(tpl)
            seen.add(tid)

    def templates(self) -> list[dict]:
        return list(self._templates)

    def register_schema(self, name: str, schema: Any) -> None:
        self._schemas[name] = schema

    def schemas(self) -> dict[str, Any]:
        return dict(self._schemas)

    def reset(self) -> None:
        """Test helper: clear all registrations."""
        self.__init__()


# Singleton consulted by the core; overlays populate it via init_overlay(registry).
registry = ExtensionRegistry()
