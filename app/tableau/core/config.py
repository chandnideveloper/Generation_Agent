import os
import base64
import logging
from dotenv import load_dotenv
from openai import AzureOpenAI
from pydantic import BaseModel, Field
from typing import Optional
from motor.motor_asyncio import AsyncIOMotorClient

# Load environment variables from .env file
load_dotenv(override=True)

logger = logging.getLogger(__name__)

class EnvValidator(BaseModel):
    ENVIRONMENT: Optional[str] = os.getenv("ENVIRONMENT", "development")
    MONGODB_URL: Optional[str] = os.getenv("MONGODB_URL", "")
    KEYVAULT_API_URL: Optional[str] = os.getenv("KEYVAULT_API_URL", "")
    DEPLOYMENT_SETTINGS_API_URL: Optional[str] = os.getenv("DEPLOYMENT_SETTINGS_API_URL", "")
    AZURE_OPENAI_API_KEY: Optional[str] = os.getenv("AZURE_OPENAI_API_KEY")
    AZURE_OPENAI_ENDPOINT: Optional[str] = os.getenv("AZURE_OPENAI_ENDPOINT")
    AZURE_DEVOPS_PAT: Optional[str] = os.getenv("AZURE_DEVOPS_PAT")
    GIT_PAT: Optional[str] = os.getenv("GIT_PAT")
    GIT_ORG: Optional[str] = os.getenv("GIT_ORG")
    GIT_REPO_NAME: Optional[str] = os.getenv("GIT_REPO_NAME")

    def validate_production(self):
        if self.ENVIRONMENT and self.ENVIRONMENT.lower() == "production":
            missing = []
            if not self.MONGODB_URL: missing.append("MONGODB_URL")
            if not self.KEYVAULT_API_URL: missing.append("KEYVAULT_API_URL")
            if not self.DEPLOYMENT_SETTINGS_API_URL: missing.append("DEPLOYMENT_SETTINGS_API_URL")
            if not self.AZURE_OPENAI_API_KEY: missing.append("AZURE_OPENAI_API_KEY")
            if not self.AZURE_OPENAI_ENDPOINT: missing.append("AZURE_OPENAI_ENDPOINT")
            if missing:
                raise ValueError(f"[Fail-Fast Config] Missing required environment variables for production environment: {', '.join(missing)}")

try:
    _env_val = EnvValidator()
    _env_val.validate_production()
except Exception as e:
    logger.error(f"[Config Error] {e}")
    if os.getenv("ENVIRONMENT", "").lower() == "production" or os.getenv("FAIL_FAST_CONFIG", "").lower() == "true":
        raise

class Config:
    # ==========================================================================
    # Azure DevOps Settings
    # ==========================================================================
    ORGANIZATION = None
    PROJECT = None
    REPO = None
    AZURE_DEVOPS_PAT = os.getenv("AZURE_DEVOPS_PAT")
    ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
    # Destination Base: Set to empty string so we can provide full paths 
    # like "pbip-deploy-tmdl/Healtcare" in the request
    DESTINATION_BASE = ""

    @staticmethod
    def get_branch() -> str:
        """Default branch fallback."""
        return "main"
    
    # ==========================================================================
    # API / Database Settings
    # ==========================================================================
    API_VERSION = "7.0"
    MONGODB_URL = os.getenv("MONGODB_URL", "")
    MONGODB_DB_NAME = "QT2F_Tableau"
    KEYVAULT_API_URL = os.getenv("KEYVAULT_API_URL", "")
    DEPLOYMENT_SETTINGS_API_URL = os.getenv("DEPLOYMENT_SETTINGS_API_URL", "")

    # ==========================================================================
    # Git (GitHub) Settings
    # ==========================================================================
    GIT_PAT = os.getenv("GIT_PAT") or os.getenv("GITHUB_PAT")
    GIT_ORG = os.getenv("GIT_ORG") or os.getenv("GITHUB_ORG")
    GIT_REPO_NAME = os.getenv("GIT_REPO_NAME") or os.getenv("GITHUB_REPO")

    # ==========================================================================
    # AI / LLM Settings
    # ==========================================================================
    AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")
    AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
    AZURE_OPENAI_ASSISTANT_ID = os.getenv("AZURE_OPENAI_ASSISTANT_ID")
    AZURE_OPENAI_DEPLOYMENT_NAME = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4.1") 
    AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
    
    # ==========================================================================
    # Local Application Settings
    # ==========================================================================
    LOCAL_SOURCE_FOLDER = "tableau_source"
    BASE_API_URL = "http://localhost:8080"
    MAX_RETRIES = 3
    RETRY_DELAY = 1

    # ==========================================================================
    # Fabric / TMDL Generation Constants
    # ==========================================================================
    TMSL_CULTURE = "en-US"
    TMSL_COMPATIBILITY_LEVEL = 1601
    TMSL_SOURCE_QUERY_CULTURE = "en-US"
    TMSL_DESKTOP_VERSION = "2.138.1004.0 (24.11)"


# ==============================================================================
# Global Exports (Dependencies for Agents)
# ==============================================================================

# 1. HTTP HEADERS (For Azure DevOps API Authentication)
_pat = Config.AZURE_DEVOPS_PAT
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json"
}
if _pat:
    try:
        _pat_encoded = base64.b64encode(f":{_pat}".encode()).decode("ascii")
        HEADERS["Authorization"] = f"Basic {_pat_encoded}"
    except (UnicodeEncodeError, TypeError) as e:
        logger.warning(f"[Config] Failed to encode AZURE_DEVOPS_PAT: {e}")

# 2. Azure OpenAI Client (For TmdlGenerator)
azure_client = None
if Config.AZURE_OPENAI_API_KEY and Config.AZURE_OPENAI_ENDPOINT:
    try:
        azure_client = AzureOpenAI(
            api_key=Config.AZURE_OPENAI_API_KEY,
            api_version=Config.AZURE_OPENAI_API_VERSION,
            azure_endpoint=Config.AZURE_OPENAI_ENDPOINT
        )
    except (ValueError, TypeError) as e:
        logger.warning(f"[Config] Warning: Could not initialize AzureOpenAI client: {e}")

# 3. MongoDB Client
mongo_client = None
mongo_db = None

if Config.MONGODB_URL:
    if Config.MONGODB_URL.startswith("http"):
        # HTTP Proxy for FastAPI JSON Store
        import httpx
        class HTTPCollection:
            def __init__(self, name, base_url):
                self.name = name
                self.base_url = base_url.rstrip('/')

            async def find_one(self, query, sort=None):
                route = self.name.replace('_', '-')
                if self.name in ["mapping", "mapping_results"]:
                    route = "mapping"
                elif self.name == "data_layer_results":
                    route = "data-layer"
                elif self.name == "activities":
                    route = "activities"
                elif self.name == "logs":
                    route = "logs"
                elif self.name == "generation":
                    route = "generation"
                elif self.name == "settings":
                    route = "settings"
                    
                url = f"{self.base_url}/api/records/{route}"
                params = {}
                for k, v in query.items():
                    if isinstance(v, (str, int, float, bool)):
                        params[k] = v
                        
                import asyncio
                async with httpx.AsyncClient() as client:
                    for attempt in range(4):
                        try:
                            resp = await client.get(url, params=params, timeout=15)
                            if resp.status_code == 200:
                                data = resp.json()
                                if isinstance(data, list) and data:
                                    return data[0]
                                if isinstance(data, dict):
                                    return data
                                return None
                            elif resp.status_code == 404:
                                return None
                            else:
                                logger.warning(f"HTTP find_one for {self.name} returned {resp.status_code} on attempt {attempt+1}")
                        except Exception as e:
                            logger.warning(f"HTTP find_one error for {self.name} on attempt {attempt+1}: {e}")
                        
                        if attempt < 3:
                            await asyncio.sleep(2 ** attempt)
                return None

            async def insert_one(self, document):
                route = self.name.replace('_', '-')
                if self.name in ["mapping", "mapping_results"]:
                    route = "mapping"
                elif self.name == "data_layer_results":
                    route = "data-layer"
                elif self.name == "activities":
                    route = "activities"
                elif self.name == "logs":
                    route = "logs"
                elif self.name == "generation":
                    route = "generation"
                    
                url = f"{self.base_url}/api/records/{route}"
                import asyncio
                async with httpx.AsyncClient() as client:
                    for attempt in range(4):
                        try:
                            resp = await client.post(url, json=document, timeout=15)
                            if resp.status_code in (200, 201):
                                return resp.json()
                            else:
                                logger.warning(f"HTTP insert_one for {self.name} returned {resp.status_code} on attempt {attempt+1}")
                        except Exception as e:
                            logger.warning(f"HTTP insert_one error for {self.name} on attempt {attempt+1}: {e}")
                            
                        if attempt < 3:
                            await asyncio.sleep(2 ** attempt)
                    return None
                        
        class HTTPDatabase:
            def __init__(self, base_url, db_name="db"):
                self.base_url = base_url
                self.name = db_name

            def __getitem__(self, collection_name):
                return HTTPCollection(collection_name, self.base_url)

        class HTTPClient:
            def __init__(self, base_url):
                self.base_url = base_url

            def __getitem__(self, db_name):
                return HTTPDatabase(self.base_url, db_name)
                
        mongo_client = HTTPClient(Config.MONGODB_URL)
        mongo_db = mongo_client[Config.MONGODB_DB_NAME]
        logger.info(f"Initialized HTTP MongoDB Proxy pointing to {Config.MONGODB_URL}")
        
    else:
        try:
            mongo_client = AsyncIOMotorClient(Config.MONGODB_URL)
            mongo_db = mongo_client[Config.MONGODB_DB_NAME]
        except Exception as e:
            logger.error(f"[Config Error] Failed to initialize MongoDB client: {e}")