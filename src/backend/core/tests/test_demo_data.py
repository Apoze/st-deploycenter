"""Tests for the local demo-data bootstrap command."""

from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError

import pytest

from core.models import Organization, Service, ServiceSubscription, User


@pytest.mark.django_db
def test_demo_data_requires_drive_service_auth_key(monkeypatch):
    """Fail before creating demo records when service authentication is unset."""
    monkeypatch.delenv("ST_DEPLOYCENTER_SERVICE_AUTH_KEY", raising=False)

    with pytest.raises(CommandError, match="ST_DEPLOYCENTER_SERVICE_AUTH_KEY"):
        call_command("demo_data")

    assert not User.objects.exists()
    assert not Organization.objects.exists()


@pytest.mark.django_db
def test_demo_data_bootstraps_drive_quotas_idempotently(monkeypatch):
    """Repeated bootstraps restore the one demo Drive quota subscription."""
    service_auth_key = "test-drive-service-auth-key"
    monkeypatch.setenv("ST_DEPLOYCENTER_SERVICE_AUTH_KEY", service_auth_key)
    output = StringIO()

    call_command("demo_data", stdout=output)
    organization = Organization.objects.get(siret="00000000000001")
    service = Service.objects.get(type="drive", instance_name="demo")
    subscription = ServiceSubscription.objects.get(
        organization=organization,
        service=service,
    )
    subscription.is_active = False
    subscription.save()
    organization.name = "Stale organization"
    organization.save(update_fields=["name"])
    service.name = "Stale Drive"
    service.config = {"entitlements_api_key": "stale", "preserved": True}
    service.is_active = False
    service.save(update_fields=["name", "config", "is_active"])
    subscription.entitlements.filter(account_type="user").update(
        config={"max_storage": 1}
    )

    call_command("demo_data", stdout=output)

    organization.refresh_from_db()
    service.refresh_from_db()
    subscription.refresh_from_db()
    quotas = {
        entitlement.account_type: entitlement.config["max_storage"]
        for entitlement in subscription.entitlements.order_by("account_type")
    }
    assert service.config["entitlements_api_key"] == service_auth_key
    assert service.config["preserved"] is True
    assert service.name == "Drive"
    assert service.is_active is True
    assert organization.name == "Commune de Demo"
    assert subscription.is_active is True
    assert quotas == {
        "organization": 10_000_000_000,
        "user": 5_000_000_000,
    }
    assert (
        ServiceSubscription.objects.filter(
            organization=organization,
            service=service,
        ).count()
        == 1
    )
    assert Organization.objects.filter(siret="00000000000001").count() == 1
    assert Service.objects.filter(type="drive", instance_name="demo").count() == 1
    assert subscription.entitlements.count() == 2
    assert f"service_id={service.id}" in output.getvalue()
    assert service_auth_key not in output.getvalue()
