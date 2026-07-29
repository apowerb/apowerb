"""TDD - Instrumentation TOKEN_DEBUG v2 dans create_truncate_history_callback.

Tests unitaires purs (aucune connexion DB).
Chargement direct du module via importlib pour eviter __init__.py (DB).

Spec v2 validee :
- per_message : {role, text_chars, fc_chars, fr_chars, total_chars} (pas 'chars' seul)
- grand_total_chars : somme de tous les total_chars
- biggest_part : {msg_index, role, kind, chars} — element individuel le plus lourd
- system_instruction_chars : taille du system_instruction
- tools_chars : taille serialisee des declarations d'outils
- fc_chars / fr_chars > 0 pour parts de type function_call / function_response
- RGPD strict : aucun contenu dans les logs, uniquement des tailles + noms structurels

MODIFICATIONS DE TESTS EXISTANTS (justification documentee) :
- test_log_contains_total_chars : renomme grand_total_chars (spec v2 change le champ)
- test_log_contains_per_message_list : per_message schema etendu (role + 4 champs tailles)
- test_per_message_chars_correct : verifie text_chars au lieu de chars
- test_per_message_has_only_role_and_chars : cles autorisees etendues au schema v2
- test_log_contains_tool_results : remplace biggest_part (tool_results retire en v2)
"""

from __future__ import annotations

import importlib.util
import json
import logging
import pathlib
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import google.genai.types as genai_types


_SRC = pathlib.Path(__file__).parent.parent / "src"


def _load_callbacks():
    """Charge callbacks.py directement par chemin pour eviter la DB."""
    name = "_th2agent_callbacks_token_debug"
    if name in sys.modules:
        del sys.modules[name]
    full = _SRC / "th2agent/core/agent_helpers/callbacks.py"
    spec = importlib.util.spec_from_file_location(name, str(full))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _mk_content(role: str, texts: list) -> Any:
    """Construit un vrai Content ADK avec parts texte."""
    parts = [genai_types.Part(text=t) for t in texts]
    return genai_types.Content(role=role, parts=parts)


def _mk_content_tool_response(text: str) -> Any:
    """Simule un tool response (role=tool) avec texte."""
    parts = [genai_types.Part(text=text)]
    return genai_types.Content(role="tool", parts=parts)


def _mk_content_with_fc(role: str, name: str, args: dict) -> Any:
    """Construit un Content ADK avec une part function_call."""
    fc = genai_types.FunctionCall(name=name, args=args)
    parts = [genai_types.Part(function_call=fc)]
    return genai_types.Content(role=role, parts=parts)


def _mk_content_with_fr(role: str, name: str, response: dict) -> Any:
    """Construit un Content ADK avec une part function_response."""
    fr = genai_types.FunctionResponse(name=name, response=response)
    parts = [genai_types.Part(function_response=fr)]
    return genai_types.Content(role=role, parts=parts)


def _mk_request(contents: list, system_instruction=None, tools=None) -> Any:
    """Construit un faux llm_request avec .contents et optionnellement .config."""
    from google.genai.types import GenerateContentConfig
    req = MagicMock()
    req.contents = contents
    req.config = GenerateContentConfig(
        system_instruction=system_instruction,
        tools=tools,
    )
    return req


def _mk_ctx(agent_name: str = "test_agent") -> Any:
    """Construit un faux callback_context."""
    return SimpleNamespace(agent_name=agent_name)


def _build_long_history(keep_recent: int = 14) -> list:
    """Construit une liste de messages qui DEPASSE keep_recent+1 pour
    declencher la troncature."""
    msgs = [_mk_content("user", ["payload initial"])]
    for i in range(keep_recent + 2):
        msgs.append(_mk_content("model", [f"reponse modele {i}"]))
    return msgs


def _extract_debug_data(caplog_records):
    """Extrait le dict JSON du premier log [TOKEN_DEBUG]."""
    record = next(r for r in caplog_records if "[TOKEN_DEBUG]" in r.message)
    msg = record.message
    json_part = msg[msg.index("{"):]
    return json.loads(json_part)


# ---------------------------------------------------------------------------
# (a) Avec TH2_TOKEN_DEBUG=1 : log emis avec les bons champs
# ---------------------------------------------------------------------------

class TestTokenDebugEnabled:
    def test_log_emitted_when_debug_flag_set(self, monkeypatch, caplog):
        """Avec TH2_TOKEN_DEBUG=1, un log [TOKEN_DEBUG] est emis au niveau INFO."""
        monkeypatch.setenv("TH2_TOKEN_DEBUG", "1")
        mod = _load_callbacks()
        cb = mod.create_truncate_history_callback(keep_recent=14)
        contents = _build_long_history(keep_recent=14)
        req = _mk_request(contents)
        ctx = _mk_ctx("my_agent")

        with caplog.at_level(logging.INFO, logger="th2agent.truncate_history"):
            cb(callback_context=ctx, llm_request=req)

        token_debug_records = [r for r in caplog.records if "[TOKEN_DEBUG]" in r.message]
        assert len(token_debug_records) == 1, (
            f"Attendu 1 log [TOKEN_DEBUG], obtenu {len(token_debug_records)}"
        )

    def test_log_contains_n_messages(self, monkeypatch, caplog):
        """Le log contient le champ n_messages."""
        monkeypatch.setenv("TH2_TOKEN_DEBUG", "1")
        mod = _load_callbacks()
        cb = mod.create_truncate_history_callback(keep_recent=14)
        contents = _build_long_history(keep_recent=14)
        req = _mk_request(contents)

        with caplog.at_level(logging.INFO, logger="th2agent.truncate_history"):
            cb(callback_context=_mk_ctx(), llm_request=req)

        data = _extract_debug_data(caplog.records)
        assert "n_messages" in data, f"Champ n_messages absent : {data}"
        assert isinstance(data["n_messages"], int)
        assert data["n_messages"] > 0

    def test_log_contains_grand_total_chars(self, monkeypatch, caplog):
        """v2 : le log contient grand_total_chars (et non plus total_chars au top)."""
        monkeypatch.setenv("TH2_TOKEN_DEBUG", "1")
        mod = _load_callbacks()
        cb = mod.create_truncate_history_callback(keep_recent=14)
        contents = _build_long_history(keep_recent=14)
        req = _mk_request(contents)

        with caplog.at_level(logging.INFO, logger="th2agent.truncate_history"):
            cb(callback_context=_mk_ctx(), llm_request=req)

        data = _extract_debug_data(caplog.records)
        assert "grand_total_chars" in data, f"Champ grand_total_chars absent : {data}"
        assert isinstance(data["grand_total_chars"], int)
        assert data["grand_total_chars"] >= 0

    def test_log_contains_agent_name(self, monkeypatch, caplog):
        """Le log contient le nom de l'agent."""
        monkeypatch.setenv("TH2_TOKEN_DEBUG", "1")
        mod = _load_callbacks()
        cb = mod.create_truncate_history_callback(keep_recent=14)
        contents = _build_long_history(keep_recent=14)
        req = _mk_request(contents)
        ctx = _mk_ctx("scei_agent_42")

        with caplog.at_level(logging.INFO, logger="th2agent.truncate_history"):
            cb(callback_context=ctx, llm_request=req)

        data = _extract_debug_data(caplog.records)
        assert data.get("agent") == "scei_agent_42", f"Champ agent incorrect : {data}"

    def test_log_contains_per_message_list_v2_schema(self, monkeypatch, caplog):
        """v2 : per_message contient {role, text_chars, fc_chars, fr_chars, total_chars}."""
        monkeypatch.setenv("TH2_TOKEN_DEBUG", "1")
        mod = _load_callbacks()
        cb = mod.create_truncate_history_callback(keep_recent=14)
        contents = _build_long_history(keep_recent=14)
        req = _mk_request(contents)

        with caplog.at_level(logging.INFO, logger="th2agent.truncate_history"):
            cb(callback_context=_mk_ctx(), llm_request=req)

        data = _extract_debug_data(caplog.records)
        assert "per_message" in data, f"Champ per_message absent : {data}"
        assert isinstance(data["per_message"], list)
        for item in data["per_message"]:
            assert "role" in item, f"per_message item manque role : {item}"
            assert "text_chars" in item, f"per_message item manque text_chars : {item}"
            assert "fc_chars" in item, f"per_message item manque fc_chars : {item}"
            assert "fr_chars" in item, f"per_message item manque fr_chars : {item}"
            assert "total_chars" in item, f"per_message item manque total_chars : {item}"
            assert isinstance(item["text_chars"], int)
            assert isinstance(item["fc_chars"], int)
            assert isinstance(item["fr_chars"], int)
            assert isinstance(item["total_chars"], int)
            assert item["total_chars"] == item["text_chars"] + item["fc_chars"] + item["fr_chars"]

    def test_log_contains_biggest_part(self, monkeypatch, caplog):
        """v2 : biggest_part contient {msg_index, role, kind, chars}."""
        monkeypatch.setenv("TH2_TOKEN_DEBUG", "1")
        mod = _load_callbacks()
        cb = mod.create_truncate_history_callback(keep_recent=14)
        contents = _build_long_history(keep_recent=14)
        req = _mk_request(contents)

        with caplog.at_level(logging.INFO, logger="th2agent.truncate_history"):
            cb(callback_context=_mk_ctx(), llm_request=req)

        data = _extract_debug_data(caplog.records)
        assert "biggest_part" in data, f"Champ biggest_part absent : {data}"
        bp = data["biggest_part"]
        assert "msg_index" in bp, f"biggest_part manque msg_index : {bp}"
        assert "role" in bp, f"biggest_part manque role : {bp}"
        assert "kind" in bp, f"biggest_part manque kind : {bp}"
        assert "chars" in bp, f"biggest_part manque chars : {bp}"
        assert bp["kind"] in ("text", "function_call", "function_response"), (
            f"biggest_part.kind invalide : {bp['kind']}"
        )

    def test_log_contains_system_instruction_chars(self, monkeypatch, caplog):
        """v2 : system_instruction_chars est logge."""
        monkeypatch.setenv("TH2_TOKEN_DEBUG", "1")
        mod = _load_callbacks()
        cb = mod.create_truncate_history_callback(keep_recent=14)
        contents = _build_long_history(keep_recent=14)
        si = "Tu es un assistant specialise SCEI."
        req = _mk_request(contents, system_instruction=si)

        with caplog.at_level(logging.INFO, logger="th2agent.truncate_history"):
            cb(callback_context=_mk_ctx(), llm_request=req)

        data = _extract_debug_data(caplog.records)
        assert "system_instruction_chars" in data, (
            f"Champ system_instruction_chars absent : {data}"
        )
        si_chars = data["system_instruction_chars"]
        assert isinstance(si_chars, int)
        # -1 = introuvable, sinon doit etre la taille reelle
        if si_chars != -1:
            assert si_chars == len(si), (
                f"system_instruction_chars={si_chars} attendu {len(si)}"
            )

    def test_log_contains_tools_chars(self, monkeypatch, caplog):
        """v2 : tools_chars est logge."""
        monkeypatch.setenv("TH2_TOKEN_DEBUG", "1")
        from google.genai.types import Tool, FunctionDeclaration
        mod = _load_callbacks()
        cb = mod.create_truncate_history_callback(keep_recent=14)
        contents = _build_long_history(keep_recent=14)
        fd = FunctionDeclaration(name="tool_sql", description="Execute SQL")
        tools = [Tool(function_declarations=[fd])]
        req = _mk_request(contents, tools=tools)

        with caplog.at_level(logging.INFO, logger="th2agent.truncate_history"):
            cb(callback_context=_mk_ctx(), llm_request=req)

        data = _extract_debug_data(caplog.records)
        assert "tools_chars" in data, f"Champ tools_chars absent : {data}"
        tc = data["tools_chars"]
        assert isinstance(tc, int)
        if tc != -1:
            assert tc > 0, f"tools_chars={tc} devrait etre > 0"

    def test_per_message_text_chars_correct(self, monkeypatch, caplog):
        """v2 : per_message.text_chars correspond exactement aux chars de texte."""
        monkeypatch.setenv("TH2_TOKEN_DEBUG", "1")
        mod = _load_callbacks()
        cb = mod.create_truncate_history_callback(keep_recent=14)
        known_text = "ABCDE"  # 5 chars
        contents = [_mk_content("user", [known_text])]
        for _ in range(15):
            contents.append(_mk_content("model", [known_text]))
        req = _mk_request(contents)

        with caplog.at_level(logging.INFO, logger="th2agent.truncate_history"):
            cb(callback_context=_mk_ctx(), llm_request=req)

        data = _extract_debug_data(caplog.records)
        for item in data["per_message"]:
            assert item["text_chars"] == 5, (
                f"text_chars incorrect pour role={item['role']}: "
                f"{item['text_chars']} (attendu 5)"
            )
            assert item["fc_chars"] == 0, f"fc_chars devrait etre 0 : {item}"
            assert item["fr_chars"] == 0, f"fr_chars devrait etre 0 : {item}"


# ---------------------------------------------------------------------------
# (b) NOUVEAU : function_call et function_response mesures correctement
# ---------------------------------------------------------------------------

class TestFunctionCallResponseMeasurement:
    def _build_history_with_fc_fr(self, keep_recent: int = 14) -> list:
        """Construit un historique avec des messages function_call et function_response."""
        msgs = [_mk_content("user", ["payload initial"])]
        # Remplir pour depasser le seuil
        for i in range(keep_recent):
            msgs.append(_mk_content("model", [f"rep {i}"]))
        # Ajouter dans les derniers messages : un fc et un fr
        msgs.append(_mk_content_with_fc("model", "tool_sql", {"query": "SELECT * FROM t"}))
        msgs.append(_mk_content_with_fr("tool", "tool_sql", {"rows": [{"id": 1}] * 50}))
        return msgs

    def test_fc_chars_nonzero_for_function_call_part(self, monkeypatch, caplog):
        """v2 : fc_chars > 0 pour un message contenant function_call."""
        monkeypatch.setenv("TH2_TOKEN_DEBUG", "1")
        mod = _load_callbacks()
        cb = mod.create_truncate_history_callback(keep_recent=14)
        contents = self._build_history_with_fc_fr(keep_recent=14)
        req = _mk_request(contents)

        with caplog.at_level(logging.INFO, logger="th2agent.truncate_history"):
            cb(callback_context=_mk_ctx(), llm_request=req)

        data = _extract_debug_data(caplog.records)
        fc_msgs = [m for m in data["per_message"] if m["fc_chars"] > 0]
        assert len(fc_msgs) >= 1, (
            f"Aucun message avec fc_chars > 0 dans per_message : {data['per_message']}"
        )

    def test_fr_chars_nonzero_for_function_response_part(self, monkeypatch, caplog):
        """v2 : fr_chars > 0 pour un message contenant function_response."""
        monkeypatch.setenv("TH2_TOKEN_DEBUG", "1")
        mod = _load_callbacks()
        cb = mod.create_truncate_history_callback(keep_recent=14)
        contents = self._build_history_with_fc_fr(keep_recent=14)
        req = _mk_request(contents)

        with caplog.at_level(logging.INFO, logger="th2agent.truncate_history"):
            cb(callback_context=_mk_ctx(), llm_request=req)

        data = _extract_debug_data(caplog.records)
        fr_msgs = [m for m in data["per_message"] if m["fr_chars"] > 0]
        assert len(fr_msgs) >= 1, (
            f"Aucun message avec fr_chars > 0 dans per_message : {data['per_message']}"
        )

    def test_biggest_part_points_to_largest_element(self, monkeypatch, caplog):
        """v2 : biggest_part pointe l'element individuel le plus lourd."""
        monkeypatch.setenv("TH2_TOKEN_DEBUG", "1")
        mod = _load_callbacks()
        cb = mod.create_truncate_history_callback(keep_recent=14)

        # Construire un historique avec un gros function_response
        big_response = {"data": "X" * 5000}  # ~5000 chars
        contents = [_mk_content("user", ["payload initial"])]
        for i in range(13):
            contents.append(_mk_content("model", ["rep courte"]))
        contents.append(_mk_content_with_fc("model", "tool_sql", {"q": "SELECT 1"}))
        contents.append(_mk_content_with_fr("tool", "tool_sql", big_response))
        req = _mk_request(contents)

        with caplog.at_level(logging.INFO, logger="th2agent.truncate_history"):
            cb(callback_context=_mk_ctx(), llm_request=req)

        data = _extract_debug_data(caplog.records)
        bp = data["biggest_part"]

        # Le gros function_response doit etre identifie comme le plus lourd
        assert bp["kind"] == "function_response", (
            f"biggest_part.kind attendu 'function_response', obtenu '{bp['kind']}'"
        )
        assert bp["chars"] >= 5000, (
            f"biggest_part.chars attendu >= 5000 (gros fr), obtenu {bp['chars']}"
        )
        assert bp["role"] == "tool", f"biggest_part.role attendu 'tool', obtenu {bp['role']}"
        assert isinstance(bp["msg_index"], int)

    def test_grand_total_chars_includes_fc_and_fr(self, monkeypatch, caplog):
        """v2 : grand_total_chars inclut les chars de fc et fr (pas seulement text)."""
        monkeypatch.setenv("TH2_TOKEN_DEBUG", "1")
        mod = _load_callbacks()
        cb = mod.create_truncate_history_callback(keep_recent=14)

        contents = [_mk_content("user", ["payload"])]
        for _ in range(13):
            contents.append(_mk_content("model", ["x"]))
        # Ajouter un fc et fr avec un contenu connu
        fc_args = {"query": "SELECT * FROM orders"}  # ~38 chars serialises
        fr_resp = {"result": "A" * 200}  # ~216 chars serialises
        contents.append(_mk_content_with_fc("model", "my_tool", fc_args))
        contents.append(_mk_content_with_fr("tool", "my_tool", fr_resp))
        req = _mk_request(contents)

        with caplog.at_level(logging.INFO, logger="th2agent.truncate_history"):
            cb(callback_context=_mk_ctx(), llm_request=req)

        data = _extract_debug_data(caplog.records)
        # grand_total_chars doit etre plus grand que la somme des text seuls
        text_only = sum(m["text_chars"] for m in data["per_message"])
        grand = data["grand_total_chars"]
        assert grand > text_only, (
            f"grand_total_chars ({grand}) devrait etre > text_only ({text_only}) "
            "car il inclut les chars fc/fr"
        )

    def test_fc_chars_matches_expected_serialization(self, monkeypatch, caplog):
        """v2 : fc_chars correspond exactement a la taille de la serialisation JSON."""
        monkeypatch.setenv("TH2_TOKEN_DEBUG", "1")
        mod = _load_callbacks()
        cb = mod.create_truncate_history_callback(keep_recent=14)

        fc_name = "tool_sql"
        fc_args = {"query": "SELECT id FROM orders LIMIT 10"}
        expected_fc_chars = len(
            json.dumps({"name": fc_name, "args": fc_args}, default=str, ensure_ascii=False)
        )

        contents = [_mk_content("user", ["payload"])]
        for _ in range(14):
            contents.append(_mk_content("model", ["rep"]))
        contents.append(_mk_content_with_fc("model", fc_name, fc_args))
        req = _mk_request(contents)

        with caplog.at_level(logging.INFO, logger="th2agent.truncate_history"):
            cb(callback_context=_mk_ctx(), llm_request=req)

        data = _extract_debug_data(caplog.records)
        fc_msgs = [m for m in data["per_message"] if m["fc_chars"] > 0]
        assert fc_msgs, "Aucun message avec fc_chars > 0"
        assert fc_msgs[-1]["fc_chars"] == expected_fc_chars, (
            f"fc_chars={fc_msgs[-1]['fc_chars']} attendu {expected_fc_chars}"
        )

    def test_fr_chars_matches_expected_serialization(self, monkeypatch, caplog):
        """v2 : fr_chars correspond exactement a la taille de la serialisation JSON."""
        monkeypatch.setenv("TH2_TOKEN_DEBUG", "1")
        mod = _load_callbacks()
        cb = mod.create_truncate_history_callback(keep_recent=14)

        fr_response = {"rows": [{"id": i, "name": f"item_{i}"} for i in range(10)]}
        expected_fr_chars = len(
            json.dumps(dict(fr_response), default=str, ensure_ascii=False)
        )

        contents = [_mk_content("user", ["payload"])]
        for _ in range(14):
            contents.append(_mk_content("model", ["rep"]))
        contents.append(_mk_content_with_fr("tool", "tool_sql", fr_response))
        req = _mk_request(contents)

        with caplog.at_level(logging.INFO, logger="th2agent.truncate_history"):
            cb(callback_context=_mk_ctx(), llm_request=req)

        data = _extract_debug_data(caplog.records)
        fr_msgs = [m for m in data["per_message"] if m["fr_chars"] > 0]
        assert fr_msgs, "Aucun message avec fr_chars > 0"
        assert fr_msgs[-1]["fr_chars"] == expected_fr_chars, (
            f"fr_chars={fr_msgs[-1]['fr_chars']} attendu {expected_fr_chars}"
        )


# ---------------------------------------------------------------------------
# (c) Sans le flag : AUCUN log [TOKEN_DEBUG]
# ---------------------------------------------------------------------------

class TestTokenDebugDisabled:
    def test_no_log_when_flag_absent(self, monkeypatch, caplog):
        """Sans TH2_TOKEN_DEBUG, aucun log [TOKEN_DEBUG] n'est emis."""
        monkeypatch.delenv("TH2_TOKEN_DEBUG", raising=False)
        mod = _load_callbacks()
        cb = mod.create_truncate_history_callback(keep_recent=14)
        contents = _build_long_history(keep_recent=14)
        req = _mk_request(contents)

        with caplog.at_level(logging.DEBUG, logger="th2agent.truncate_history"):
            cb(callback_context=_mk_ctx(), llm_request=req)

        token_debug_records = [r for r in caplog.records if "[TOKEN_DEBUG]" in r.message]
        assert len(token_debug_records) == 0, (
            f"Log [TOKEN_DEBUG] emis sans le flag : {token_debug_records}"
        )

    def test_no_log_when_flag_is_zero(self, monkeypatch, caplog):
        """Avec TH2_TOKEN_DEBUG=0, aucun log [TOKEN_DEBUG]."""
        monkeypatch.setenv("TH2_TOKEN_DEBUG", "0")
        mod = _load_callbacks()
        cb = mod.create_truncate_history_callback(keep_recent=14)
        contents = _build_long_history(keep_recent=14)
        req = _mk_request(contents)

        with caplog.at_level(logging.DEBUG, logger="th2agent.truncate_history"):
            cb(callback_context=_mk_ctx(), llm_request=req)

        token_debug_records = [r for r in caplog.records if "[TOKEN_DEBUG]" in r.message]
        assert len(token_debug_records) == 0, (
            f"Log [TOKEN_DEBUG] emis avec TH2_TOKEN_DEBUG=0 : {token_debug_records}"
        )


# ---------------------------------------------------------------------------
# (d) La troncature n'est pas alteree
# ---------------------------------------------------------------------------

class TestTruncationUnaltered:
    def test_truncation_behavior_preserved_with_debug(self, monkeypatch):
        """Avec TH2_TOKEN_DEBUG=1, le nb de messages apres troncature est identique."""
        keep = 14
        contents_ref = _build_long_history(keep_recent=keep)

        # Reference sans flag
        monkeypatch.delenv("TH2_TOKEN_DEBUG", raising=False)
        mod_ref = _load_callbacks()
        cb_ref = mod_ref.create_truncate_history_callback(keep_recent=keep)
        req_ref = _mk_request(list(contents_ref))
        cb_ref(callback_context=_mk_ctx(), llm_request=req_ref)
        n_ref = len(req_ref.contents)

        # Avec flag
        monkeypatch.setenv("TH2_TOKEN_DEBUG", "1")
        mod = _load_callbacks()
        cb = mod.create_truncate_history_callback(keep_recent=keep)
        req = _mk_request(list(contents_ref))
        cb(callback_context=_mk_ctx(), llm_request=req)
        n_after = len(req.contents)

        assert n_after == n_ref, (
            f"Troncature alteree par debug : {n_after} (ref={n_ref})"
        )

    def test_truncation_returns_none(self, monkeypatch):
        """Le callback retourne None."""
        monkeypatch.setenv("TH2_TOKEN_DEBUG", "1")
        mod = _load_callbacks()
        cb = mod.create_truncate_history_callback(keep_recent=14)
        contents = _build_long_history(keep_recent=14)
        req = _mk_request(contents)
        result = cb(callback_context=_mk_ctx(), llm_request=req)
        assert result is None


# ---------------------------------------------------------------------------
# (e) RGPD : aucun contenu texte dans le log
# ---------------------------------------------------------------------------

class TestRgpdNoTextInLog:
    def test_message_text_not_in_log(self, monkeypatch, caplog):
        """RGPD : le texte exact des messages ne doit PAS apparaitre dans le log."""
        monkeypatch.setenv("TH2_TOKEN_DEBUG", "1")
        mod = _load_callbacks()
        cb = mod.create_truncate_history_callback(keep_recent=14)
        secret_text = "SECRET_PAYLOAD_RGPD_12345"
        contents = [_mk_content("user", [secret_text])]
        for _ in range(15):
            contents.append(_mk_content("model", [secret_text]))
        req = _mk_request(contents)

        with caplog.at_level(logging.INFO, logger="th2agent.truncate_history"):
            cb(callback_context=_mk_ctx(), llm_request=req)

        for record in caplog.records:
            if "[TOKEN_DEBUG]" in record.message:
                assert secret_text not in record.message, (
                    f"Texte confidentiel trouve dans le log [TOKEN_DEBUG] : "
                    f"{record.message[:200]}"
                )

    def test_fc_args_not_in_log(self, monkeypatch, caplog):
        """RGPD : les args d'un function_call ne doivent PAS apparaitre dans le log."""
        monkeypatch.setenv("TH2_TOKEN_DEBUG", "1")
        mod = _load_callbacks()
        cb = mod.create_truncate_history_callback(keep_recent=14)
        secret_query = "SELECT secret_column FROM secret_table WHERE id=42"
        contents = [_mk_content("user", ["payload"])]
        for _ in range(14):
            contents.append(_mk_content("model", ["rep"]))
        contents.append(_mk_content_with_fc("model", "tool_sql", {"query": secret_query}))
        req = _mk_request(contents)

        with caplog.at_level(logging.INFO, logger="th2agent.truncate_history"):
            cb(callback_context=_mk_ctx(), llm_request=req)

        for record in caplog.records:
            if "[TOKEN_DEBUG]" in record.message:
                assert secret_query not in record.message, (
                    f"Args function_call trouves dans le log : {record.message[:300]}"
                )

    def test_fr_response_not_in_log(self, monkeypatch, caplog):
        """RGPD : la reponse d'un function_response ne doit PAS apparaitre dans le log."""
        monkeypatch.setenv("TH2_TOKEN_DEBUG", "1")
        mod = _load_callbacks()
        cb = mod.create_truncate_history_callback(keep_recent=14)
        secret_data = "CONFIDENTIEL_NDA_SCEI_2026"
        contents = [_mk_content("user", ["payload"])]
        for _ in range(14):
            contents.append(_mk_content("model", ["rep"]))
        contents.append(_mk_content_with_fr("tool", "tool_sql", {"data": secret_data}))
        req = _mk_request(contents)

        with caplog.at_level(logging.INFO, logger="th2agent.truncate_history"):
            cb(callback_context=_mk_ctx(), llm_request=req)

        for record in caplog.records:
            if "[TOKEN_DEBUG]" in record.message:
                assert secret_data not in record.message, (
                    f"Reponse function_response trouvee dans le log : {record.message[:300]}"
                )

    def test_per_message_has_only_v2_schema_keys(self, monkeypatch, caplog):
        """v2 : per_message ne contient QUE les cles du schema v2 (pas d'autres)."""
        monkeypatch.setenv("TH2_TOKEN_DEBUG", "1")
        mod = _load_callbacks()
        cb = mod.create_truncate_history_callback(keep_recent=14)
        contents = _build_long_history(keep_recent=14)
        req = _mk_request(contents)

        with caplog.at_level(logging.INFO, logger="th2agent.truncate_history"):
            cb(callback_context=_mk_ctx(), llm_request=req)

        data = _extract_debug_data(caplog.records)
        for item in data["per_message"]:
            allowed_keys = {"role", "text_chars", "fc_chars", "fr_chars", "total_chars"}
            extra = set(item.keys()) - allowed_keys
            assert not extra, (
                f"per_message contient des champs non autorises : {extra}"
            )

    def test_system_instruction_content_not_in_log(self, monkeypatch, caplog):
        """RGPD : le contenu du system_instruction ne doit PAS apparaitre dans le log."""
        monkeypatch.setenv("TH2_TOKEN_DEBUG", "1")
        mod = _load_callbacks()
        cb = mod.create_truncate_history_callback(keep_recent=14)
        secret_si = "CONFIDENTIEL_SYSTEM_INSTRUCTION_XYZ"
        contents = _build_long_history(keep_recent=14)
        req = _mk_request(contents, system_instruction=secret_si)

        with caplog.at_level(logging.INFO, logger="th2agent.truncate_history"):
            cb(callback_context=_mk_ctx(), llm_request=req)

        for record in caplog.records:
            if "[TOKEN_DEBUG]" in record.message:
                assert secret_si not in record.message, (
                    f"system_instruction trouve dans le log : {record.message[:300]}"
                )


# ---------------------------------------------------------------------------
# (f) Robustesse : le try/except avale les exceptions du bloc debug
# ---------------------------------------------------------------------------

class TestDebugBlockRobust:
    def test_callback_survives_broken_ctx(self, monkeypatch):
        """ctx sans agent_name -> getattr utilise '?', le callback ne crash pas."""
        monkeypatch.setenv("TH2_TOKEN_DEBUG", "1")
        mod = _load_callbacks()
        cb = mod.create_truncate_history_callback(keep_recent=14)
        contents = _build_long_history(keep_recent=14)
        req = _mk_request(contents)
        ctx = SimpleNamespace()  # Pas d'attribut agent_name

        result = cb(callback_context=ctx, llm_request=req)
        assert result is None

    def test_callback_survives_parts_none(self, monkeypatch):
        """Meme si un message n'a pas de parts, le callback ne crash pas."""
        monkeypatch.setenv("TH2_TOKEN_DEBUG", "1")
        mod = _load_callbacks()
        cb = mod.create_truncate_history_callback(keep_recent=14)
        broken_msg = SimpleNamespace(role="model", parts=None)
        contents = [_mk_content("user", ["payload"])]
        for _ in range(15):
            contents.append(broken_msg)
        req = _mk_request(contents)

        result = cb(callback_context=_mk_ctx(), llm_request=req)
        assert result is None

    def test_callback_survives_no_config(self, monkeypatch):
        """Meme si llm_request.config est None, le callback ne crash pas."""
        monkeypatch.setenv("TH2_TOKEN_DEBUG", "1")
        mod = _load_callbacks()
        cb = mod.create_truncate_history_callback(keep_recent=14)
        contents = _build_long_history(keep_recent=14)
        req = MagicMock()
        req.contents = contents
        req.config = None  # Pas de config

        result = cb(callback_context=_mk_ctx(), llm_request=req)
        assert result is None
