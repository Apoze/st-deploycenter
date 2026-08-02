"""Management command to create demo data for local development."""

import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.models import (
    Operator,
    OperatorOrganizationRole,
    OperatorServiceConfig,
    Organization,
    Service,
    ServiceSubscription,
    User,
    UserOperatorRole,
)
from core.services import get_service_handler

SERVICE_TYPES = [
    {
        "type": "proconnect",
        "name": "ProConnect",
        "url": "https://proconnect.example.fr",
        "config": {"idp_id": "demo-idp"},
    },
    {
        "type": "drive",
        "name": "Drive",
        "url": "https://drive.example.fr",
    },
    {
        "type": "messages",
        "name": "Messages",
        "url": "https://messages.example.fr",
    },
    {
        "type": "adc",
        "name": "Administration Centrale",
        "url": "https://adc.example.fr",
    },
    {
        "type": "esd",
        "name": "Espace de Services",
        "url": "https://esd.example.fr",
    },
    {
        "type": "meet",
        "name": "Visio",
        "url": "https://meet.example.fr",
    },
]


class Command(BaseCommand):
    """Create demo data: operator, organization, services, and user role."""

    help = "Insert demo data for local development"

    def handle(self, *args, **options):
        if settings.ENVIRONMENT == "production":
            self.stdout.write(
                self.style.ERROR("This command is not allowed in production!")
            )
            return

        service_auth_key = os.environ.get("ST_DEPLOYCENTER_SERVICE_AUTH_KEY")
        if not service_auth_key:
            raise CommandError("ST_DEPLOYCENTER_SERVICE_AUTH_KEY must be set")

        # User
        user, created = User.objects.get_or_create(
            email="user1@example.local",
            password="user1",  # noqa: S106
            defaults={
                "sub": "user1@example.local",
                "full_name": "Demo User",
            },
        )
        if created:
            self.stdout.write(self.style.SUCCESS(f"Created user: {user.email}"))
        else:
            self.stdout.write(f"User already exists: {user.email}")

        # Operator
        operator, created = Operator.objects.get_or_create(
            name="Demo Operator",
            defaults={
                "url": "https://operator.example.fr",
                "is_active": True,
            },
        )
        if created:
            self.stdout.write(self.style.SUCCESS(f"Created operator: {operator.name}"))
        else:
            self.stdout.write(f"Operator already exists: {operator.name}")

        # User role on operator
        _, created = UserOperatorRole.objects.get_or_create(
            user=user,
            operator=operator,
            defaults={"role": "admin"},
        )
        if created:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Granted admin role to {user.email} on {operator.name}"
                )
            )

        # Organization
        organization, created = Organization.objects.update_or_create(
            siret="00000000000001",
            defaults={
                "name": "Commune de Demo",
                "type": "commune",
                "siren": "000000001",
                "code_insee": "00001",
                "population": 2500,
                "code_postal": "75001",
                "adresse_messagerie": "mairie@demo.example.fr",
            },
        )
        if created:
            self.stdout.write(
                self.style.SUCCESS(f"Created organization: {organization.name}")
            )
        else:
            self.stdout.write(f"Organization already exists: {organization.name}")

        # Operator <-> Organization role
        _, created = OperatorOrganizationRole.objects.get_or_create(
            operator=operator,
            organization=organization,
            defaults={"role": "admin"},
        )
        if created:
            self.stdout.write(
                self.style.SUCCESS(f"Linked {operator.name} to {organization.name}")
            )

        # Services (one per type)
        for svc in SERVICE_TYPES:
            service, created = Service.objects.get_or_create(
                type=svc["type"],
                instance_name="demo",
                defaults={
                    "name": svc["name"],
                    "url": svc["url"],
                    "config": svc.get("config", {}),
                    "is_active": True,
                    "maturity": "stable",
                },
            )
            if service.type == "drive":
                service.name = svc["name"]
                service.url = svc["url"]
                service.config = {
                    **(service.config or {}),
                    "entitlements_api_key": service_auth_key,
                }
                service.is_active = True
                service.maturity = "stable"
                service.save()
            if created:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Created service: {service.name} ({service.type})"
                    )
                )
            else:
                self.stdout.write(
                    f"Service already exists: {service.name} ({service.type})"
                )

            # Link operator to service
            _, created = OperatorServiceConfig.objects.get_or_create(
                operator=operator,
                service=service,
                defaults={"display_priority": 0},
            )
            if created:
                self.stdout.write(
                    self.style.SUCCESS(f"Linked {operator.name} to {service.name}")
                )

            if service.type == "drive":
                subscription, _ = ServiceSubscription.objects.update_or_create(
                    organization=organization,
                    service=service,
                    defaults={"operator": operator, "is_active": True},
                )
                defaults = get_service_handler(service).get_default_entitlements()
                for entitlement in defaults:
                    subscription.entitlements.update_or_create(
                        type=entitlement["type"],
                        account_type=entitlement["account_type"],
                        account=None,
                        defaults={"config": entitlement["config"].copy()},
                    )
                self.stdout.write(
                    self.style.SUCCESS(
                        "Drive quota bootstrap ready: "
                        f"service_id={service.id} "
                        f"organization_siret={organization.siret} "
                        f"entitlements={len(defaults)} subscription=active"
                    )
                )

        self.stdout.write(self.style.SUCCESS("\nDemo data ready!"))
