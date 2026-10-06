"""AWS Budgets opcional: e-mail quando o gasto do mês passa de 40% e de 100% do limite."""

from __future__ import annotations

from aws_cdk import aws_budgets as budgets
from constructs import Construct

ALERT_THRESHOLDS_PERCENT = (40, 100)


class CostBudget(Construct):
    """Orçamento mensal de custo. Vale para a conta inteira, não só para esta stack."""

    def __init__(
        self, scope: Construct, construct_id: str, *, email: str, limit_usd: float
    ) -> None:
        super().__init__(scope, construct_id)
        budgets.CfnBudget(
            self,
            "MonthlyBudget",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_type="COST",
                time_unit="MONTHLY",
                budget_limit=budgets.CfnBudget.SpendProperty(amount=limit_usd, unit="USD"),
            ),
            notifications_with_subscribers=[
                _notification(threshold, email) for threshold in ALERT_THRESHOLDS_PERCENT
            ],
        )


def _notification(
    threshold: int, email: str
) -> budgets.CfnBudget.NotificationWithSubscribersProperty:
    return budgets.CfnBudget.NotificationWithSubscribersProperty(
        notification=budgets.CfnBudget.NotificationProperty(
            notification_type="ACTUAL",
            comparison_operator="GREATER_THAN",
            threshold=threshold,
            threshold_type="PERCENTAGE",
        ),
        subscribers=[
            budgets.CfnBudget.SubscriberProperty(subscription_type="EMAIL", address=email)
        ],
    )
