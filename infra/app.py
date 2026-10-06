"""Ponto de entrada do CDK (`cdk synth`/`cdk deploy` executam este arquivo pelo cdk.json)."""

from __future__ import annotations

import os

import aws_cdk as cdk

from prumo_infra.config import StackConfig
from prumo_infra.stack import PrumoStack


def main() -> None:
    app = cdk.App()
    # Conta/região vêm das credenciais do CLI; sem elas o synth gera um template agnóstico.
    env = cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION"),
    )
    PrumoStack(app, "PrumoStack", config=StackConfig.from_context(app.node), env=env)
    app.synth()


if __name__ == "__main__":
    main()
