import json
import re
import gc

class MemoryFileAgent:
    def __init__(self, real_agent, bypass_real_agent=False):
        self.real_agent = real_agent
        self.captured_files = {}
        self.bypass_real_agent = bypass_real_agent
        self.repo = None
        self.org = None
        self.project = None
        self.token = None
        self.branch = None

    def clear_captured_files(self):
        """Drop in-memory PBIP payloads to reduce container memory pressure."""
        self.captured_files.clear()
        gc.collect()

    async def create_or_update_file(self, file_path, content, commit_message, **kwargs):
        self.captured_files[file_path] = content
        if self.bypass_real_agent:
            return True
        
        # Inject stored attributes if missing
        if "repo" not in kwargs and self.repo: kwargs["repo"] = self.repo
        if "org" not in kwargs and self.org: kwargs["org"] = self.org
        if "project" not in kwargs and self.project: kwargs["project"] = self.project
        if "token" not in kwargs and self.token: kwargs["token"] = self.token
        if "branch" not in kwargs and getattr(self, "branch", None): kwargs["branch"] = self.branch
        
        return await self.real_agent.create_or_update_file(file_path, content, commit_message, **kwargs)

    async def batch_update_files(self, changes, repo=None, **kwargs):
        repo = repo or self.repo
        for path, content, action in changes:
            if action in ["add", "edit"] and content is not None:
                self.captured_files[path] = content
        
        if self.bypass_real_agent:
            return True
            
        # Inject stored attributes if missing
        if not repo and self.repo:
            repo = self.repo
        if "org" not in kwargs and self.org: kwargs["org"] = self.org
        if "project" not in kwargs and self.project: kwargs["project"] = self.project
        if "token" not in kwargs and self.token: kwargs["token"] = self.token
        if "branch" not in kwargs and getattr(self, "branch", None): kwargs["branch"] = self.branch

        if not repo:
            raise ValueError("MemoryFileAgent requires repo for Azure DevOps write operations")
            
        return await self.real_agent.batch_update_files(changes, repo=repo, **kwargs)

    def build_file_tree(self, base_folder):
        tree = {}
        for path, content in self.captured_files.items():
            if not path.startswith(base_folder):
                continue
            rel_path = path[len(base_folder):].lstrip("/")
            parts = rel_path.split("/")
            
            current = tree
            for part in parts[:-1]:
                if part not in current:
                    current[part] = {}
                current = current[part]
                
            filename = parts[-1]
            if filename.endswith(".json"):
                try:
                    current[filename] = json.loads(content)
                except (json.JSONDecodeError, TypeError, ValueError):
                    current[filename] = str(content)
            elif filename.endswith(".tmdl") and "table" in content[:20]:
                current[filename] = self.parse_tmdl_table(content)
            else:
                current[filename] = str(content)
        return tree

    def parse_tmdl_table(self, tmdl_str):
        import re
        lines = tmdl_str.replace('\r', '').split('\n')
        table_dict = {
            "name": "",
            "columns": [],
            "measures": [],
            "partitions": [],
            "calculationItems": []
        }
        
        current_type = None
        current_obj = None

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
                
            m_table = re.match(r"^table\s+'?([^'\n]+)'?", line)
            if m_table:
                table_dict["name"] = m_table.group(1)
                continue
                
            m_col = re.match(r"^\s+column\s+'?([^'\n=]+)'?", line)
            if m_col:
                current_obj = {"name": m_col.group(1), "properties": {}}
                table_dict["columns"].append(current_obj)
                current_type = "column"
                if "=" in line:
                    current_obj["isCalculated"] = True
                    current_obj["expression"] = line.split("=", 1)[1].strip() + "\n"
                continue
                
            m_meas = re.match(r"^\s+measure\s+'?([^'\n=]+)'?", line)
            if m_meas:
                current_obj = {"name": m_meas.group(1), "properties": {}, "expression": ""}
                table_dict["measures"].append(current_obj)
                current_type = "measure"
                if "=" in line:
                    current_obj["expression"] += line.split("=", 1)[1].strip() + "\n"
                continue
                
            m_part = re.match(r"^\s+partition\s+'?([^'\n=]+)'?", line)
            if m_part:
                current_obj = {"name": m_part.group(1), "properties": {}, "expression": ""}
                table_dict["partitions"].append(current_obj)
                current_type = "partition"
                if "=" in line:
                    current_obj["expression"] += line.split("=", 1)[1].strip() + "\n"
                continue

            m_calc = re.match(r"^\s+calculationItem\s+'?([^'\n=]+)'?", line)
            if m_calc:
                current_obj = {"name": m_calc.group(1), "properties": {}, "expression": ""}
                table_dict["calculationItems"].append(current_obj)
                current_type = "calculationItem"
                if "=" in line:
                    current_obj["expression"] += line.split("=", 1)[1].strip() + "\n"
                continue
                
            if current_obj is not None:
                if current_type in ("column", "measure", "partition", "calculationItem"):
                    m_prop = re.match(r"^\s+([a-zA-Z0-9_]+)\s*[:=]\s*(.+)", line)
                    if m_prop and current_type not in ("partition"):
                        k, v = m_prop.group(1), m_prop.group(2)
                        # Filter out properties vs expressions for measures/calcItems
                        if k in ("formatString", "isHidden", "displayFolder", "description", "dataType", "sourceColumn", "summarizeBy", "sortByColumn", "annotation"):
                            current_obj["properties"][k.strip()] = v.strip()
                        else:
                            if "expression" in current_obj:
                                current_obj["expression"] += stripped + "\n"
                            elif current_type == "column":
                                current_obj["isCalculated"] = True
                                current_obj["expression"] = stripped + "\n"
                    else:
                        if "expression" in current_obj:
                            current_obj["expression"] += stripped + "\n"
                        elif current_type == "column" and not stripped.startswith("column") and not stripped.startswith("measure") and ":" not in stripped:
                            current_obj["isCalculated"] = True
                            current_obj["expression"] = (current_obj.get("expression") or "") + stripped + "\n"

        for lst in (table_dict["columns"], table_dict["measures"], table_dict["partitions"], table_dict["calculationItems"]):
            for obj in lst:
                if "expression" in obj:
                    obj["expression"] = obj["expression"].strip()

        return table_dict

