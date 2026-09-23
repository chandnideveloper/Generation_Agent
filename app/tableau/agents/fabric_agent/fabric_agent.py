import aiohttp
import base64
import json
from typing import Dict, List, Optional
from app.tableau.core.logging_utils import log_info, log_error, log_warning

class FabricAgent:
    def __init__(self):
        self.base_url = "https://api.fabric.microsoft.com/v1"

    async def get_items(self, workspace_id: str, token: str) -> List[Dict]:
        url = f"{self.base_url}/workspaces/{workspace_id}/items"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json"
        }
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    try:
                        data = await resp.json()
                    except (json.JSONDecodeError, aiohttp.ClientError, ValueError, TypeError):
                        data = None
                    return (data or {}).get("value", [])
                else:
                    body = await resp.text()
                    log_error(f"[FabricAgent] Failed to fetch items: {resp.status} - {body}")
                    if resp.status == 401:
                        return None
                    return []

    async def get_workspace(self, workspace_id: str, token: str) -> Optional[Dict]:
        """
        Fetches the workspace details (like displayName).
        """
        url = f"{self.base_url}/workspaces/{workspace_id}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json"
        }
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    try:
                        return await resp.json()
                    except (json.JSONDecodeError, aiohttp.ClientError, ValueError, TypeError):
                        return None
                else:
                    body = await resp.text()
                    log_error(f"[FabricAgent] Failed to fetch workspace details: {resp.status} - {body}")
                    return None

    async def create_folder(self, workspace_id: str, display_name: str, token: str) -> Optional[str]:
        """
        Creates a folder in the workspace using the specialized /folders endpoint.
        Returns the folder GUID or "Accepted" if async.
        """
        url = f"{self.base_url}/workspaces/{workspace_id}/folders"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        payload = {
            "displayName": display_name
        }
        
        log_info(f"[FabricAgent] Creating folder '{display_name}' via /folders endpoint...")
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status in [200, 201, 202]:
                    try:
                        data = await resp.json()
                    except (json.JSONDecodeError, aiohttp.ClientError, ValueError, TypeError):
                        data = {}
                    
                    if not isinstance(data, dict): data = {}
                    folder_id = data.get("id")
                    
                    # Fallback: Extract ID from Location header (e.g., .../folders/{guid})
                    if not folder_id and "Location" in resp.headers:
                        location = resp.headers["Location"]
                        folder_id = location.split("/")[-1]
                        log_info(f"[FabricAgent] Extracted Folder ID from Location header: {folder_id}")

                    log_info(f"[FabricAgent] Folder creation successful: {folder_id or 'Accepted'}")
                    return folder_id or "Accepted"
                else:
                    body = await resp.text()
                    log_error(f"[FabricAgent] Folder creation failed: {resp.status} - {body}")
                    raise Exception(f"Folder creation failed (Status: {resp.status}, Response: {body})")

    async def create_item(self, workspace_id: str, display_name: str, item_type: str, definition_parts: List[Dict], token: str, parent_item_id: Optional[str] = None) -> Optional[str]:
        """
        Creates an item in Fabric with a definition.
        """
        url = f"{self.base_url}/workspaces/{workspace_id}/items"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        
        payload = {
            "displayName": display_name,
            "type": item_type,
            "definition": {
                "parts": definition_parts
            }
        }
        
        if parent_item_id:
            payload["folderId"] = parent_item_id
            log_info(f"[FabricAgent] Creating {item_type} '{display_name}' inside folder {parent_item_id}")
            
        log_info(f"[FabricAgent] Creating {item_type} '{display_name}' with {len(definition_parts)} parts...")
        
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.post(url, json=payload) as resp:
                body = await resp.text()
                if resp.status in [201, 202]:
                    try:
                        data = json.loads(body) if body else {}
                    except (json.JSONDecodeError, ValueError, TypeError):
                        data = {}
                    
                    if not isinstance(data, dict): data = {}
                    item_id = data.get("id")
                    log_info(f"[FabricAgent] Successfully created {item_type}: {display_name} | ID: {item_id or 'Accepted'}")
                    return item_id or "Accepted"
                else:
                    log_error(f"[FabricAgent] Failed to create {item_type} '{display_name}' | Status: {resp.status} | Body: {body}")
                    raise Exception(f"Failed to create {item_type} '{display_name}' in Fabric (Status: {resp.status}, Response: {body})")

    async def move_item(self, workspace_id: str, item_id: str, target_folder_id: str, token: str) -> bool:
        """
        Moves an item to a specific folder within the same workspace.
        """
        url = f"{self.base_url}/workspaces/{workspace_id}/items/{item_id}/move"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        payload = {
            "targetFolderId": target_folder_id
        }
        
        log_info(f"[FabricAgent] Moving item {item_id} to folder {target_folder_id}...")
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status in [200, 201, 204]:
                    log_info(f"[FabricAgent] Successfully moved item {item_id} to folder")
                    return True
                else:
                    body = await resp.text()
                    log_error(f"[FabricAgent] Failed to move item {item_id}: {resp.status} - {body}")
                    return False

    async def update_item_definition(self, workspace_id: str, item_id: str, definition_parts: List[Dict], token: str) -> bool:
        url = f"{self.base_url}/workspaces/{workspace_id}/items/{item_id}/updateDefinition"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        
        payload = {
            "definition": {
                "parts": definition_parts
            }
        }
        
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status in [200, 202]:
                    log_info(f"[FabricAgent] Successfully updated definition for item {item_id}")
                    return True
                else:
                    body = await resp.text()
                    log_error(f"[FabricAgent] Failed to update definition for {item_id}: {resp.status} - {body}")
                    return False

    @staticmethod
    def convert_to_definition_parts(file_map: Dict[str, str]) -> List[Dict]:
        """
        Converts a flat dictionary of {path: content} to Fabric API definition parts.
        """
        parts = []
        for path, content in file_map.items():
            # If content is dict/list, convert to JSON string
            if isinstance(content, (dict, list)):
                content = json.dumps(content, indent=2)
            
            # Base64 encode the content
            encoded_payload = base64.b64encode(content.encode('utf-8')).decode('utf-8')
            
            parts.append({
                "path": path,
                "payload": encoded_payload,
                "payloadType": "InlineBase64"
            })
        return parts
