from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from core.env import env_int, env_path, env_str


SUPPORTED_EXTENSIONS = {
    ".pdf",
    ".hwp",
    ".hwpx",
    ".xlsx",
    ".xls",
    ".csv",
    ".png",
    ".jpg",
    ".jpeg",
    ".bmp",
    ".tiff",
    ".gif",
}

EXCLUDE_FILES = {
    ".gitkeep",
    ".gitignore",
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
    ".directory",
}


class AppConfig(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    input_root: Path = Field(default=Path("data/inputs"))
    output_root: Path = Field(default=Path("data/outputs"))
    tmp_root: Path = Field(default=Path("data/tmp"))
    vlm_device: str = Field(default="gpu")
    max_workers: int = Field(default=2)
    vlm_max_concurrent: int = Field(default=8)
    gpu_max_concurrent: int = Field(default=1)
    openai_model: str = Field(default="openrouter/openai/gpt-5.2")


def load_config() -> AppConfig:
    return AppConfig(
        input_root=env_path("INPUT_ROOT", "data/inputs"),
        output_root=env_path("OUTPUT_ROOT", "data/outputs"),
        tmp_root=env_path("TMP_ROOT", "data/tmp"),
        vlm_device=env_str("VLM_DEVICE", "gpu"),
        max_workers=env_int("MAX_WORKERS", 2),
        vlm_max_concurrent=env_int("VLM_MAX_CONCURRENT_API", 8),
        gpu_max_concurrent=env_int("GPU_MAX_CONCURRENT_INFERENCE", 1),
        openai_model=env_str("OPENAI_MODEL", "openrouter/openai/gpt-5.2"),
    )
