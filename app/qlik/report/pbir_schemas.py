"""PBIR / PBIP schema URIs, in one place.

Every version here was taken from a working Power BI project rather than
guessed. The generator previously emitted 1.0.0 for report, page and
visualContainer; Desktop rejects those — the live formats are 3.2.0, 2.0.0 and
2.9.0 respectively, and a report is additionally required to carry a
`version.json` declaring 2.0.0.

Getting a version wrong makes Desktop refuse the whole project, so these are
constants rather than anything computed.
"""

BASE = "https://developer.microsoft.com/json-schemas/fabric"

# --- report item ---------------------------------------------------------
REPORT = f"{BASE}/item/report/definition/report/3.2.0/schema.json"
PAGE = f"{BASE}/item/report/definition/page/2.0.0/schema.json"
PAGES_METADATA = f"{BASE}/item/report/definition/pagesMetadata/1.0.0/schema.json"
VISUAL_CONTAINER = f"{BASE}/item/report/definition/visualContainer/2.9.0/schema.json"
VERSION_METADATA = f"{BASE}/item/report/definition/versionMetadata/1.0.0/schema.json"
BOOKMARK = f"{BASE}/item/report/definition/bookmark/1.0.0/schema.json"
BOOKMARKS_METADATA = f"{BASE}/item/report/definition/bookmarksMetadata/1.0.0/schema.json"
REPORT_DEFINITION_PROPERTIES = f"{BASE}/item/report/definitionProperties/1.0.0/schema.json"
REPORT_LOCAL_SETTINGS = f"{BASE}/item/report/localSettings/1.0.0/schema.json"

# --- semantic model item -------------------------------------------------
MODEL_DEFINITION_PROPERTIES = f"{BASE}/item/semanticModel/definitionProperties/1.0.0/schema.json"
MODEL_LOCAL_SETTINGS = f"{BASE}/item/semanticModel/localSettings/1.1.0/schema.json"
MODEL_EDITOR_SETTINGS = f"{BASE}/item/semanticModel/editorSettings/1.0.0/schema.json"

# --- shared --------------------------------------------------------------
PLATFORM = f"{BASE}/gitIntegration/platformProperties/2.0.0/schema.json"
PBIP = f"{BASE}/item/pbip/definitionProperties/1.0.0/schema.json"

# The report format version a `version.json` must declare.
REPORT_VERSION = "2.0.0"

# Component versions the base theme records as its import baseline.
THEME_VERSIONS = {"visual": "2.4.0", "report": "3.0.0", "page": "2.3.0"}

BASE_THEME_NAME = "CY24SU10"
BASE_THEME_PATH = f"BaseThemes/{BASE_THEME_NAME}.json"

QLIK_THEME_NAME = "QlikAppTheme"
QLIK_THEME_PATH = f"BaseThemes/{QLIK_THEME_NAME}.json"
