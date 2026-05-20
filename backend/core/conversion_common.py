import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional


class ConversionError(Exception):
    pass


DEFAULT_COMMAND_TIMEOUT_SECONDS = 300


def find_tool(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found

    venv_bin = Path(sys.executable).parent / name
    if venv_bin.exists():
        return str(venv_bin)

    project_venv_bin = Path(__file__).resolve().parents[1] / ".venv" / "bin" / name
    if project_venv_bin.exists():
        return str(project_venv_bin)

    cwd_venv_bin = Path.cwd() / ".venv" / "bin" / name
    if cwd_venv_bin.exists():
        return str(cwd_venv_bin)

    return name


def find_libreoffice() -> Optional[str]:
    for cmd in ("libreoffice", "soffice"):
        found = shutil.which(cmd)
        if found:
            return found

    win_paths = [
        Path("C:/Program Files/LibreOffice/program/soffice.exe"),
        Path("C:/Program Files (x86)/LibreOffice/program/soffice.exe"),
    ]
    for path in win_paths:
        if path.exists():
            return str(path)
    return None


def run_command(
    cmd: list[str],
    cwd: Optional[Path] = None,
    timeout: int = DEFAULT_COMMAND_TIMEOUT_SECONDS,
) -> None:
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        executable = cmd[0] if cmd else "command"
        raise ConversionError(
            f"{executable} execution timed out after {timeout} seconds"
        ) from exc
    if proc.returncode != 0:
        raise ConversionError(proc.stderr.decode("utf-8", errors="ignore"))


def resolve_html_resource(uri: str, rel: Optional[str]) -> str:
    if not uri:
        return uri
    if uri.startswith(("http://", "https://", "data:")):
        return uri
    if uri.startswith("file:///"):
        return uri[8:] if uri[7:8] == "/" else uri[7:]
    if uri.startswith("file://"):
        return uri[7:]

    base_path = Path(rel).resolve().parent if rel else Path.cwd()
    return str((base_path / uri).resolve())
