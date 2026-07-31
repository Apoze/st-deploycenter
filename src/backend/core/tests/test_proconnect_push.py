"""
Tests for the ProConnect api-partenaires fqdns push (core/proconnect.py).
"""

import hashlib
import hmac
import json
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

import pytest
import requests
import responses
from rest_framework.test import APIClient

from core import factories
from core.models import ServiceSubscription
from core.signals import suppress_proconnect_sync
from core.services.proconnect import (
    ProConnectPartnersClient,
    ProConnectPartnersError,
    _redact_credentials,
    compute_idp_fqdns,
    sign_request,
    subscription_idp_id,
    sync_proconnect_provider,
)

pytestmark = pytest.mark.django_db

BASE_URL = "https://api-partenaires-sandbox.test"
SECRET = "test-oidc-providers-secret"
IDP = "aaa58fc5-0397-495d-8cb5-92b02559d376"

proconnect_settings = override_settings(
    PROCONNECT_API_PARTENAIRES_URL=BASE_URL,
    PROCONNECT_API_PARTENAIRES_SECRET=SECRET,
)


def _expected_signature(method, path, timestamp, body=None):
    """Recompute the signature the same way the api-partenaires middleware does."""
    message = f"{timestamp}:{method}:{path}?"
    if body:
        message += f":{body}"
    return hmac.new(
        SECRET.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _make_proconnect_subscription(idp_id, domains, is_active=True, config_override=None):
    """Create an active ProConnect subscription resolving to ``idp_id``.

    Suppresses the synchronous push so the factory setup doesn't hit the API.
    """
    operator = factories.OperatorFactory()
    organization = factories.OrganizationFactory()
    service = factories.ServiceFactory(type="proconnect", config={"idp_id": idp_id})
    factories.OperatorServiceConfigFactory(
        operator=operator,
        service=service,
        config_override=config_override or {},
    )
    with suppress_proconnect_sync():
        return factories.ServiceSubscriptionFactory(
            organization=organization,
            service=service,
            operator=operator,
            metadata={"domains": domains},
            is_active=is_active,
        )


# --- signature ---------------------------------------------------------------


def test_sign_request_matches_middleware_format():
    """The signed message matches the api-partenaires format (no body)."""
    timestamp, signature = sign_request(
        SECRET, "GET", f"/api/oidc_providers/{IDP}/configuration", "", None
    )
    assert signature == _expected_signature(
        "GET", f"/api/oidc_providers/{IDP}/configuration", timestamp
    )


def test_sign_request_includes_body():
    """When a body is present, it is appended to the signed message."""
    body = '{"fqdns":["a.fr"]}'
    timestamp, signature = sign_request(
        SECRET, "PATCH", f"/api/oidc_providers/{IDP}/configuration", "", body
    )
    assert signature == _expected_signature(
        "PATCH", f"/api/oidc_providers/{IDP}/configuration", timestamp, body
    )


# --- client ------------------------------------------------------------------


@responses.activate
def test_client_set_fqdns_sends_signed_patch():
    """set_fqdns issues a signed PATCH whose body is the exact bytes signed."""
    url = f"{BASE_URL}/api/oidc_providers/{IDP}/configuration"
    responses.add(
        responses.PATCH,
        url,
        json={"uid": IDP, "name": "test", "fqdns": ["a.fr", "b.fr"]},
        status=200,
    )

    client = ProConnectPartnersClient(base_url=BASE_URL, secret=SECRET)
    result = client.set_fqdns(IDP, ["a.fr", "b.fr"])
    assert result["fqdns"] == ["a.fr", "b.fr"]

    request = responses.calls[0].request
    body = request.body
    assert json.loads(body) == {"fqdns": ["a.fr", "b.fr"]}
    # The signature in the header must validate against the exact body sent.
    expected = _expected_signature(
        "PATCH",
        f"/api/oidc_providers/{IDP}/configuration",
        request.headers["X-Timestamp"],
        body,
    )
    assert request.headers["X-Signature"] == expected
    assert request.headers["Content-Type"] == "application/json"


@responses.activate
def test_client_raises_on_error_status():
    """A 4xx/5xx response raises ProConnectPartnersError."""
    url = f"{BASE_URL}/api/oidc_providers/{IDP}/configuration"
    responses.add(responses.PATCH, url, json={"error": "fqdn_not_allowed"}, status=422)

    client = ProConnectPartnersClient(base_url=BASE_URL, secret=SECRET)
    with pytest.raises(ProConnectPartnersError):
        client.set_fqdns(IDP, ["evil.fr"])


@responses.activate
def test_client_error_carries_structured_fqdn_not_allowed():
    """The error exposes api-partenaires' error code + offending fqdns."""
    url = f"{BASE_URL}/api/oidc_providers/{IDP}/configuration"
    responses.add(
        responses.PATCH,
        url,
        json={"error": "fqdn_not_allowed", "fqdns": ["evil.fr", "bad.fr"]},
        status=422,
    )

    client = ProConnectPartnersClient(base_url=BASE_URL, secret=SECRET)
    with pytest.raises(ProConnectPartnersError) as excinfo:
        client.set_fqdns(IDP, ["evil.fr", "bad.fr"])
    assert excinfo.value.status_code == 422
    assert excinfo.value.error_code == "fqdn_not_allowed"
    assert excinfo.value.fqdns == ["evil.fr", "bad.fr"]


def test_client_routes_through_proxy_when_configured():
    """A configured proxy_url is passed to requests for both http and https."""
    client = ProConnectPartnersClient(
        base_url=BASE_URL,
        secret=SECRET,
        proxy_url="socks5://user:pass@proxy:1080",
    )
    fake = mock.Mock(status_code=200, text="{}")
    fake.json.return_value = {"fqdns": []}
    with mock.patch(
        "core.services.proconnect.requests.request", return_value=fake
    ) as request_mock:
        client.get_configuration(IDP)

    _, kwargs = request_mock.call_args
    assert kwargs["proxies"] == {
        "http": "socks5://user:pass@proxy:1080",
        "https": "socks5://user:pass@proxy:1080",
    }


def test_client_no_proxy_by_default():
    """With no proxy configured, requests gets proxies=None (direct)."""
    client = ProConnectPartnersClient(base_url=BASE_URL, secret=SECRET)
    fake = mock.Mock(status_code=200, text="{}")
    fake.json.return_value = {}
    with mock.patch(
        "core.services.proconnect.requests.request", return_value=fake
    ) as request_mock:
        client.get_configuration(IDP)

    _, kwargs = request_mock.call_args
    assert kwargs["proxies"] is None


def test_client_not_configured():
    """An unconfigured client reports it and refuses to call."""
    client = ProConnectPartnersClient(base_url="", secret="")
    assert client.is_configured is False
    with pytest.raises(ProConnectPartnersError):
        client.get_configuration(IDP)


# --- compute_idp_fqdns -------------------------------------------------------


def test_compute_idp_fqdns_unions_across_active_subscriptions():
    """Domains from all active subscriptions for the idp are unioned + normalized."""
    _make_proconnect_subscription(IDP, ["b.fr", "a.fr"])
    _make_proconnect_subscription(IDP, ["A.fr ", "c.fr"])  # dup + case/space
    # A different idp must not leak in.
    _make_proconnect_subscription("other-idp", ["z.fr"])
    # Inactive subscription must be ignored.
    _make_proconnect_subscription(IDP, ["inactive.fr"], is_active=False)

    assert compute_idp_fqdns(IDP) == ["a.fr", "b.fr", "c.fr"]


def test_compute_idp_fqdns_respects_operator_override():
    """idp_id resolution uses the operator's effective config override."""
    subscription = _make_proconnect_subscription(
        "base-idp", ["a.fr"], config_override={"idp_id": "override-idp"}
    )
    assert subscription_idp_id(subscription) == "override-idp"
    assert compute_idp_fqdns("override-idp") == ["a.fr"]
    assert compute_idp_fqdns("base-idp") == []


# --- sync_proconnect_provider ------------------------------------------------


@proconnect_settings
@responses.activate
def test_sync_provider_pushes_full_list():
    """sync PATCHes the full union of active-subscription domains (single call)."""
    _make_proconnect_subscription(IDP, ["a.fr", "b.fr"])
    url = f"{BASE_URL}/api/oidc_providers/{IDP}/configuration"
    responses.add(
        responses.PATCH, url, json={"uid": IDP, "fqdns": ["a.fr", "b.fr"]}, status=200
    )

    result = sync_proconnect_provider(IDP)
    assert result["success"] is True
    assert result["fqdns"] == ["a.fr", "b.fr"]
    assert len(responses.calls) == 1  # no verify GET, just the PATCH
    assert responses.calls[0].request.method == "PATCH"
    assert json.loads(responses.calls[0].request.body) == {"fqdns": ["a.fr", "b.fr"]}


@proconnect_settings
@responses.activate
def test_sync_provider_swallows_api_errors():
    """A failing push is reported but never raises."""
    _make_proconnect_subscription(IDP, ["a.fr"])
    url = f"{BASE_URL}/api/oidc_providers/{IDP}/configuration"
    responses.add(responses.PATCH, url, json={"error": "boom"}, status=500)

    result = sync_proconnect_provider(IDP)
    assert result["success"] is False
    assert "500" in result["error"]


def test_sync_provider_skips_when_not_configured():
    """With no secret/url configured, sync is skipped (no HTTP call)."""
    with override_settings(
        PROCONNECT_API_PARTENAIRES_URL="", PROCONNECT_API_PARTENAIRES_SECRET=""
    ):
        result = sync_proconnect_provider(IDP)
    assert result["skipped"] is True


# --- management command ------------------------------------------------------


@proconnect_settings
@responses.activate
def test_management_command_pushes_all_providers():
    """The backfill command discovers active providers and pushes each."""
    _make_proconnect_subscription(IDP, ["a.fr"])
    url = f"{BASE_URL}/api/oidc_providers/{IDP}/configuration"
    responses.add(responses.PATCH, url, json={"uid": IDP, "fqdns": ["a.fr"]}, status=200)

    out = StringIO()
    call_command("proconnect_sync", stdout=out)
    assert "OK" in out.getvalue()
    assert IDP in out.getvalue()


def test_management_command_dry_run_makes_no_calls():
    """--dry-run prints the fqdns without needing configuration or HTTP."""
    _make_proconnect_subscription(IDP, ["a.fr", "b.fr"])
    out = StringIO()
    with override_settings(
        PROCONNECT_API_PARTENAIRES_URL="", PROCONNECT_API_PARTENAIRES_SECRET=""
    ):
        call_command("proconnect_sync", "--dry-run", stdout=out)
    assert "[dry-run]" in out.getvalue()
    assert "a.fr" in out.getvalue()


# --- rollback on push failure (in-transaction sync) --------------------------


@proconnect_settings
@responses.activate
def test_subscription_activation_rolls_back_on_push_failure():
    """A failed api-partenaires push rolls back the activation and returns 502."""
    url = f"{BASE_URL}/api/oidc_providers/{IDP}/configuration"
    responses.add(responses.PATCH, url, json={"error": "boom"}, status=500)

    user = factories.UserFactory()
    client = APIClient()
    client.force_login(user)
    operator = factories.OperatorFactory()
    factories.UserOperatorRoleFactory(user=user, operator=operator)
    organization = factories.OrganizationFactory(
        rpnt=["1.1", "1.2", "2.1", "2.2", "2.3"],
        adresse_messagerie="contact@commune.fr",
        site_internet="https://www.commune.fr",
    )
    factories.OperatorOrganizationRoleFactory(
        operator=operator, organization=organization
    )
    service = factories.ServiceFactory(type="proconnect", config={"idp_id": IDP})
    factories.OperatorServiceConfigFactory(operator=operator, service=service)

    response = client.patch(
        f"/api/v1.0/operators/{operator.id}/organizations/{organization.id}"
        f"/services/{service.id}/subscription/",
        {"is_active": True},
        format="json",
    )
    assert response.status_code == 502
    # The activation was rolled back — no subscription persisted.
    assert not ServiceSubscription.objects.filter(
        service=service, organization=organization
    ).exists()


# --- signature golden vectors ------------------------------------------------


def test_sign_request_matches_known_vectors():
    """Golden vectors: digests precomputed *independently* (openssl) against the
    api-partenaires signature format — pins byte-for-byte interop, not just parity
    with our own re-implementation."""
    with mock.patch("core.services.proconnect.time.time", return_value=1700000000):
        timestamp, signature = sign_request(
            SECRET, "GET", f"/api/oidc_providers/{IDP}/configuration", "", None
        )
    assert timestamp == "1700000000"
    assert signature == (
        "84b91f377d8cd445cbfb0879395a75f2ff5a6dfc2799784d37968b83711c0817"
    )

    with mock.patch("core.services.proconnect.time.time", return_value=1700000000):
        _, signature = sign_request(
            SECRET,
            "PATCH",
            f"/api/oidc_providers/{IDP}/configuration",
            "",
            '{"fqdns":["a.fr","b.fr"]}',
        )
    assert signature == (
        "b377a6c7b209f9831974a3403e6d7b7495a0065a4b5177418f6d87f3284d7ef1"
    )


# --- proxy credential redaction ----------------------------------------------


def test_redact_credentials_strips_userinfo():
    assert (
        _redact_credentials("socks5://user:s3cr3t@proxy:1080 failed")
        == "socks5://***@proxy:1080 failed"
    )
    # A username-only proxy URL is redacted too (no password present).
    assert (
        _redact_credentials("http://bob@proxy:8080 boom")
        == "http://***@proxy:8080 boom"
    )
    assert _redact_credentials("no credentials here") == "no credentials here"


def test_request_error_does_not_leak_proxy_password():
    """A proxy failure must not surface the proxy password in the raised error."""
    client = ProConnectPartnersClient(
        base_url=BASE_URL,
        secret=SECRET,
        proxy_url="socks5://user:s3cr3t@proxy:1080",
    )
    boom = requests.exceptions.ProxyError(
        "Cannot connect to proxy socks5://user:s3cr3t@proxy:1080"
    )
    with mock.patch("core.services.proconnect.requests.request", side_effect=boom):
        with pytest.raises(ProConnectPartnersError) as excinfo:
            client.get_configuration(IDP)
    assert "s3cr3t" not in str(excinfo.value)
    assert "***" in str(excinfo.value)


# --- management command exit codes -------------------------------------------


@proconnect_settings
@responses.activate
def test_sync_command_raises_on_push_failure():
    """A failed push makes the command exit non-zero (CommandError)."""
    _make_proconnect_subscription(IDP, ["a.fr"])
    url = f"{BASE_URL}/api/oidc_providers/{IDP}/configuration"
    responses.add(responses.PATCH, url, json={"error": "boom"}, status=500)
    with pytest.raises(CommandError):
        call_command("proconnect_sync")


def test_sync_command_raises_when_unconfigured():
    """Running the real push while unconfigured is an error, not a silent no-op."""
    _make_proconnect_subscription(IDP, ["a.fr"])
    with override_settings(
        PROCONNECT_API_PARTENAIRES_URL="", PROCONNECT_API_PARTENAIRES_SECRET=""
    ):
        with pytest.raises(CommandError):
            call_command("proconnect_sync")


# --- proconnect_detect_drift -------------------------------------------------


@proconnect_settings
@responses.activate
def test_detect_drift_passes_when_in_sync():
    """No drift when live provider fqdns exactly match the intended routing."""
    _make_proconnect_subscription(IDP, ["a.fr", "b.fr"])
    url = f"{BASE_URL}/api/oidc_providers/{IDP}/configuration"
    responses.add(
        responses.GET, url, json={"uid": IDP, "fqdns": ["b.fr", "a.fr"]}, status=200
    )

    out = StringIO()
    call_command("proconnect_detect_drift", stdout=out)  # no raise
    assert "in sync" in out.getvalue()


@proconnect_settings
@responses.activate
def test_detect_drift_raises_when_lists_differ():
    """Any mismatch between live and intended fqdns is drift (non-zero exit)."""
    _make_proconnect_subscription(IDP, ["a.fr", "b.fr"])
    url = f"{BASE_URL}/api/oidc_providers/{IDP}/configuration"
    # provider is missing b.fr and has an unexpected extra c.fr.
    responses.add(
        responses.GET, url, json={"uid": IDP, "fqdns": ["a.fr", "c.fr"]}, status=200
    )

    with pytest.raises(CommandError):
        call_command("proconnect_detect_drift")


@proconnect_settings
@responses.activate
def test_detect_drift_raises_on_provider_error():
    _make_proconnect_subscription(IDP, ["a.fr"])
    url = f"{BASE_URL}/api/oidc_providers/{IDP}/configuration"
    responses.add(responses.GET, url, json={"error": "boom"}, status=500)
    with pytest.raises(CommandError):
        call_command("proconnect_detect_drift")


@proconnect_settings
@responses.activate
def test_detect_drift_ignores_duplicate_live_fqdns():
    """A duplicated fqdn in the live config is not drift (compared as a set)."""
    _make_proconnect_subscription(IDP, ["a.fr"])
    url = f"{BASE_URL}/api/oidc_providers/{IDP}/configuration"
    responses.add(
        responses.GET, url, json={"uid": IDP, "fqdns": ["a.fr", "a.fr"]}, status=200
    )
    out = StringIO()
    call_command("proconnect_detect_drift", stdout=out)  # no raise
    assert "in sync" in out.getvalue()


@proconnect_settings
@responses.activate
def test_detect_drift_idp_id_filter_checks_only_that_provider():
    """--idp-id checks only the given provider (other idps are not GET-ed)."""
    _make_proconnect_subscription(IDP, ["a.fr"])
    _make_proconnect_subscription("other-idp", ["z.fr"])
    url = f"{BASE_URL}/api/oidc_providers/{IDP}/configuration"
    responses.add(responses.GET, url, json={"uid": IDP, "fqdns": ["a.fr"]}, status=200)
    out = StringIO()
    call_command("proconnect_detect_drift", "--idp-id", IDP, stdout=out)
    assert "in sync" in out.getvalue()
    assert len(responses.calls) == 1  # only IDP was checked


@proconnect_settings
def test_detect_drift_no_active_providers():
    out = StringIO()
    call_command("proconnect_detect_drift", stdout=out)  # no subs → clean no-op
    assert "No active" in out.getvalue()


# --- in-transaction sync: happy path, change-detection, suppression ----------


def _proconnect_api_setup(is_superuser=False):
    """A logged-in operator user managing an org with a proconnect service."""
    user = factories.UserFactory(is_superuser=is_superuser)
    client = APIClient()
    client.force_login(user)
    operator = factories.OperatorFactory()
    factories.UserOperatorRoleFactory(user=user, operator=operator)
    organization = factories.OrganizationFactory(
        rpnt=["1.1", "1.2", "2.1", "2.2", "2.3"],  # makes mail_domain resolvable
        adresse_messagerie="contact@commune.fr",
        site_internet="https://www.commune.fr",
    )
    factories.OperatorOrganizationRoleFactory(
        operator=operator, organization=organization
    )
    service = factories.ServiceFactory(type="proconnect", config={"idp_id": IDP})
    factories.OperatorServiceConfigFactory(operator=operator, service=service)
    return client, operator, organization, service


def _subscription_url(operator, organization, service):
    return (
        f"/api/v1.0/operators/{operator.id}/organizations/{organization.id}"
        f"/services/{service.id}/subscription/"
    )


@proconnect_settings
@responses.activate
def test_activation_surfaces_fqdn_not_allowed_with_domains():
    """A 'fqdn_not_allowed' push → 400 naming the offending domain, and rolled back."""
    url = f"{BASE_URL}/api/oidc_providers/{IDP}/configuration"
    responses.add(
        responses.PATCH,
        url,
        json={"error": "fqdn_not_allowed", "fqdns": ["commune.fr"]},
        status=422,
    )
    client, operator, organization, service = _proconnect_api_setup()

    response = client.patch(
        _subscription_url(operator, organization, service),
        {"is_active": True},
        format="json",
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "commune.fr" in detail
    assert "ProConnect" in detail
    # The activation was rolled back — no subscription persisted.
    assert not ServiceSubscription.objects.filter(
        service=service, organization=organization
    ).exists()


@proconnect_settings
@responses.activate
def test_subscription_activation_pushes_once_on_success():
    url = f"{BASE_URL}/api/oidc_providers/{IDP}/configuration"
    responses.add(
        responses.PATCH, url, json={"uid": IDP, "fqdns": ["commune.fr"]}, status=200
    )
    client, operator, organization, service = _proconnect_api_setup()

    response = client.patch(
        _subscription_url(operator, organization, service),
        {"is_active": True},
        format="json",
    )
    assert response.status_code == 201
    assert ServiceSubscription.objects.filter(
        service=service, organization=organization
    ).exists()
    assert len(responses.calls) == 1
    assert json.loads(responses.calls[0].request.body) == {"fqdns": ["commune.fr"]}


@proconnect_settings
@responses.activate
def test_push_fires_across_subscription_lifecycle():
    """A push goes to api-partenaires on: (1) activation, (2) adding/removing a
    domain on an active subscription, and (3) deactivation."""
    url = f"{BASE_URL}/api/oidc_providers/{IDP}/configuration"
    responses.add(responses.PATCH, url, json={"uid": IDP, "fqdns": []}, status=200)
    client, operator, organization, service = _proconnect_api_setup(is_superuser=True)
    endpoint = _subscription_url(operator, organization, service)

    def pushed_fqdns():
        return json.loads(responses.calls[-1].request.body)["fqdns"]

    # (1) Activate -> pushes the routed mail domain.
    response = client.patch(endpoint, {"is_active": True}, format="json")
    assert response.status_code == 201
    assert len(responses.calls) == 1
    assert pushed_fqdns() == ["commune.fr"]

    # (2a) Add a domain on the active subscription -> pushes the larger set.
    response = client.patch(
        endpoint, {"metadata": {"domains": ["commune.fr", "extra.fr"]}}, format="json"
    )
    assert response.status_code == 200
    assert len(responses.calls) == 2
    assert pushed_fqdns() == ["commune.fr", "extra.fr"]

    # (2b) Remove a domain -> pushes the smaller set.
    response = client.patch(
        endpoint, {"metadata": {"domains": ["extra.fr"]}}, format="json"
    )
    assert response.status_code == 200
    assert len(responses.calls) == 3
    assert pushed_fqdns() == ["extra.fr"]

    # (2c) A save that changes NO domain does not push again (change-detection).
    response = client.patch(
        endpoint, {"metadata": {"domains": ["extra.fr"]}}, format="json"
    )
    assert response.status_code == 200
    assert len(responses.calls) == 3  # unchanged -> no new push

    # (3) Deactivate -> pushes the now-empty set (subscription no longer contributes).
    response = client.patch(endpoint, {"is_active": False}, format="json")
    assert response.status_code == 200
    assert len(responses.calls) == 4
    assert pushed_fqdns() == []


@proconnect_settings
@responses.activate
def test_subscription_resave_without_domain_change_does_not_repush():
    """A save that changes neither is_active nor the domain set issues no new push."""
    url = f"{BASE_URL}/api/oidc_providers/{IDP}/configuration"
    responses.add(
        responses.PATCH, url, json={"uid": IDP, "fqdns": ["commune.fr"]}, status=200
    )
    client, operator, organization, service = _proconnect_api_setup()
    endpoint = _subscription_url(operator, organization, service)

    client.patch(endpoint, {"is_active": True}, format="json")  # create + push
    assert len(responses.calls) == 1

    # Same domains, still active -> no re-push.
    client.patch(endpoint, {"is_active": True}, format="json")
    assert len(responses.calls) == 1

    # Deactivating removes the contribution -> pushes the now-empty set.
    response = client.patch(endpoint, {"is_active": False}, format="json")
    assert response.status_code == 200
    assert len(responses.calls) == 2
    assert json.loads(responses.calls[1].request.body) == {"fqdns": []}


@proconnect_settings
@responses.activate
def test_invalid_entitlement_type_rejected_before_any_push():
    """A bad entitlement type is a 400 at validation time — no push, no drift."""
    client, operator, organization, service = _proconnect_api_setup()
    response = client.patch(
        _subscription_url(operator, organization, service),
        {
            "is_active": True,
            "entitlements": [
                {"type": "not_a_real_type", "account_type": "commune", "config": {}}
            ],
        },
        format="json",
    )
    assert response.status_code == 400
    assert len(responses.calls) == 0  # the provider was never contacted
    assert not ServiceSubscription.objects.filter(
        service=service, organization=organization
    ).exists()


@proconnect_settings
@responses.activate
def test_suppress_proconnect_sync_prevents_push():
    """Writes inside suppress_proconnect_sync() do not hit the provider."""
    # No responses mock registered: any HTTP call would raise ConnectionError.
    _make_proconnect_subscription(IDP, ["a.fr"])  # created under suppression
    assert len(responses.calls) == 0


@proconnect_settings
@responses.activate
def test_subscription_delete_pushes_reduced_set():
    """Deleting an active proconnect subscription pushes the now-smaller fqdn set."""
    url = f"{BASE_URL}/api/oidc_providers/{IDP}/configuration"
    responses.add(responses.PATCH, url, json={"uid": IDP, "fqdns": []}, status=200)
    client, operator, organization, service = _proconnect_api_setup()
    with suppress_proconnect_sync():
        subscription = factories.ServiceSubscriptionFactory(
            organization=organization,
            service=service,
            operator=operator,
            metadata={"domains": ["commune.fr"]},
            is_active=True,
        )

    response = client.delete(_subscription_url(operator, organization, service))
    assert response.status_code == 204
    assert not ServiceSubscription.objects.filter(pk=subscription.pk).exists()
    assert len(responses.calls) == 1
    assert json.loads(responses.calls[0].request.body) == {"fqdns": []}


@proconnect_settings
@responses.activate
def test_subscription_delete_rolls_back_on_push_failure():
    """A failed push on delete rolls back the deletion (subscription survives)."""
    url = f"{BASE_URL}/api/oidc_providers/{IDP}/configuration"
    responses.add(responses.PATCH, url, json={"error": "boom"}, status=500)
    client, operator, organization, service = _proconnect_api_setup()
    with suppress_proconnect_sync():
        subscription = factories.ServiceSubscriptionFactory(
            organization=organization,
            service=service,
            operator=operator,
            metadata={"domains": ["commune.fr"]},
            is_active=True,
        )

    response = client.delete(_subscription_url(operator, organization, service))
    assert response.status_code == 502
    assert ServiceSubscription.objects.filter(pk=subscription.pk).exists()
