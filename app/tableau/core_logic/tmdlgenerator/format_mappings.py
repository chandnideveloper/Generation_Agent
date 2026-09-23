
def get_pbi_format_for_currency(symbol: str) -> dict:
    """
    Maps a Tableau/BI currency symbol to Power BI TMDL formatting info.
    Returns a dict with 'formatString' and 'culture'.
    Defaults to Australian Dollar if symbol is unrecognized.
    """
    # Mapping from input symbol to the requested Power BI label
    CURRENCY_TO_LABEL = {
        "$ US Dollar": "$ American English",
        "₹ Indian Rupee": "Hindi (India)",
        "A$ Australian Dollar": "$ Australian English",
        "£ British Pound": "British English",
        "€ Euro": "€ Euro (€ 123)",
        "€ Euro (German)": "€ Euro (123 €)"
    }

    # Mapping from Power BI label to DAX formatString and culture code
    LABEL_TO_FORMAT = {
        "$ American English": {
            "formatString": r'"\$"#,0.00;("\$"#,0.00);"\$"#,0.00',
            "culture": "en-US"
        },
        "Hindi (India)": {
            "formatString": r'"₹" #,0.00;-"₹" #,0.00;"₹" #,0.00',
            "culture": "hi-IN"
        },
        "$ Australian English": {
            "formatString": r'"\$"#,0.00;("\$"#,0.00);"\$"#,0.00',
            "culture": "en-AU"
        },
        "British English": {
            "formatString": r'"£"#,0.00;-"£"#,0.00;"£"#,0.00',
            "culture": "en-GB"
        },
        "€ Euro (€ 123)": {
            "formatString": r'"€" #,0.00;-"€" #,0.00;"€" #,0.00',
            "culture": "fr-FR"
        },
        "€ Euro (123 €)": {
            "formatString": r'#,0.00 "€";-#,0.00 "€";#,0.00 "€"',
            "culture": "de-DE"
        }
    }

    # Default fallback
    DEFAULT_INFO = LABEL_TO_FORMAT["$ Australian English"]

    if not symbol:
        return DEFAULT_INFO

    label = CURRENCY_TO_LABEL.get(symbol)
    if not label:
        # Check for partial matches (e.g. "€ Euro" inside something else)
        for s, l in CURRENCY_TO_LABEL.items():
            if s in symbol:
                label = l
                break
    
    return LABEL_TO_FORMAT.get(label, DEFAULT_INFO)
