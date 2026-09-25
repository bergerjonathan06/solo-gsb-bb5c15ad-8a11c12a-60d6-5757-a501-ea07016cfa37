"""FastAPI 应用入口。"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.schemas import (
    ErrorResponse,
    RescanPlanRequest,
    RescanPlanResponse,
    ReviewRequest,
    ReviewResponse,
    StripDirectionReviewRequest,
    StripDirectionReviewResponse,
)
from app.solver import (
    InvalidRangeError,
    NoFeasibleCombinationError,
    NoFeasibleSequenceError,
    solve_rescan_plan,
    solve_review,
    solve_strip_directions,
)

app = FastAPI(
    title="胶片数字化帧号复核 API",
    description=(
        "对扫描顺序的 2～100 帧 OCR 帧号进行复核：用动态规划指派严格递增的"
        "帧号，舍弃误识别候选并推断未识别帧，输出缺失帧号与代价明细。"
        "并支持 2～4 条胶片条的方向复核：枚举各条正序/倒序组合，"
        "在合法指派中取总代价最小的方案。"
        "还可基于复核结果规划复扫窗口：把缺失帧号与推断帧号合并为复扫目标，"
        "划分成固定成本加长度计费的连续窗口，求最低总成本方案。"
    ),
    version="1.2.0",
)


@app.exception_handler(InvalidRangeError)
async def invalid_range_handler(_request: Request, exc: InvalidRangeError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"error": exc.code, "message": exc.message},
    )


@app.exception_handler(NoFeasibleSequenceError)
async def no_feasible_handler(
    _request: Request, exc: NoFeasibleSequenceError
) -> JSONResponse:
    # 输入本身合法，但约束下不存在合法序列 -> 422 语义校验失败
    return JSONResponse(
        status_code=422,
        content={"error": exc.code, "message": exc.message},
    )


@app.exception_handler(NoFeasibleCombinationError)
async def no_feasible_combination_handler(
    _request: Request, exc: NoFeasibleCombinationError
) -> JSONResponse:
    # 所有胶片条方向组合均不存在合法指派 -> 422 语义校验失败
    return JSONResponse(
        status_code=422,
        content={"error": exc.code, "message": exc.message},
    )


@app.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/api/v1/frame-reviews",
    response_model=ReviewResponse,
    tags=["review"],
    summary="复核扫描帧的帧号指派",
    responses={422: {"model": ErrorResponse, "description": "参数非法或无合法序列"}},
)
async def review_frames(request: ReviewRequest) -> ReviewResponse:
    result = solve_review(request.model_dump())
    return ReviewResponse.model_validate(result)


@app.post(
    "/api/v1/strip-direction-reviews",
    response_model=StripDirectionReviewResponse,
    tags=["review"],
    summary="复核多条胶片条的方向与帧号指派",
    responses={
        422: {
            "model": ErrorResponse,
            "description": "参数非法，或所有方向组合均无合法指派",
        }
    },
)
async def review_strip_directions(
    request: StripDirectionReviewRequest,
) -> StripDirectionReviewResponse:
    result = solve_strip_directions(request.model_dump())
    return StripDirectionReviewResponse.model_validate(result)


@app.post(
    "/api/v1/rescan-plans",
    response_model=RescanPlanResponse,
    tags=["rescan"],
    summary="规划缺失/推断帧的复扫窗口",
    responses={422: {"model": ErrorResponse, "description": "参数非法或无合法序列"}},
)
async def plan_rescan(request: RescanPlanRequest) -> RescanPlanResponse:
    result = solve_rescan_plan(request.model_dump())
    return RescanPlanResponse.model_validate(result)
