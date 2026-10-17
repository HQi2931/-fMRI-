"""Durable shared model-call budgets for explicit composite goals."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class CompositeGoalModelBudget(BaseModel):
    """Cumulative model allowance shared by every stage of one intent goal.

    Reserved calls and tokens count as used before a provider is invoked. Actual
    provider usage can exceed a reservation or elapsed-time cap; that overrun is
    retained as evidence and prevents subsequent reservations.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    intent_task_id: str = Field(min_length=1, max_length=100)
    max_steps: int = Field(ge=1, le=1000)
    max_model_calls: int = Field(ge=0, le=1000)
    max_tokens: int = Field(ge=0, le=10_000_000)
    max_elapsed_seconds: int = Field(ge=1, le=31_536_000)
    used_steps: int = Field(default=0, ge=0)
    used_model_calls: int = Field(default=0, ge=0)
    used_tokens: int = Field(default=0, ge=0)
    elapsed_seconds: int = Field(default=0, ge=0)

    @property
    def remaining_steps(self) -> int:
        return max(0, self.max_steps - self.used_steps)

    @property
    def remaining_model_calls(self) -> int:
        return max(0, self.max_model_calls - self.used_model_calls)

    @property
    def remaining_tokens(self) -> int:
        return max(0, self.max_tokens - self.used_tokens)

    @property
    def remaining_elapsed_seconds(self) -> int:
        return max(0, self.max_elapsed_seconds - self.elapsed_seconds)


class CompositeGoalModelCallDecisionStatus(StrEnum):
    RESERVED = "reserved"
    DUPLICATE = "duplicate"
    EXHAUSTED = "exhausted"


class CompositeGoalModelCallState(StrEnum):
    RESERVED = "reserved"
    SETTLED = "settled"
    EXHAUSTED = "exhausted"


class CompositeGoalModelCallReservation(BaseModel):
    """One durable model-call reservation or settled charge."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    intent_task_id: str = Field(min_length=1, max_length=100)
    reservation_id: str = Field(min_length=1, max_length=100)
    call_id: str = Field(min_length=1, max_length=100)
    state: CompositeGoalModelCallState
    estimated_tokens: int = Field(ge=0, le=10_000_000)
    reserved_tokens: int = Field(ge=0, le=10_000_000)
    actual_tokens: int | None = Field(default=None, ge=0, le=10_000_000)
    elapsed_seconds: int = Field(default=0, ge=0, le=31_536_000)


class CompositeGoalModelCallReserveResult(BaseModel):
    """Reservation outcome; only ``reserved`` permits a provider call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: CompositeGoalModelCallDecisionStatus
    reservation: CompositeGoalModelCallReservation
    budget: CompositeGoalModelBudget

    @property
    def reservation_id(self) -> str:
        return self.reservation.reservation_id

    @property
    def reserved_tokens(self) -> int:
        return self.reservation.reserved_tokens


class CompositeGoalModelCallSettlement(BaseModel):
    """The idempotent settlement result and the latest cumulative budget."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reservation: CompositeGoalModelCallReservation
    budget: CompositeGoalModelBudget
