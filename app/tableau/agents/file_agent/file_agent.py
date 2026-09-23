from autogen import AssistantAgent
import aiohttp
import json
import uuid
import random
import asyncio
from urllib.parse import quote
from app.tableau.core.config import Config
from app.tableau.core.logging_utils import log_info, log_warning, log_error, log_action_to_api
from app.tableau.utils.path_utils import to_repo_path, normalize_tmdl_filename

class FileAgent(AssistantAgent):
    def __init__(self, name):
        super().__init__(name=name, code_execution_config={"use_docker": False})
        self.commit_id_cache = {}
        self.branch = None

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

    async def get_latest_commit_id(self, repo=None, folder_name=None, org=None, project=None, token=None, branch=None):
        if not all([repo, org, project, token]):
            log_error("[FileAgent] repo, org, project, and token are required")
            return None
        target_branch = branch or getattr(self, 'branch', None) or Config.get_branch() or "main"
        refs_url = (f"https://dev.azure.com/{org}/{project}/_apis/git/repositories/{repo}/refs?"
                    f"filter=heads/{target_branch}&api-version={Config.API_VERSION}") # Assuming 'main' branch
        async with aiohttp.ClientSession(headers=self._get_headers(token)) as session:
            try:
                async with session.get(refs_url) as response:
                    if response.status != 200:
                        log_error(f"Failed to fetch commit ID: {response.status}")
                        return None
                    data = await response.json()
                    if not data["value"]:
                        log_error("No branch found (check if 'main' exists)")
                        return None
                    return data["value"][0]["objectId"]
            except Exception as e:
                log_error(f"Commit fetch failed: {str(e)}")
                return None

    async def create_or_update_file(self, file_path, content, commit_message, repo=None, org=None, project=None, token=None, branch=None):
        if not all([repo, org, project, token]):
            log_error("[FileAgent] repo, org, project, and token are required")
            return False
        file_path = to_repo_path(file_path).lstrip("/")
        log_info(f"Pushing file: {file_path}")
        
        target_branch = branch or getattr(self, 'branch', None) or Config.get_branch() or "main"
        try:
            async with aiohttp.ClientSession(headers=self._get_headers(token)) as session:
                latest_commit = await self.get_latest_commit_id(repo, org=org, project=project, token=token, branch=target_branch)
                if not latest_commit: return False

                push_data = {
                    "refUpdates": [{"name": f"refs/heads/{target_branch}", "oldObjectId": latest_commit}],
                    "commits": [{
                        "comment": commit_message,
                        "changes": [{
                            "changeType": "add", # Simplified for initial migration
                            "item": {"path": f"/{file_path}"},
                            "newContent": {"content": content, "contentType": "rawtext"}
                        }]
                    }]
                }

                url = f"https://dev.azure.com/{org}/{project}/_apis/git/repositories/{repo}/pushes?api-version={Config.API_VERSION}"
                async with session.post(url, json=push_data) as response:
                    if response.status in [200, 201]:
                        log_info(f"Successfully pushed {file_path}")
                        return True
                    
                    resp_text = await response.text()
                    log_warning(f"Push failed with status {response.status} for {file_path}: {resp_text}")
                    
                    # Concurrency check (stale reference)
                    if "GitReferenceStaleException" in resp_text or "TF401028" in resp_text or response.status == 409:
                        log_info(f"Detected stale reference (GitReferenceStaleException) for {file_path}. Retrying push...")
                        # Fetch the updated commit ID
                        latest_commit = await self.get_latest_commit_id(repo, org=org, project=project, token=token, branch=target_branch)
                        if not latest_commit:
                            log_error("Failed to fetch fresh commit ID for retry.")
                            return False
                        
                        push_data["refUpdates"][0]["oldObjectId"] = latest_commit
                        log_info(f"Retrying push for {file_path} with new oldObjectId: {latest_commit}")
                        
                        async with session.post(url, json=push_data) as retry_response:
                            if retry_response.status in [200, 201]:
                                log_info(f"Successfully pushed {file_path} on retry")
                                return True
                            log_error(f"Push failed on retry for {file_path} with status {retry_response.status}: {await retry_response.text()}")
                            return False
                    else:
                        log_error(f"Push failed with status {response.status} for {file_path}: {resp_text}")
                        return False
        except Exception as e:
            import traceback
            log_error(f"Error pushing file {file_path}: {str(e)}\n{traceback.format_exc()}")
            return False

    async def batch_update_files(self, files_to_update, repo=None, org=None, project=None, token=None, branch=None):
        # files_to_update is list of (path, content, change_type)
        if not files_to_update: return True
        if not all([repo, org, project, token]):
            log_error("[FileAgent] repo, org, project, and token are required")
            return False
        
        target_branch = branch or getattr(self, 'branch', None) or Config.get_branch() or "main"
        try:
            async with aiohttp.ClientSession(headers=self._get_headers(token)) as session:
                latest_commit = await self.get_latest_commit_id(repo, org=org, project=project, token=token, branch=target_branch)
                changes = []
                for path, content, ctype in files_to_update:
                    change = {
                        "changeType": ctype,
                        "item": {"path": f"/{to_repo_path(path)}"}
                    }
                    if ctype in ["add", "edit"]:
                        change["newContent"] = {"content": content, "contentType": "rawtext"}
                    changes.append(change)
                
                push_data = {
                    "refUpdates": [{"name": f"refs/heads/{target_branch}", "oldObjectId": latest_commit}],
                    "commits": [{"comment": "Batch update (Tableau Migration)", "changes": changes}]
                }
                
                url = f"https://dev.azure.com/{org}/{project}/_apis/git/repositories/{repo}/pushes?api-version={Config.API_VERSION}"
                async with session.post(url, json=push_data) as res:
                    if res.status in [200, 201]:
                        log_info(f"Batch push successful ({len(changes)} files)")
                        return True
                    
                    resp_text = await res.text()
                    log_warning(f"Batch push failed with status {res.status}: {resp_text}")
                    
                    # Concurrency check (stale reference)
                    if "GitReferenceStaleException" in resp_text or "TF401028" in resp_text or res.status == 409:
                        log_info("Detected stale reference (GitReferenceStaleException) in batch push. Retrying push...")
                        # Fetch the updated commit ID
                        latest_commit = await self.get_latest_commit_id(repo, org=org, project=project, token=token, branch=target_branch)
                        if not latest_commit:
                            log_error("Failed to fetch fresh commit ID for retry.")
                            return False
                        
                        push_data["refUpdates"][0]["oldObjectId"] = latest_commit
                        log_info(f"Retrying batch push with new oldObjectId: {latest_commit}")
                        
                        async with session.post(url, json=push_data) as retry_res:
                            if retry_res.status in [200, 201]:
                                log_info(f"Batch push successful on retry ({len(changes)} files)")
                                return True
                            log_error(f"Batch push failed on retry with status {retry_res.status}: {await retry_res.text()}")
                            return False
                    else:
                        log_error(f"Batch push failed: {resp_text}")
                        return False
        except Exception as e:
            import traceback
            log_error(f"Error during batch push: {str(e)}\n{traceback.format_exc()}")
            return False
