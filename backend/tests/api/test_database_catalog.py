from core.model_catalog import find_model_code, get_model_catalog
from db.enums import ModelProvider
from db.models.parser import ParserModel
from db.seed import seed_reference_data
from db.session import SessionLocal, init_db
from infra.store_maps import _SqliteStoreMap


def test_model_catalog_is_seeded_from_sqlalchemy_for_frontend_ids() -> None:
    init_db()

    catalog = get_model_catalog()
    by_id = {model["modelId"]: model for model in catalog}

    assert by_id["m1"]["code"] == "openrouter/openai/gpt-5.2"
    assert by_id["m1"]["provider"] == "openrouter"
    assert by_id["m1"]["defaultExecutionBackend"] == "openrouter"
    assert by_id["m1"]["supportedExecutionBackends"] == ["openrouter"]
    assert by_id["m3"]["code"] == "qwen2.5-vl-7b"
    assert by_id["m3"]["defaultExecutionBackend"] == "qwen_gpu"
    assert by_id["m4"]["provider"] == "openrouter"
    assert by_id["m4"]["supportedExecutionBackends"] == ["openrouter"]
    assert find_model_code("m4", "fallback") == "openrouter/qwen3-vl-8b"

    with SessionLocal() as session:
        gpt_row = session.get(ParserModel, "m1")
        assert gpt_row is not None
        assert gpt_row.model_code == "openrouter/openai/gpt-5.2"
        assert gpt_row.default_execution_backend == "openrouter"

        row = session.get(ParserModel, "m4")
        assert row is not None
        assert row.model_code == "openrouter/qwen3-vl-8b"
        assert row.default_execution_backend == "openrouter"


def test_default_catalog_hides_local_qwen_without_opt_in(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_LOCAL_QWEN_MODEL", "0")

    from core.model_catalog_defaults import default_model_catalog

    codes = {model["code"] for model in default_model_catalog()}

    assert "qwen2.5-vl-7b" not in codes
    assert "openrouter/qwen3-vl-8b" in codes


def test_seed_updates_legacy_gpt_catalog_code_to_openrouter() -> None:
    init_db()

    with SessionLocal() as session:
        row = session.get(ParserModel, "m1")
        assert row is not None
        row.model_code = "gpt-5.2"
        row.display_name = "GPT-5.2"
        row.provider = ModelProvider.OPENAI
        row.default_execution_backend = "openai"
        row.supported_execution_backends_json = ["openai"]
        session.commit()

        seed_reference_data(session)

        refreshed = session.get(ParserModel, "m1")
        assert refreshed is not None
        assert refreshed.model_code == "openrouter/openai/gpt-5.2"
        assert refreshed.provider == ModelProvider.OPENROUTER
        assert refreshed.default_execution_backend == "openrouter"
        assert refreshed.supported_execution_backends_json == ["openrouter"]


def test_sqlalchemy_store_map_keeps_frontend_camel_case_payloads() -> None:
    store = _SqliteStoreMap("test_sqlalchemy_store")
    store.clear()

    store["doc_1"] = {
        "documentId": "doc_1",
        "latestStatus": "UPLOADED",
        "meta": {"pageCount": 1},
    }
    store["doc_1"]["latestStatus"] = "COMPLETED"
    store["doc_1"]["meta"]["pageCount"] = 2

    reloaded = _SqliteStoreMap("test_sqlalchemy_store")
    assert reloaded["doc_1"] == {
        "documentId": "doc_1",
        "latestStatus": "COMPLETED",
        "meta": {"pageCount": 2},
    }

    reloaded.clear()
