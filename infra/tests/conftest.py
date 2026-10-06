"""Fixtures da infra: sintetiza a stack com um bundle falso, sem depender do build real."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import aws_cdk as cdk
import pytest
from aws_cdk import assertions

from prumo_infra.config import StackConfig
from prumo_infra.functions import ALL_SPECS
from prumo_infra.stack import PrumoStack

INFRA_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = INFRA_DIR.parent

TemplateFactory = Callable[..., assertions.Template]


def feature_flags() -> dict[str, Any]:
    """Os mesmos feature flags do cdk.json, para o teste sintetizar o que o deploy sintetiza."""
    context: dict[str, Any] = json.loads((INFRA_DIR / "cdk.json").read_text())["context"]
    return context


@pytest.fixture(scope="session")
def fake_asset(tmp_path_factory: pytest.TempPathFactory) -> Path:
    asset = tmp_path_factory.mktemp("lambda_asset")
    (asset / "prumo").mkdir()
    (asset / "prumo" / "__init__.py").write_text("")
    for spec in ALL_SPECS:
        module = asset.joinpath(*spec.handler.rsplit(".", 1)[0].split("."))
        module.parent.mkdir(parents=True, exist_ok=True)
        (module.parent / "__init__.py").touch()
        module.with_suffix(".py").write_text("def handler(event, context):\n    return None\n")
    return asset


@pytest.fixture(scope="session")
def make_template(fake_asset: Path) -> TemplateFactory:
    def factory(**context: Any) -> assertions.Template:
        app = cdk.App(context={**feature_flags(), "lambdaAssetPath": str(fake_asset), **context})
        stack = PrumoStack(app, "PrumoStack", config=StackConfig.from_context(app.node))
        return assertions.Template.from_stack(stack)

    return factory


@pytest.fixture(scope="session")
def template(make_template: TemplateFactory) -> assertions.Template:
    return make_template()
