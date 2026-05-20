from typing import Optional

from core.config import AppConfig, load_config
from core.pipeline import DocumentPipeline
from core.scheduler import AutoProcessor, load_schedule_config


config = load_config()
pipeline = DocumentPipeline(config)
auto_processor = AutoProcessor(config)

_schedule = load_schedule_config(config)
if _schedule.get("enabled"):
    auto_processor.start()


def set_runtime_services(
    *,
    config_override: Optional[AppConfig] = None,
    pipeline_override: Optional[DocumentPipeline] = None,
    auto_processor_override: Optional[AutoProcessor] = None,
) -> None:
    global config, pipeline, auto_processor

    if config_override is not None:
        config = config_override

    if pipeline_override is not None:
        pipeline = pipeline_override
    elif config_override is not None:
        pipeline = DocumentPipeline(config)

    if auto_processor_override is not None:
        auto_processor = auto_processor_override
    elif config_override is not None:
        auto_processor = AutoProcessor(config)
        schedule = load_schedule_config(config)
        if schedule.get("enabled"):
            auto_processor.start()


def get_config() -> AppConfig:
    return config


def get_pipeline() -> DocumentPipeline:
    return pipeline


def get_auto_processor() -> AutoProcessor:
    return auto_processor
