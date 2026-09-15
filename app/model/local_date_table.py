"""
Auto-generate hidden LocalDateTable TMDL files for datetime columns.
Ported and adapted from vl-q2f-report-generation/app/agents/tmdl_generator.py.

Replicates Power BI Desktop's auto date/time calendar tables, providing
Year, Quarter, Month, and Day drill-down hierarchies for every datetime column.
"""
import uuid
from typing import Any, Dict, List, Tuple
from app.util.payload import as_list, as_dict, text


_LOCAL_DATE_TABLE_TEMPLATE = """\
table LocalDateTable_{guid}
\tisHidden
\tlineageTag: {table_lineage_tag}

\tcolumn Date
\t\tdataType: dateTime
\t\tisHidden
\t\tformatString: General Date
\t\tlineageTag: {date_lineage_tag}
\t\tdataCategory: PaddedDateTableDates
\t\tsummarizeBy: none
\t\tisNameInferred
\t\tsourceColumn: [Date]
\t\tannotation SummarizationSetBy = User

\tcolumn Year = YEAR([Date])
\t\tdataType: int64
\t\tisHidden
\t\tformatString: 0
\t\tlineageTag: {year_lineage_tag}
\t\tdataCategory: Years
\t\tsummarizeBy: none
\t\tannotation SummarizationSetBy = User
\t\tannotation TemplateId = Year

\tcolumn MonthNo = MONTH([Date])
\t\tdataType: int64
\t\tisHidden
\t\tformatString: 0
\t\tlineageTag: {monthno_lineage_tag}
\t\tdataCategory: MonthOfYear
\t\tsummarizeBy: none
\t\tannotation SummarizationSetBy = User
\t\tannotation TemplateId = MonthNumber

\tcolumn Month = FORMAT([Date], "MMMM")
\t\tdataType: string
\t\tisHidden
\t\tlineageTag: {month_lineage_tag}
\t\tdataCategory: Months
\t\tsummarizeBy: none
\t\tsortByColumn: MonthNo
\t\tannotation SummarizationSetBy = User
\t\tannotation TemplateId = Month

\tcolumn QuarterNo = INT(([MonthNo] + 2) / 3)
\t\tdataType: int64
\t\tisHidden
\t\tformatString: 0
\t\tlineageTag: {quarterno_lineage_tag}
\t\tdataCategory: QuarterOfYear
\t\tsummarizeBy: none
\t\tannotation SummarizationSetBy = User
\t\tannotation TemplateId = QuarterNumber

\tcolumn Quarter = "Qtr " & [QuarterNo]
\t\tdataType: string
\t\tisHidden
\t\tlineageTag: {quarter_lineage_tag}
\t\tdataCategory: Quarters
\t\tsummarizeBy: none
\t\tsortByColumn: QuarterNo
\t\tannotation SummarizationSetBy = User
\t\tannotation TemplateId = Quarter

\tcolumn Day = DAY([Date])
\t\tdataType: int64
\t\tisHidden
\t\tformatString: 0
\t\tlineageTag: {day_lineage_tag}
\t\tdataCategory: DayOfMonth
\t\tsummarizeBy: none
\t\tannotation SummarizationSetBy = User
\t\tannotation TemplateId = Day

\thierarchy 'Date Hierarchy'
\t\tlineageTag: {hierarchy_lineage_tag}
\t\tlevel Year
\t\t\tlineageTag: {year_level_lineage_tag}
\t\t\tcolumn: Year
\t\tlevel Quarter
\t\t\tlineageTag: {quarter_level_lineage_tag}
\t\t\tcolumn: Quarter
\t\tlevel Month
\t\t\tlineageTag: {month_level_lineage_tag}
\t\t\tcolumn: Month
\t\tlevel Day
\t\t\tlineageTag: {day_level_lineage_tag}
\t\t\tcolumn: Day
\t\tannotation TemplateId = DateHierarchy

\tpartition LocalDateTable_{guid} = calculated
\t\tmode: import
\t\tsource = Calendar(Date(Year(MIN('{table_name}'[{date_column}])), 1, 1), Date(Year(MAX('{table_name}'[{date_column}])), 12, 31))

\tannotation __PBI_LocalDateTable = true
"""


def _find_datetime_columns(tables: List[Dict[str, Any]]) -> List[Tuple[str, str]]:
    """Return a list of (table_name, column_name) tuples for all datetime columns."""
    from app.model.table_tmdl import _clean_table_name
    results: List[Tuple[str, str]] = []
    seen = set()

    for table in tables:
        raw_name = text(table.get("name") or table.get("table_name"))
        table_name = _clean_table_name(raw_name)
        if not table_name:
            continue

        for col_entry in as_list(table.get("columns")) or as_list(table.get("fields")) or []:
            col = as_dict(col_entry)
            dtype = text(
                col.get("fabric_datatype") or
                col.get("powerbi_data_type") or
                col.get("qlik_datatype") or
                col.get("dataType") or ""
            ).lower()

            if "date" in dtype or "time" in dtype:
                col_name = text(
                    col.get("fabric_column_name") or
                    col.get("qlik_column_name") or
                    col.get("name")
                )
                key = (table_name, col_name)
                if col_name and key not in seen:
                    seen.add(key)
                    results.append(key)

    return results


def build_local_date_tables(tables: List[Dict[str, Any]]) -> Dict[str, str]:
    """
    Build hidden LocalDateTable TMDL content for each datetime column.

    Returns:
        dict of {"LocalDateTable_{guid}": tmdl_content}
    """
    date_columns = _find_datetime_columns(tables)
    result: Dict[str, str] = {}

    for table_name, date_column in date_columns:
        guid = uuid.uuid4().hex  # 32-char hex without hyphens
        key = f"LocalDateTable_{guid}"
        content = _LOCAL_DATE_TABLE_TEMPLATE.format(
            guid=guid,
            table_name=table_name,
            date_column=date_column,
            table_lineage_tag=str(uuid.uuid4()),
            date_lineage_tag=str(uuid.uuid4()),
            year_lineage_tag=str(uuid.uuid4()),
            monthno_lineage_tag=str(uuid.uuid4()),
            month_lineage_tag=str(uuid.uuid4()),
            quarterno_lineage_tag=str(uuid.uuid4()),
            quarter_lineage_tag=str(uuid.uuid4()),
            day_lineage_tag=str(uuid.uuid4()),
            hierarchy_lineage_tag=str(uuid.uuid4()),
            year_level_lineage_tag=str(uuid.uuid4()),
            quarter_level_lineage_tag=str(uuid.uuid4()),
            month_level_lineage_tag=str(uuid.uuid4()),
            day_level_lineage_tag=str(uuid.uuid4()),
        )
        result[key] = content

    return result
