from __future__ import annotations

from core.qwen_vl_client import QwenVLClient


class FakeQwenVLClient(QwenVLClient):
    def __init__(self) -> None:
        super().__init__(model_path="fake-model", device="cpu")
        self.load_count = 0
        self.generate_stages: list[str] = []
        self.stage_outputs: dict[str, list[str]] = {}

    def _load_model(self) -> None:
        self.load_count += 1
        self.model = object()
        self.processor = object()

    def _preprocess_image(self, image_bytes: bytes) -> bytes:
        return image_bytes

    def _generate(self, messages: list, max_new_tokens: int, stage: str = "generate") -> str:
        self.generate_stages.append(stage)
        outputs = self.stage_outputs.get(stage)
        if outputs:
            return outputs.pop(0)
        if stage == "classify":
            return "IMAGE"
        if stage == "table_check":
            return "NOT_TABLE"
        return "<table><tr><td>cached</td></tr></table>"

    def _validate_and_fix_table(self, html_content: str, image_bytes: bytes, language: str) -> str:
        return html_content


def test_describe_image_caches_successful_result_and_skips_model_load_on_hit() -> None:
    client = FakeQwenVLClient()

    first = client.describe_image(b"same-image", is_table=True, language="ko")
    first["text"] = "mutated-by-caller"
    second = client.describe_image(b"same-image", is_table=True, language="ko")

    assert second["text"] == "<table><tr><td>cached</td></tr></table>"
    assert client.generate_stages == ["describe"]
    assert client.load_count == 1


def test_describe_image_uses_positive_type_hint_to_skip_classification() -> None:
    client = FakeQwenVLClient()

    client.describe_image(b"table-image", is_table=True, language="ko")

    assert client.generate_stages == ["describe"]


def test_describe_image_does_not_treat_false_type_hint_as_strong_hint() -> None:
    client = FakeQwenVLClient()
    client.stage_outputs["classify"] = ["MATH"]

    client.describe_image(b"math-image", is_table=False, language="ko")

    assert client.generate_stages == ["classify", "describe"]
