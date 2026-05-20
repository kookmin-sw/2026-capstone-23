import importlib
import sys


def test_importing_api_submodule_does_not_create_app() -> None:
    sys.modules.pop("api", None)
    sys.modules.pop("core.jobs.execution", None)

    execution_module = importlib.import_module("core.jobs.execution")

    assert execution_module.OPENAI_BACKEND == "openai"
    assert execution_module.OPENROUTER_BACKEND == "openrouter"
    assert "api" not in sys.modules
