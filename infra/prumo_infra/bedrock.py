"""Permissões do Bedrock que cada modelo configurado exige no IAM.

Um id com prefixo geográfico (`us.`, `eu.`...) é um inference profile de inferência entre
regiões: o IAM pede permissão no profile E no foundation model de cada região para onde ele
pode rotear. Por isso o foundation model leva região `*` (continua preso ao modelo exato) e só
vale quando a chamada passa pelo profile, pela condição `bedrock:InferenceProfileArn`. Sem ela, a
Lambda poderia chamar o modelo direto em qualquer região, fora do roteamento que escolhemos.
"""

from __future__ import annotations

from dataclasses import dataclass

CROSS_REGION_PREFIXES = ("us.", "eu.", "apac.", "jp.", "au.", "ca.", "us-gov.", "global.")
INFERENCE_PROFILE_CONDITION_KEY = "bedrock:InferenceProfileArn"

Conditions = dict[str, dict[str, str]]


@dataclass(frozen=True, slots=True)
class ArnContext:
    """Partição, região e conta (tokens do CloudFormation na stack, textos fixos nos testes)."""

    partition: str
    region: str
    account: str


@dataclass(frozen=True, slots=True)
class ModelGrant:
    """Um statement do IAM: ARNs de um modelo e, quando houver, a condição que os prende."""

    resource_arns: tuple[str, ...]
    conditions: Conditions | None = None


def model_grants(model_id: str, ctx: ArnContext) -> list[ModelGrant]:
    """Statements mínimos para chamar `model_id` com InvokeModel/Converse."""
    prefix = next((p for p in CROSS_REGION_PREFIXES if model_id.startswith(p)), None)
    if prefix is None:
        return [
            ModelGrant((f"arn:{ctx.partition}:bedrock:{ctx.region}::foundation-model/{model_id}",))
        ]
    profile_arn = inference_profile_arn(model_id, ctx)
    base_model = model_id.removeprefix(prefix)
    return [
        ModelGrant((profile_arn,)),
        ModelGrant(
            (f"arn:{ctx.partition}:bedrock:*::foundation-model/{base_model}",),
            {"StringEquals": {INFERENCE_PROFILE_CONDITION_KEY: profile_arn}},
        ),
    ]


def inference_profile_arn(model_id: str, ctx: ArnContext) -> str:
    return f"arn:{ctx.partition}:bedrock:{ctx.region}:{ctx.account}:inference-profile/{model_id}"


def guardrail_arn(guardrail_id: str, ctx: ArnContext) -> str:
    return f"arn:{ctx.partition}:bedrock:{ctx.region}:{ctx.account}:guardrail/{guardrail_id}"
