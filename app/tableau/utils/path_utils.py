def indent_block(block: str, spaces: int = 8) -> str:
    """Indents a multi-line string by `spaces`."""
    pad = " " * spaces
    return "\n".join(pad + line if line.strip() else line for line in block.splitlines())

def to_repo_path(path: str) -> str:
    """Normalizes paths for Azure DevOps."""
    return path.replace("\\", "/").lstrip("/")

def normalize_tmdl_filename(fpath: str) -> str:
    """Ensures consistent casing for file paths."""
    return fpath # Simplified for now