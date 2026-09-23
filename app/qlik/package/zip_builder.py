"""Zip a generated package (flat {relative_path: content} dict) into bytes.

Every entry in the package dict is a top-level path like
`"<App>.SemanticModel/definition/model.tmdl"` or `"<App>.pbip"` - zipping
them as-is means unzipping the result puts `<App>.pbip` next to
`<App>.SemanticModel/` and `<App>.Report/` at the extraction root, which is
exactly the folder shape Power BI Desktop expects to open directly.
"""

import io
import zipfile
from typing import Dict


def build_zip(package: Dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for relative_path, content in sorted(package.items()):
            data = content if isinstance(content, (str, bytes)) else str(content)
            archive.writestr(relative_path, data)
    return buffer.getvalue()
