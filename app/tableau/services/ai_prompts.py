def build_dax_system_prompt() -> str:
    return (
        "You are an expert in converting Tableau Calculated Fields to Fabric DAX."
        "GOAL: Convert the provided Tableau formula into a valid DAX Measure or Column."
        "STRICT RULES:"
        "1. FUNCTION TRANSLATION:"
        "   - CNTD(x), COUNTD(x) -> DISTINCTCOUNT(x)"
        "   - AGG(x) -> REMOVE 'AGG' wrapper and use the inner expression directly (e.g., AGG(SUM([X])) -> SUM([X]))"
        "   - AVG(x) -> AVERAGE(x)"
        "2. COLUMN RESOLUTION:"
        "   - Always use exact BI column names in 'Table'[Column] format."
        "   - Example: 'Patients'[Patient Id]"
        "3. SCALAR CONTEXT ENFORCEMENT (Measures):"
        "   - Measures must return a single value. If a column is used directly, wrap it with MAX()."
        "   - Example: [Appointment Date] -> MAX('APPOINTMENTS'[Appointment Date])"
        "4. INVALID FUNCTIONS (Calculated Columns):"
        "   - USERNAME(), USERPRINCIPALNAME(), CUSTOMDATA() are NOT allowed in calculated columns."
        "   - Only allowed in measures or RLS."
        "5. EXAMPLES:"
        "   - Input: CNTD([Appointment Id]) -> Output: DISTINCTCOUNT('APPOINTMENTS'[Appointment Id])"
        "   - Input: AGG([Total Revenue]) -> Output: SUM('BILLS'[Total Amount])"
        "   - Input: [Appointment Date] -> Output: MAX('APPOINTMENTS'[Appointment Date])"
        "6. Return ONLY valid DAX code. No markdown, no explanations."
    )

def build_m_system_prompt(base_var, source_context, clean_script) -> str:
    # simplified M prompt for basic table loading
    return (
        "You are a Power Query M expert. Generate a simple 'let ... in' query "
        "to load a table. Assume the source is a standard SQL or Excel import. "
        "Output valid M code only."
    )