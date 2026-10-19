import pytest
from app.tableau.core_logic.tmdlgenerator.generator import TmdlGenerator

class MockAgent:
    def __init__(self):
        self.captured_files = {}
    async def create_or_update_file(self, path, content, msg, **kwargs):
        self.captured_files[path] = content
        return True

def test_calculated_column_tmdl_syntax():
    gen = TmdlGenerator(MockAgent(), MockAgent())
    
    table_def = {
        "table_name": "organizations",
        "columns": [
            {"name": "Id", "datatype": "string"},
            {"name": "Name", "datatype": "string"},
            {
                "name": "Username",
                "datatype": "string",
                "dax_formula": "'organizations'[Name]"
            },
            {
                "name": "EmptyCalc",
                "datatype": "string",
                "dax_formula": ""
            }
        ]
    }
    
    extra_calcs = [
        {
            "name": "FullLocation",
            "data_type": "string",
            "dax_formula": "'organizations'[City] & \", \" & 'organizations'[State]"
        }
    ]
    
    tmdl_content, _ = gen._generate_table_tmdl(
        table_def=table_def,
        connection_info={},
        extra_measures=[],
        extra_calcs=extra_calcs
    )
    
    # 1. Verify 'Username' has DAX formula on the SAME line after '='
    assert "column 'Username' = 'organizations'[Name]" in tmdl_content
    
    # 2. Verify neither dataType nor summarizeBy appear inside the DAX block
    # Check lines following column 'Username'
    lines = tmdl_content.splitlines()
    for i, line in enumerate(lines):
        if "column 'Username' =" in line:
            # Must be exactly on the same line
            assert line.strip() == "column 'Username' = 'organizations'[Name]"
            # Next line must be a property, not part of expression
            assert lines[i+1].strip().startswith("dataType:")
            assert lines[i+2].strip().startswith("lineageTag:")
            assert lines[i+3].strip().startswith("summarizeBy: none")
            break
            
    # 3. Verify 'FullLocation' from extra_calcs is also on the same line
    assert "column 'FullLocation' = 'organizations'[City] & \", \" & 'organizations'[State]" in tmdl_content
    for i, line in enumerate(lines):
        if "column 'FullLocation' =" in line:
            assert lines[i+1].strip().startswith("dataType:")
            assert lines[i+2].strip().startswith("lineageTag:")
            assert lines[i+3].strip().startswith("summarizeBy: none")
            break

    # 4. Verify EmptyCalc fell back cleanly to sourceColumn instead of broken '='
    assert "column 'EmptyCalc' =" not in tmdl_content
    assert "sourceColumn: EmptyCalc" in tmdl_content
