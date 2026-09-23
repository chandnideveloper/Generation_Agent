from autogen import AssistantAgent
import aiohttp
import httpx
import re
import datetime
from app.tableau.core.config import Config
from app.tableau.core.logging_utils import log_info, log_error, log_warning, log_action_to_api
from app.tableau.services.action_logger import ActionLogger
from app.tableau.core_logic.metadata_exporter.metadata_exporter import MetadataExporter
import json
from .memory_file_agent import MemoryFileAgent


class FolderManagerMixin:
    async def clean_destination_folder(self, destination_folder: str, repo=Config.REPO, org=Config.ORGANIZATION, project=Config.PROJECT, token=None, branch=None):
        log_info(f"[CoordinatorAgent] Checking/Cleaning destination: {destination_folder} in {org}/{project}/{repo} (branch: {branch})")
        items = await self.folder_agent.get_contents(destination_folder, repo=repo, org=org, project=project, token=token, version=branch)
        if not items:
            return True

        deletes = []
        for item in items:
            if not item.get('isFolder', False):
                deletes.append((item['path'].lstrip('/'), None, "delete"))

        if deletes:
            success = await self.report_generator.file_agent.batch_update_files(deletes, repo=repo, org=org, project=project, token=token, branch=branch)
            return success
        return True

    async def _cleanup_folder(self, destination_folder: str, repo=Config.REPO, org=Config.ORGANIZATION, project=Config.PROJECT, token=None, branch=None):
        """Delete all files in a partially-created folder after an error."""
        log_info(f"[CoordinatorAgent] Cleaning up failed folder: {destination_folder} in {org}/{project}/{repo} (branch: {branch})")
        try:
            items = await self.folder_agent.get_contents(destination_folder, repo=repo, org=org, project=project, token=token, version=branch)
            if not items:
                log_info(f"[CoordinatorAgent] No items found in {destination_folder}, nothing to clean up")
                return True

            deletes = []
            for item in items:
                if not item.get('isFolder', False):
                    deletes.append((item['path'].lstrip('/'), None, "delete"))

            if deletes:
                success = await self.report_generator.file_agent.batch_update_files(deletes, repo=repo, org=org, project=project, token=token, branch=branch)
                if success:
                    log_info(f"[CoordinatorAgent] Successfully cleaned up {len(deletes)} files from {destination_folder}")
                else:
                    log_error(f"[CoordinatorAgent] Failed to clean up files from {destination_folder}")
                return success
            return True
        except Exception as e:
            log_error(f"[CoordinatorAgent] Cleanup failed for {destination_folder}: {e}")
            return False

