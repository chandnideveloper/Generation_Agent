import logging
from azure.identity.aio import DefaultAzureCredential
from app.tableau.core.auth.bearer_validator import _jwks, JWKS_URL, validate_auth, _decode_jwt_payload

logger = logging.getLogger(__name__)

def get_jwks_client():
    """Returns the shared PyJWKClient instance for Entra ID JWKS validation."""
    return _jwks

def get_managed_credential() -> DefaultAzureCredential:
    """
    Returns an async DefaultAzureCredential for least-privilege authentication
    with Azure services (Key Vault, Cosmos DB, etc.).
    """
    logger.info("[SharedAuth] Initializing DefaultAzureCredential for least-privilege access.")
    return DefaultAzureCredential()
