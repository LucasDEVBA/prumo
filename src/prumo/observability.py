"""Logs JSON estruturados com correlation id, e métricas no formato EMF do CloudWatch.

O EMF (Embedded Metric Format) é uma linha de log JSON que o CloudWatch transforma em métrica
sozinho. Numa Lambda, basta imprimir: não há chamada de API nem custo de PutMetricData.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from prumo.domain.models import QualitySnapshot

correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar("correlation_id", default="-")

_STANDARD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "action": record.getMessage(),
            "correlation_id": correlation_id.get(),
        }
        payload.update({k: v for k, v in record.__dict__.items() if k not in _STANDARD_ATTRS})
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())


_STATUS_CODES = {"coletando": -1, "saudavel": 0, "em_observacao": 1, "abaixo_da_meta": 2}


def emf_document(snapshot: QualitySnapshot, namespace: str) -> dict[str, Any]:
    """Monta a linha EMF de uma leitura. `SloBreached` = 1 quando dá para afirmar que piorou."""
    metrics: dict[str, float] = {
        "SloBreached": 1.0 if snapshot.status == "abaixo_da_meta" else 0.0,
        "SloStatusCode": float(_STATUS_CODES.get(snapshot.status, -1)),
        "LabelsUsed": float(snapshot.n_labels),
        "Decisions": float(snapshot.n_decisions),
    }
    optional = {
        "QualityPoint": snapshot.point,
        "QualityLower": snapshot.lower,
        "QualityUpper": snapshot.upper,
        "JudgeApprovalRate": snapshot.judge_rate,
        "BurnRate": snapshot.burn_rate,
    }
    metrics.update({k: v for k, v in optional.items() if v is not None})
    return {
        "_aws": {
            "Timestamp": int(snapshot.at.timestamp() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": namespace,
                    "Dimensions": [["PromptVersion"], []],
                    "Metrics": [
                        {
                            "Name": name,
                            "Unit": "Count" if name in ("LabelsUsed", "Decisions") else "None",
                        }
                        for name in metrics
                    ],
                }
            ],
        },
        "PromptVersion": snapshot.prompt_version,
        **metrics,
    }


class EmfMetricsPublisher:
    """Imprime a métrica em EMF no stdout (na Lambda, vai direto para o CloudWatch)."""

    def __init__(self, namespace: str) -> None:
        self._namespace = namespace

    def publish(self, snapshot: QualitySnapshot) -> None:
        sys.stdout.write(json.dumps(emf_document(snapshot, self._namespace)) + "\n")


class NullMetricsPublisher:
    def publish(self, snapshot: QualitySnapshot) -> None:
        return None
