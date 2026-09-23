from fastapi import Request, HTTPException

async def validate_request_bearer_token(request: Request):
    """
    Mock Auth: Accepts all requests in local mode.
    In production, validation logic against Azure AD would go here.
    """
    # For local testing, just return True
    return True
    
    # Example of real logic:
    # auth_header = request.headers.get("Authorization")
    # if not auth_header or not auth_header.startswith("Bearer "):
    #     raise HTTPException(status_code=401, detail="Missing Token")