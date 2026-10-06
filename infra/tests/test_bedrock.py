"""Permissões do Bedrock derivadas do id do modelo (o mesmo valor que vai para a env var)."""

from __future__ import annotations

from prumo_infra.bedrock import ArnContext, ModelGrant, guardrail_arn, model_grants

CTX = ArnContext(partition="aws", region="us-east-1", account="123456789012")
NOVA_PROFILE = "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.amazon.nova-lite-v1:0"


def test_should_grant_profile_and_any_region_model_only_through_the_profile() -> None:
    assert model_grants("us.amazon.nova-lite-v1:0", CTX) == [
        ModelGrant((NOVA_PROFILE,)),
        ModelGrant(
            ("arn:aws:bedrock:*::foundation-model/amazon.nova-lite-v1:0",),
            {"StringEquals": {"bedrock:InferenceProfileArn": NOVA_PROFILE}},
        ),
    ]


def test_should_grant_only_regional_model_without_condition_for_plain_foundation_model() -> None:
    assert model_grants("amazon.nova-micro-v1:0", CTX) == [
        ModelGrant(("arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-micro-v1:0",))
    ]


def test_should_treat_global_prefix_as_inference_profile() -> None:
    grants = model_grants("global.anthropic.claude-haiku-4-5-20251001-v1:0", CTX)
    assert grants[1].resource_arns == (
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0",
    )
    assert grants[1].conditions == {
        "StringEquals": {"bedrock:InferenceProfileArn": grants[0].resource_arns[0]}
    }


def test_should_build_guardrail_arn_in_stack_region_and_account() -> None:
    assert guardrail_arn("abc123", CTX) == "arn:aws:bedrock:us-east-1:123456789012:guardrail/abc123"
