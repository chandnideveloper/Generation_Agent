from typing import Any, Dict, List, Optional
import json
import os
from functools import lru_cache

from app.report import pbir_schemas as S
from app.util.payload import as_dict, as_list, text

ASSET_DIR = os.path.join(os.path.dirname(__file__), "assets")

QLIK_DEFAULT_PALETTE = [
    "#0065B3", "#009845", "#FF9900", "#E60000", "#7B3F00", "#3399CC",
    "#228833", "#EE6677", "#AA3377", "#CCBB44", "#4477AA", "#66CCEE",
]


def build_theme_json(mapping: Optional[Dict[str, Any]] = None) -> str:
    """Dynamically build a rich Power BI theme reflecting Qlik Sense styling and palette."""
    palette: List[str] = []
    bg_color = "#FFFFFF"
    primary_color = "#0065B3"
    font_family = "Segoe UI"

    if mapping and isinstance(mapping, dict):
        # 1. Check top-level themes
        themes_obj = mapping.get("themes")
        if isinstance(themes_obj, list) and themes_obj:
            t = themes_obj[0] if isinstance(themes_obj[0], dict) else {}
            pal = t.get("palette") or t.get("colorPalette") or t.get("color_palette") or t.get("colors")
            if isinstance(pal, list) and pal:
                palette = [str(c) for c in pal if str(c).startswith("#")]
            if t.get("primary_color") or t.get("primaryColor"):
                primary_color = str(t.get("primary_color") or t.get("primaryColor"))
            if t.get("background_color") or t.get("backgroundColor"):
                bg_color = str(t.get("background_color") or t.get("backgroundColor"))
            if t.get("font_family") or t.get("fontFamily"):
                font_family = str(t.get("font_family") or t.get("fontFamily"))

        # 2. Check app_layout / app_metadata
        app_layout = as_dict(mapping.get("app_layout"))
        if app_layout.get("theme"):
            t_data = as_dict(app_layout.get("theme"))
            pal = t_data.get("palette") or t_data.get("colors")
            if isinstance(pal, list) and pal and not palette:
                palette = [str(c) for c in pal if str(c).startswith("#")]

        # 3. Collect any visual-level custom colors
        for vis in as_list(mapping.get("visuals", {}).get("sheet_visuals") if isinstance(mapping.get("visuals"), dict) else mapping.get("visuals")):
            if not isinstance(vis, dict):
                continue
            qs = as_dict(vis.get("qlik_source")) or vis
            col_info = as_dict(qs.get("formatting", {}).get("coloring") or qs.get("custom_coloring"))
            c = col_info.get("single_color") or col_info.get("baseColor") or col_info.get("color")
            if c and str(c).startswith("#") and str(c) not in palette:
                palette.append(str(c))

    if not palette:
        palette = list(QLIK_DEFAULT_PALETTE)

    # Ensure primary color is first in dataColors
    if primary_color and primary_color.startswith("#"):
        if primary_color in palette:
            palette.remove(primary_color)
        palette.insert(0, primary_color)

    theme_data = {
        "name": S.BASE_THEME_NAME,
        "dataColors": palette,
        "foreground": "#252423",
        "foregroundNeutralSecondary": "#605E5C",
        "foregroundNeutralTertiary": "#B3B0AD",
        "background": bg_color,
        "backgroundLight": "#F8F9FA",
        "backgroundNeutral": "#EDEBE9",
        "tableAccent": primary_color,
        "good": "#009845",
        "neutral": "#FF9900",
        "bad": "#E60000",
        "maximum": primary_color,
        "center": "#FF9900",
        "minimum": "#C7E0F4",
        "null": "#FF7F48",
        "hyperlink": primary_color,
        "visitedHyperlink": primary_color,
        "textClasses": {
            "callout": {"fontSize": 32, "fontFace": font_family, "color": primary_color},
            "title": {"fontSize": 12, "fontFace": f"{font_family} Semibold", "color": "#252423"},
            "header": {"fontSize": 11, "fontFace": f"{font_family} Semibold", "color": "#252423"},
            "label": {"fontSize": 10, "fontFace": font_family, "color": "#605E5C"}
        },
        "visualStyles": {
            "*": {
                "*": {
                    "*": [{"wordWrap": True}],
                    "title": [
                        {
                            "show": True,
                            "fontColor": {"solid": {"color": "#252423"}},
                            "fontSize": 12,
                            "fontFamily": f"{font_family} Semibold",
                            "titleWrap": True
                        }
                    ],
                    "background": [
                        {
                            "show": True,
                            "color": {"solid": {"color": "#FFFFFF"}},
                            "transparency": 0
                        }
                    ],
                    "border": [
                        {
                            "show": True,
                            "color": {"solid": {"color": "#E5E5E5"}},
                            "radius": 4
                        }
                    ],
                    "categoryAxis": [
                        {
                            "showAxisTitle": True,
                            "gridlineStyle": "dotted",
                            "concatenateLabels": False
                        }
                    ],
                    "valueAxis": [
                        {
                            "showAxisTitle": True,
                            "gridlineStyle": "dotted"
                        }
                    ],
                    "legend": [
                        {
                            "show": True,
                            "position": "Top"
                        }
                    ]
                }
            },
            "card": {
                "*": {
                    "labels": [
                        {
                            "color": {"solid": {"color": primary_color}},
                            "fontSize": 28,
                            "fontFamily": f"{font_family} Bold"
                        }
                    ],
                    "categoryLabels": [
                        {
                            "show": True,
                            "color": {"solid": {"color": "#605E5C"}},
                            "fontSize": 10
                        }
                    ],
                    "card": [
                        {
                            "outlineColor": {"solid": {"color": "#E5E5E5"}},
                            "outlineWeight": 1
                        }
                    ]
                }
            },
            "slicer": {
                "*": {
                    "header": [
                        {
                            "show": True,
                            "fontColor": {"solid": {"color": "#252423"}},
                            "fontSize": 10,
                            "fontFamily": f"{font_family} Semibold"
                        }
                    ],
                    "items": [
                        {
                            "fontColor": {"solid": {"color": "#252423"}},
                            "fontSize": 10
                        }
                    ]
                }
            },
            "page": {
                "*": {
                    "background": [{"transparency": 0, "color": {"solid": {"color": "#F4F5F7"}}}]
                }
            }
        }
    }
    return json.dumps(theme_data, indent=2)


def base_theme_json() -> str:
    """Fallback without mapping payload."""
    return build_theme_json(None)

