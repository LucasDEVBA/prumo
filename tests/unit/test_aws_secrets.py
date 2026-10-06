import logging

import boto3
import pytest
from botocore.exceptions import EndpointConnectionError
from botocore.stub import Stubber

from prumo.adapters.aws.secrets import read_secret, secrets_client
from prumo.config import Settings
from prumo.domain.errors import ProviderError

REGION = "us-east-1"
ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:prumo-admin-token-AbCdEf"
SECRET_VALUE = "s3gr3d0-que-nunca-aparece-no-log"  # noqa: S105 (valor fictício)
SETTINGS = Settings(aws_region=REGION)


@pytest.fixture
def secrets():
    client = boto3.client("secretsmanager", region_name=REGION)
    stubber = Stubber(client)
    with stubber:
        yield client, stubber
        stubber.assert_no_pending_responses()


def respond(stubber: Stubber, response: dict) -> None:
    stubber.add_response(
        "get_secret_value", {"ARN": ARN, "Name": "prumo-admin-token", **response}, {"SecretId": ARN}
    )


def test_should_read_the_secret_string(secrets):
    client, stubber = secrets
    respond(stubber, {"SecretString": SECRET_VALUE})
    assert read_secret(ARN, SETTINGS, client=client) == SECRET_VALUE


def test_should_strip_whitespace_around_the_secret(secrets):
    client, stubber = secrets
    respond(stubber, {"SecretString": f"  {SECRET_VALUE}\n"})
    assert read_secret(ARN, SETTINGS, client=client) == SECRET_VALUE


@pytest.mark.parametrize(
    "response",
    [{"SecretBinary": b"\x00\x01"}, {"SecretString": "   "}, {}],
    ids=["binario", "em-branco", "sem-valor"],
)
def test_should_raise_provider_error_when_secret_has_no_text(secrets, response):
    client, stubber = secrets
    respond(stubber, response)
    with pytest.raises(ProviderError, match="não tem texto"):
        read_secret(ARN, SETTINGS, client=client)


@pytest.mark.parametrize(
    "code", ["ResourceNotFoundException", "AccessDeniedException", "DecryptionFailure"]
)
def test_should_convert_aws_errors_into_provider_error_without_leaking(secrets, code, caplog):
    client, stubber = secrets
    stubber.add_client_error(
        "get_secret_value", service_error_code=code, service_message=f"detalhe {SECRET_VALUE}"
    )

    with caplog.at_level(logging.DEBUG), pytest.raises(ProviderError) as caught:
        read_secret(ARN, SETTINGS, client=client)

    assert caught.value.details == {
        "service": "secretsmanager",
        "operation": "GetSecretValue",
        "error_code": code,
    }
    assert SECRET_VALUE not in str(caught.value)
    assert SECRET_VALUE not in caplog.text


def test_should_convert_network_failure_into_provider_error():
    class UnreachableClient:
        def get_secret_value(self, **_kwargs):
            raise EndpointConnectionError(endpoint_url="https://secretsmanager")

    with pytest.raises(ProviderError) as caught:
        read_secret(ARN, SETTINGS, client=UnreachableClient())
    assert caught.value.details["error_code"] == "EndpointConnectionError"


def test_should_never_log_the_secret_value(secrets, caplog):
    client, stubber = secrets
    respond(stubber, {"SecretString": SECRET_VALUE})
    with caplog.at_level(logging.DEBUG):
        read_secret(ARN, SETTINGS, client=client)
    assert "secret_loaded" in caplog.messages
    assert SECRET_VALUE not in caplog.text
    assert all(SECRET_VALUE not in str(vars(record)) for record in caplog.records)


def test_should_build_client_with_region_and_common_config():
    client = secrets_client(Settings(aws_region="sa-east-1"))
    assert client.meta.region_name == "sa-east-1"
    assert client.meta.config.retries["mode"] == "adaptive"
    assert client.meta.config.read_timeout == 5
