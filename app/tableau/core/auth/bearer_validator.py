import json
import base64
import time
import os
import jwt
from jwt import PyJWKClient, ExpiredSignatureError, InvalidSignatureError, InvalidAudienceError, InvalidIssuerError, InvalidTokenError, PyJWTError
from fastapi import HTTPException, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

security = HTTPBearer()

# Entra ID JWKS endpoint
JWKS_URL = "https://login.microsoftonline.com/common/discovery/v2.0/keys"
_jwks = PyJWKClient(JWKS_URL)

def _decode_jwt_payload(token: str) -> dict:
    """Decodes the payload (middle part) of a JWT without verifying the signature."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError("Token is not a valid JWT (expected 3 parts)")
        
        # JWT payload is base64url-encoded; add padding if needed
        payload_b64 = parts[1]
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        
        payload_bytes = base64.urlsafe_b64decode(payload_b64)
        return json.loads(payload_bytes)
    except (ValueError, TypeError, json.JSONDecodeError, base64.binascii.Error):
        raise ValueError("Failed to decode token payload")

async def validate_auth(auth: HTTPAuthorizationCredentials = Security(security)):
    """
    Bypassed authentication for local testing.
    """
    if auth and auth.credentials:
        return auth.credentials
    return "test-token"