"""Authentifizierung.

Zwei austauschbare Verifier hinter einer gemeinsamen Schnittstelle:

* ``DevTokenVerifier`` – statisches Bearer-Token für die lokale Entwicklung.
  Wird beim Start abgelehnt, wenn ``MUSICHUB_APP_ENV=production``.
* ``OidcVerifier`` – validiert JWT-Access-Tokens eines OIDC-Providers (Authentik)
  über dessen JWKS (Signatur, ``iss``, ``aud``, ``exp``).

Endpunkte hängen nur von ``current_principal`` bzw. ``require_write`` ab und wissen
nicht, welcher Verifier aktiv ist.
"""

from musichub.auth.dependencies import current_principal, get_verifier, require_write
from musichub.auth.principal import Principal

__all__ = ["Principal", "current_principal", "get_verifier", "require_write"]
