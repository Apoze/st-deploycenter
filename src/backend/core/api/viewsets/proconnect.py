"""Public API route serving the ProConnect api-partenaires allowlist YAML."""

from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page

import rest_framework as drf
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework.permissions import AllowAny
from rest_framework.renderers import BaseRenderer

from core.services.proconnect import (
    build_proconnect_allowlist,
    render_proconnect_allowlist_yaml,
)

# The allowlist is an expensive full-DB query; cache the response briefly.
ALLOWLIST_CACHE_TTL = 60  # seconds


class PlainTextRenderer(BaseRenderer):
    """Render already-serialized text as ``text/plain``."""

    media_type = "text/plain"
    format = "txt"
    charset = "utf-8"

    def render(self, data, accepted_media_type=None, renderer_context=None):
        if isinstance(data, bytes):
            return data
        return str(data).encode(self.charset)


@method_decorator(cache_page(ALLOWLIST_CACHE_TTL), name="dispatch")
class ProConnectAllowlistView(drf.views.APIView):
    """Serve the ``oidc_providers`` allowlist as text/plain YAML (public data).

    One entry per ``type=proconnect`` provider; ``allowed_fqdns`` is the union of
    each in-scope organization's authorized domains (manual + dpnt + candidates + routed),
    with a ``# Source: ... | <Service-Public URL>`` comment per domain. The response
    is cached (Django's cache_page) for a short TTL.
    """

    permission_classes = [AllowAny]
    authentication_classes = []
    renderer_classes = [PlainTextRenderer]

    @extend_schema(
        tags=["proconnect"],
        responses={
            200: OpenApiResponse(
                description="The oidc_providers allowlist as text/plain YAML."
            )
        },
        description=(
            "Return the ProConnect api-partenaires oidc_providers allowlist, "
            "generated from DB data, as text/plain YAML."
        ),
    )
    def get(self, request):
        """GET /api/v1.0/proconnect/oidc_providers.yaml"""
        yaml_text = render_proconnect_allowlist_yaml(build_proconnect_allowlist())
        return drf.response.Response(yaml_text)
