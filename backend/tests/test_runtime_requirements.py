from core.env import PROJECT_ROOT


def _runtime_requirement_names() -> set[str]:
    names: set[str] = set()
    for raw_line in (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        for separator in ("[", "=", "<", ">", "~", "!"):
            if separator in line:
                line = line.split(separator, 1)[0]
        names.add(line.lower().replace("_", "-"))
    return names


def test_runtime_requirements_include_websocket_protocol_dependency() -> None:
    assert {"websockets", "wsproto"} & _runtime_requirement_names()


def test_container_entrypoints_force_websocket_protocol() -> None:
    for relative_path in ("Dockerfile", "docker-compose.yml", "docker-compose.onprem.yml"):
        content = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")

        assert "--ws" in content
        assert "websockets" in content
