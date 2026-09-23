"""
GitAgent — Pushes generated files to a GitHub repository using the GitHub REST API.

Uses a single-commit approach via the Git Trees API to push all files in one atomic commit.
"""
import aiohttp
import asyncio
import base64
import random
from app.tableau.core.logging_utils import log_info, log_error, log_warning


class GitRetryableException(Exception):
    """Exception raised for retryable git errors (rate limit, conflict, etc.)."""
    def __init__(self, message: str, response: aiohttp.ClientResponse = None):
        super().__init__(message)
        self.response = response


class GitFatalException(Exception):
    """Exception raised for non-retryable git errors (invalid credential, missing repo, etc.)."""
    pass


class GitAgent:
    """Pushes generated PBIP files to a GitHub repository."""

    GITHUB_API = "https://api.github.com"
    
    # Class-level semaphore to throttle concurrent GitHub pushes.
    # This prevents triggering abuse limits/secondary rate limits on GitHub.
    _push_semaphore = asyncio.Semaphore(3)

    def __init__(self, pat: str = None, org: str = None, repo: str = None):
        self.pat = pat
        self.org = org
        self.repo = repo

    async def _handle_response_error(self, resp: aiohttp.ClientResponse, action: str):
        body = await resp.text()
        status = resp.status
        
        # 429: rate limit, 409: conflict, 422: unprocessable entity (e.g. non-fast-forward / ref update failure), 5xx: server error
        is_retryable = status in (403, 409, 422, 429) or (status >= 500)
        
        # Check if 403 is actually a rate limit / abuse detection or a credential/scope error
        if status == 403:
            body_lower = body.lower()
            if "rate limit" not in body_lower and "abuse" not in body_lower and "secondary" not in body_lower:
                is_retryable = False
                
        err_msg = f"{status} - {body}"
        if is_retryable:
            raise GitRetryableException(err_msg, resp)
        else:
            raise GitFatalException(err_msg)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def batch_push_files(
        self,
        files: dict,
        branch: str = "main",
        commit_message: str = "Tableau to Fabric Migration - Generated PBIP",
    ) -> bool:
        """
        Push all *files* to the GitHub repo in a single commit.

        Parameters
        ----------
        files : dict
            Mapping of ``repo_path -> content`` (strings).
        branch : str
            Target branch (default ``main``).
        commit_message : str
            Commit message for the push.

        Returns
        -------
        bool
            ``True`` on success, ``False`` on failure.
        """
        if not files:
            log_warning("[GitAgent] No files to push.")
            return True

        if not all([self.pat, self.org, self.repo]):
            log_error("[GitAgent] Git PAT, org, or repo is not configured from deployment settings.")
            return False

        headers = {
            "Authorization": f"token {self.pat}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        base = f"{self.GITHUB_API}/repos/{self.org}/{self.repo}"

        max_attempts = 5
        base_delay = 2.0

        for attempt in range(1, max_attempts + 1):
            try:
                # Use class-level semaphore to limit concurrent pushes
                async with self._push_semaphore:
                    async with aiohttp.ClientSession(headers=headers) as session:
                        # 1. Get the SHA of the branch HEAD
                        ref_url = f"{base}/git/ref/heads/{branch}"
                        is_new_branch = False
                        async with session.get(ref_url) as resp:
                            if resp.status == 200:
                                ref_data = await resp.json()
                                head_sha = ref_data["object"]["sha"]
                            elif resp.status == 404:
                                # Branch doesn't exist, try to find default branch to branch off from
                                log_info(f"[GitAgent] Branch '{branch}' not found. Attempting to create it from the default branch.")
                                async with session.get(base) as repo_resp:
                                    if repo_resp.status != 200:
                                        await self._handle_response_error(repo_resp, "get repository info")
                                    repo_data = await repo_resp.json()
                                    default_branch = repo_data.get("default_branch", "main")
                                
                                log_info(f"[GitAgent] Default branch identified as '{default_branch}'. Fetching its HEAD.")
                                default_ref_url = f"{base}/git/ref/heads/{default_branch}"
                                async with session.get(default_ref_url) as d_resp:
                                    if d_resp.status != 200:
                                        await self._handle_response_error(d_resp, f"get default branch '{default_branch}'")
                                    d_ref_data = await d_resp.json()
                                    head_sha = d_ref_data["object"]["sha"]
                                    is_new_branch = True
                            else:
                                await self._handle_response_error(resp, f"get ref for branch '{branch}'")

                        # 2. Get the tree SHA of that commit
                        commit_url = f"{base}/git/commits/{head_sha}"
                        async with session.get(commit_url) as resp:
                            if resp.status != 200:
                                await self._handle_response_error(resp, f"get commit {head_sha}")
                            commit_data = await resp.json()
                            base_tree_sha = commit_data["tree"]["sha"]

                        # 3. Build tree entries
                        tree_items = []
                        for path, content in files.items():
                            clean_path = path.lstrip("/")
                            item = {
                                "path": clean_path,
                                "mode": "100644",
                                "type": "blob"
                            }
                            if isinstance(content, bytes):
                                item["content"] = content.decode("utf-8", errors="replace")
                            else:
                                item["content"] = content
                            tree_items.append(item)

                        # 4. Create new tree
                        tree_url = f"{base}/git/trees"
                        tree_payload = {"base_tree": base_tree_sha, "tree": tree_items}
                        async with session.post(tree_url, json=tree_payload) as resp:
                            if resp.status not in (200, 201):
                                await self._handle_response_error(resp, "create tree")
                            new_tree = await resp.json()
                            new_tree_sha = new_tree["sha"]

                        # 5. Create commit
                        create_commit_url = f"{base}/git/commits"
                        commit_payload = {
                            "message": commit_message,
                            "tree": new_tree_sha,
                            "parents": [head_sha],
                        }
                        async with session.post(create_commit_url, json=commit_payload) as resp:
                            if resp.status not in (200, 201):
                                await self._handle_response_error(resp, "create commit")
                            new_commit = await resp.json()
                            new_commit_sha = new_commit["sha"]
                            self.last_commit_sha = new_commit_sha

                        # 6. Update or Create ref
                        if is_new_branch:
                            log_info(f"[GitAgent] Creating new branch '{branch}' at {new_commit_sha[:8]}")
                            create_ref_url = f"{base}/git/refs"
                            ref_payload = {"ref": f"refs/heads/{branch}", "sha": new_commit_sha}
                            async with session.post(create_ref_url, json=ref_payload) as resp:
                                if resp.status not in (200, 201):
                                    await self._handle_response_error(resp, f"create branch '{branch}'")
                        else:
                            update_ref_url = f"{base}/git/refs/heads/{branch}"
                            ref_payload = {"sha": new_commit_sha, "force": False}
                            async with session.patch(update_ref_url, json=ref_payload) as resp:
                                if resp.status != 200:
                                    await self._handle_response_error(resp, "update ref")

                        log_info(f"[GitAgent] Successfully pushed {len(files)} files to {self.org}/{self.repo}@{branch} (commit: {new_commit_sha[:8]})")
                        return True

            except GitFatalException as e:
                log_error(f"[GitAgent] Fatal error during push (will not retry): {e}")
                return False

            except GitRetryableException as e:
                if attempt == max_attempts:
                    log_error(f"[GitAgent] Push failed after {max_attempts} attempts. Last error: {e}")
                    return False
                
                delay = base_delay ** attempt + random.uniform(0.5, 1.5)
                if e.response:
                    retry_after = e.response.headers.get("retry-after")
                    if retry_after:
                        try:
                            delay = float(retry_after) + random.uniform(0.5, 1.5)
                            log_info(f"[GitAgent] GitHub 'retry-after' header found. Using delay of {delay:.2f}s.")
                        except ValueError:
                            pass
                
                log_warning(f"[GitAgent] Retryable error during push: {e}. Retrying in {delay:.2f}s (Attempt {attempt}/{max_attempts})...")
                await asyncio.sleep(delay)

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt == max_attempts:
                    log_error(f"[GitAgent] Push failed after {max_attempts} attempts due to network error: {e}")
                    return False
                delay = base_delay ** attempt + random.uniform(0.5, 1.5)
                log_warning(f"[GitAgent] Network error during push: {e}. Retrying in {delay:.2f}s (Attempt {attempt}/{max_attempts})...")
                await asyncio.sleep(delay)

            except Exception as e:
                log_error(f"[GitAgent] Push failed with unexpected exception: {e}")
                return False
