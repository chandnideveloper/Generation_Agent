import json

with open(r'c:\Users\sohit\Downloads\QliktoFabrics_backup\mapping.md', 'r', encoding='utf-8') as f:
    data = json.load(f)

for t in data.get('tables', []):
    tname = t.get('name')
    q = t.get('qlik_query') or ''
    csql = t.get('custom_sql') or ''
    conn = t.get('connection_details') or t.get('connection') or {}
    print(f"Table {tname:15} | schema: {t.get('schema')} | conn_schema: {conn.get('schema')}")
    for line in q.splitlines():
        if "from " in line.lower() or "select " in line.lower():
            print(f"  qlik_query line: {line.strip()}")