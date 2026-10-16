"""read_uploaded_file : un gros CSV lu par un agent BI rend en-tête, échantillon
et nombre de lignes au lieu de 50 000 caractères bruts ; le reste ne change pas."""
import pytest

from apowerb.core.agent_helpers.read_file_tool import _make_read_uploaded_file


@pytest.fixture
def uploads(tmp_path, monkeypatch):
    from apowerb.configs.settings import get_settings

    settings = get_settings()  # le singleton, que d'autres modules ont importé
    monkeypatch.setattr(settings, "storage_mode", "local")
    monkeypatch.setattr(settings, "uploads_dir", str(tmp_path))
    (tmp_path / "agent1").mkdir()
    return tmp_path / "agent1"


def _big_csv(rows=2000):
    lines = ["order_date,order_amount,note"] + [f"2026-01-{i % 28 + 1:02d},{i}.50,ok" for i in range(rows)]
    lines[5] = '2026-01-05,4.50,"two\nlines"'  # une cellule sur deux lignes reste une seule ligne
    return "\n".join(lines) + "\n"


def test_bi_agent_gets_header_sample_and_row_count(uploads):
    (uploads / "orders.csv").write_text(_big_csv())
    res = _make_read_uploaded_file("agent1", tabular_preview_rows=20)("orders.csv")
    assert res["status"] == "success"
    assert res["columns"] == ["order_date", "order_amount", "note"]
    assert res["row_count"] == 2000
    assert res["content"].splitlines()[0] == "order_date,order_amount,note"
    assert len(res["content"].splitlines()) == 21
    assert res["truncated"] is True and "file_id" in res["note"]


def test_other_agents_still_get_the_raw_content(uploads):
    content = _big_csv()
    (uploads / "orders.csv").write_text(content)
    res = _make_read_uploaded_file("agent1")("orders.csv")
    assert res["content"] == content and res["truncated"] is False


@pytest.mark.parametrize("name,text", [
    ("small.csv", "a,b\n1,2\n"),             # petit fichier : rendu entier
    ("notes.txt", "x" * 10_000),             # pas un tableau
])
def test_small_or_non_tabular_files_are_unchanged(uploads, name, text):
    (uploads / name).write_text(text)
    res = _make_read_uploaded_file("agent1", tabular_preview_rows=20)(name)
    assert res["content"] == text and "row_count" not in res


def test_tsv_uses_tabs(uploads):
    lines = ["date\tqty"] + [f"2026-02-01\t{i}" for i in range(1000)]
    (uploads / "s.tsv").write_text("\n".join(lines))
    res = _make_read_uploaded_file("agent1", tabular_preview_rows=5)("s.tsv")
    assert res["columns"] == ["date", "qty"] and res["row_count"] == 1000


def test_unparsable_csv_falls_back_to_raw_content(uploads):
    text = 'a,b\n1,"' + "x" * 200_000 + '"\n'  # champ au-delà de csv.field_size_limit
    (uploads / "big_field.csv").write_text(text)
    res = _make_read_uploaded_file("agent1", tabular_preview_rows=20)("big_field.csv")
    assert res["status"] == "success" and "row_count" not in res and res["truncated"] is True
