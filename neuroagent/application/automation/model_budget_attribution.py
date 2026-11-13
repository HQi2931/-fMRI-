"""Which model-call entries spend the turn allowance, the goal ledger, or both.

The in-memory turn allowance (:class:`SharedWorkTurnBudget`) always bounds one
provider invocation and its repair/fallback inside that invocation. The durable
composite-goal ledger is attached only when the call belongs to an already
admitted goal that this turn is still serving. Rule-only routing does not call
a provider, so it spends neither ledger.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ModelBudgetLedger(StrEnum):
    """One accounting scope for a provider invocation."""

    TURN = "turn"
    GOAL = "goal"


class ModelCallAttribution(BaseModel):
    """The budget contract for one production model-call entry."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entry: str = Field(min_length=1, max_length=80)
    ledgers: tuple[ModelBudgetLedger, ...] = Field(min_length=1, max_length=2)
    charges_goal_before_role_decision: bool = False
    notes: str = Field(min_length=1, max_length=400)


MODEL_CALL_ATTRIBUTION: tuple[ModelCallAttribution, ...] = (
    ModelCallAttribution(
        entry="ordinary_work_without_composite_goal",
        ledgers=(ModelBudgetLedger.TURN,),
        notes="无持久目标的普通 Work 只使用本回合预算，不创建目标账本。",
    ),
    ModelCallAttribution(
        entry="auto_route_while_existing_goal_may_be_left",
        ledgers=(ModelBudgetLedger.TURN,),
        notes=(
            "已有分析目标时，路由调用在选出角色前不接收该目标账本；"
            "切到预处理后本回合生成也不扣除原目标。"
        ),
    ),
    ModelCallAttribution(
        entry="auto_route_admitted_preprocess_then_analyze",
        ledgers=(ModelBudgetLedger.TURN, ModelBudgetLedger.GOAL),
        charges_goal_before_role_decision=True,
        notes="本请求刚接纳的预处理再分析根在路由前就绑定共享目标预算。",
    ),
    ModelCallAttribution(
        entry="foreground_generation_on_bound_analysis_goal",
        ledgers=(ModelBudgetLedger.TURN, ModelBudgetLedger.GOAL),
        notes="路由结果仍是当前分析目标时，前台生成同时计入回合预算和该目标账本。",
    ),
    ModelCallAttribution(
        entry="analysis_proposal_outbox_with_goal",
        ledgers=(ModelBudgetLedger.TURN, ModelBudgetLedger.GOAL),
        notes="有复合根的分析提案 outbox 与前台共享同一目标账本；无根的历史 outbox 只有回合预算。",
    ),
    ModelCallAttribution(
        entry="result_interpretation_outbox_with_goal",
        ledgers=(ModelBudgetLedger.TURN, ModelBudgetLedger.GOAL),
        notes="有复合根的结果解释 outbox 共享同一目标账本；无根历史 outbox 只有回合预算。",
    ),
    ModelCallAttribution(
        entry="json_repair_on_goal_backed_turn",
        ledgers=(ModelBudgetLedger.TURN, ModelBudgetLedger.GOAL),
        notes="目标回合上的 JSON 修复再预留一次同一目标账本；耗尽或重放身份都不再调用模型。",
    ),
)


def attribution_by_entry() -> dict[str, ModelCallAttribution]:
    """Return the attribution table keyed by entry name."""

    return {item.entry: item for item in MODEL_CALL_ATTRIBUTION}
