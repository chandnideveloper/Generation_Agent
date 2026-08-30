"""PBIP packaging — the folder Power BI Desktop opens directly.

    <App>.pbip                      pointer Desktop opens
    <App>.SemanticModel/            TMDL model
    <App>.Report/                   PBIR report
    .gitignore

The same folder is what Fabric's Git integration expects, so one package
serves Desktop, GitHub and Azure DevOps. Only the direct-to-Fabric REST path
needs a different envelope (see fabric_packager).
"""

import json
import os
from typing import Any, Dict, Optional

from app.util.ids import safe_filename
from app.util.logging_utils import get_logger

logger = get_logger(__name__)

GITIGNORE = "\n".join([
    "# Power BI Desktop local state",
    "*.pbix",
    ".pbi/localSettings.json",
    ".pbi/cache.abf",
    "",
])


def pbip_document(app_name: str) -> str:
    """The .pbip file Desktop opens."""
    return json.dumps({
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json",
        "version": "1.0",
        "artifacts": [{"report": {"path": f"{app_name}.Report"}}],
        "settings": {"enableAutoRecovery": True},
    }, indent=2)


def build_package(
    app_name: str,
    model_files: Dict[str, str],
    report_files: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Combine model and report into one PBIP file map (relative paths)."""
    safe = safe_filename(app_name, "QlikApp")
    files: Dict[str, str] = {}

    for path, content in model_files.items():
        files[f"{safe}.SemanticModel/{path}"] = content

    if report_files:
        for path, content in report_files.items():
            files[f"{safe}.Report/{path}"] = content
        files[f"{safe}.pbip"] = pbip_document(safe)

    files[".gitignore"] = GITIGNORE
    return files


import shutil


def write_to_disk(files: Dict[str, str], output_dir: str) -> Dict[str, Any]:
    """Materialize the package. Returns the root path and file count."""
    root = os.path.abspath(output_dir)
    if os.path.exists(root):
        shutil.rmtree(root)
    os.makedirs(root, exist_ok=True)

    written = 0
    for relative_path, content in files.items():
        destination = os.path.join(root, *relative_path.split("/"))
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        with open(destination, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        written += 1

    logger.info("Wrote %s files to %s", written, root)
    return {"output_path": root, "file_count": written}
