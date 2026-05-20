from core.env import (
    PROJECT_ROOT,
    env_bool,
    env_csv,
    env_first_int,
    env_float,
    env_int,
    env_path,
    env_str,
    project_environ,
)


def test_env_str_reads_environment_and_can_strip(monkeypatch) -> None:
    monkeypatch.setenv("LUMINIR_TEST_ENV_STR", " value ")

    assert env_str("LUMINIR_TEST_ENV_STR") == " value "
    assert env_str("LUMINIR_TEST_ENV_STR", strip=True) == "value"


def test_env_int_falls_back_and_applies_bounds(monkeypatch) -> None:
    monkeypatch.setenv("LUMINIR_TEST_ENV_INT", "not-an-int")
    monkeypatch.setenv("LUMINIR_TEST_ENV_LOW_INT", "-3")

    assert env_int("LUMINIR_TEST_ENV_INT", 7) == 7
    assert env_int("LUMINIR_TEST_ENV_LOW_INT", 7, minimum=1) == 1


def test_env_first_int_uses_first_valid_value(monkeypatch) -> None:
    monkeypatch.setenv("LUMINIR_TEST_ENV_FIRST_BAD", "bad")
    monkeypatch.setenv("LUMINIR_TEST_ENV_FIRST_GOOD", "4")

    assert (
        env_first_int(
            (
                "LUMINIR_TEST_ENV_FIRST_BAD",
                "LUMINIR_TEST_ENV_FIRST_GOOD",
            ),
            1,
            minimum=1,
        )
        == 4
    )


def test_env_bool_float_csv_and_path(monkeypatch) -> None:
    monkeypatch.setenv("LUMINIR_TEST_ENV_BOOL", "yes")
    monkeypatch.setenv("LUMINIR_TEST_ENV_FLOAT", "0.01")
    monkeypatch.setenv("LUMINIR_TEST_ENV_CSV", "jobs, documents, ,worker_leases")
    monkeypatch.setenv("LUMINIR_TEST_ENV_PATH", "data/test-env")

    assert env_bool("LUMINIR_TEST_ENV_BOOL") is True
    assert env_float("LUMINIR_TEST_ENV_FLOAT", 1.0, minimum=0.1) == 0.1
    assert env_csv("LUMINIR_TEST_ENV_CSV") == {"jobs", "documents", "worker_leases"}
    assert env_path("LUMINIR_TEST_ENV_PATH", "unused") == (PROJECT_ROOT / "data/test-env").resolve()


def test_project_environ_returns_loaded_environment(monkeypatch) -> None:
    monkeypatch.setenv("LUMINIR_TEST_PROJECT_ENVIRON", "present")

    assert project_environ()["LUMINIR_TEST_PROJECT_ENVIRON"] == "present"
