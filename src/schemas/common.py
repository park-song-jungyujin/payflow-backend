"""schema-contract.md §1 공통 규약 — 금액 표현."""

from pydantic import BaseModel, ConfigDict, Field


class Money(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount_minor: int
    currency: str = Field(pattern=r"^[A-Z]{3}$")
