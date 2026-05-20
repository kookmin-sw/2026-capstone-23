from fastapi import APIRouter

from api.common import ok
from core.model_catalog import get_model_catalog


router = APIRouter(tags=["models"])


@router.get(
    "/models",
    summary="모델 목록 조회",
    description="프론트에서 선택 가능한 모델 카탈로그를 반환합니다. 업로드/배치 화면의 모델 선택 UI를 구성할 때 사용합니다.",
)
def list_models():
    return ok({"models": get_model_catalog()})
