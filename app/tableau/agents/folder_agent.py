from autogen import AssistantAgent
import aiohttp
import asyncio
from urllib.parse import quote
from app.tableau.core.config import Config
from app.tableau.core.logging_utils import log_info, log_error, log_error_to_api

class FolderAgent(AssistantAgent):
    def __init__(self, name):
        super().__init__(name=name, code_execution_config={"use_docker": False})
        self.repo_id_cache = {}

    def _get_headers(self, token=None):
        import base64
        if not token:
            raise ValueError("Azure DevOps token is required")
        encoded_token = base64.b64encode(f":{token}".encode()).decode("ascii")
        return {
            "Authorization": f"Basic {encoded_token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

    async def get_contents(self, path="", repo=None, org=None, project=None, token=None, version=None):
        if not all([repo, org, project, token]):
            log_error("[FolderAgent] repo, org, project, and token are required")
            return None
        path = path.lstrip('/')
        url = (f"https://dev.azure.com/{org}/{project}/_apis/git/repositories/{repo}/items?"
            f"scopePath=/{quote(path)}&recursionLevel=Full&includeContentMetadata=true&api-version={Config.API_VERSION}")
        if version:
            url += f"&versionDescriptor.versionType=branch&versionDescriptor.version={quote(version)}"
        async with aiohttp.ClientSession(headers=self._get_headers(token)) as session:
            try:
                async with session.get(url) as response:
                    if response.status == 203:
                        log_error(f"Auth failed for {path} in repo {repo}")
                        return None
                    if response.status == 404:
                        log_info(f"Folder {path} not found in {repo}")
                        return []
                    response.raise_for_status()
                    items = (await response.json()).get("value", [])
                    return [item for item in items if item['path'].lower() != f'/{path.lower()}']
            except aiohttp.ClientError as e:
                log_error(f"Failed to fetch {path}: {str(e)}")
                return None

    async def get_repository_id(self, repo_name, org=None, project=None, token=None):
        if not all([repo_name, org, project, token]):
            log_error("[FolderAgent] repo_name, org, project, and token are required")
            return None
        if repo_name in self.repo_id_cache:
            return self.repo_id_cache[repo_name]
        url = f"https://dev.azure.com/{org}/{project}/_apis/git/repositories/{repo_name}?api-version={Config.API_VERSION}"
        async with aiohttp.ClientSession(headers=self._get_headers(token)) as session:
            try:
                async with session.get(url) as response:
                    if response.status == 203:
                        log_error(f"Auth failed for repo {repo_name}")
                        return None
                    response.raise_for_status()
                    repo_id = (await response.json())["id"]
                    self.repo_id_cache[repo_name] = repo_id
                    return repo_id
            except aiohttp.ClientError as e:
                log_error(f"Failed to get repo ID: {str(e)}")
                return None

    def validate_app_and_folder(self, repo_id, folder_name):
        if not repo_id or not folder_name:
            return None
        return {"path": f"/{folder_name}", "name": folder_name}