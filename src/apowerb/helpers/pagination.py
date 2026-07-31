from typing import Generic, TypeVar

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select


class PageParams(BaseModel):
    """Request query params for paginated API."""

    page: int = Field(default=1, ge=1)
    size: int = Field(default=20, ge=1, le=100)


T = TypeVar("T")


class PageResponse(BaseModel, Generic[T]):
    total: int
    page: int
    size: int
    has_next: bool
    has_previous: bool
    results: list[T]


# def paginate(query: Query, page_params: PageParams, ResponseSchema: BaseModel) -> PageResponse[T]:

#     paginated_query = query.offset((page_params.page - 1) * page_params.size).limit(page_params.size).all()
#     total = query.count()
#     has_next = (page_params.page * page_params.size) < total
#     has_previous = page_params.page > 1

#     return PageResponse(
#         total=total,
#         page=page_params.page,
#         size=page_params.size,
#         has_next=has_next,
#         has_previous=has_previous,
#         results=[ResponseSchema.model_validate(item) for item in paginated_query]
#     )


async def paginate(
    session: AsyncSession,
    stmt: Select,
    page_params: PageParams,
    ResponseSchema: BaseModel,
) -> PageResponse[T]:
    offset_val = (page_params.page - 1) * page_params.size
    paginated_stmt = stmt.offset(offset_val).limit(page_params.size)

    result = await session.execute(paginated_stmt)
    items = result.scalars().all()

    count_stmt = select(func.count()).select_from(stmt.subquery())
    count_result = await session.execute(count_stmt)
    total = count_result.scalar_one()

    has_next = (page_params.page * page_params.size) < total
    has_previous = page_params.page > 1

    return PageResponse(
        total=total,
        page=page_params.page,
        size=page_params.size,
        has_next=has_next,
        has_previous=has_previous,
        results=[ResponseSchema.model_validate(item) for item in items],
    )
