"""Build JSON-only prompts for CGC Audit Engine V2."""

def build_prompt(context_text: str) -> str:
    """
    Build a prompt instructing the model to return ONLY a JSON object with keys:
    summary, critical, high, medium, low, root_cause, patch_candidates, files_to_review, risk_if_unchanged, confidence.
    - NEVER invent facts or filenames.
    - If evidence is insufficient, use the string 'NOT PROVEN' ONLY for text fields.
    - For list fields, return empty lists ([]).
    - The confidence field MUST always be numeric between 0.0 and 1.0. If evidence is insufficient, set confidence to 0.0.
    - The first character of the response MUST be '{'.
    - The last character of the response MUST be '}'.
    - Return exactly one JSON object.
    - Do not output markdown, code fences, explanations, reasoning, or any text before or after the JSON.
    - If the model cannot comply, return '{}'.
    - Output ONLY a JSON object with the above keys.
    - CONTEXT follows below.
    CONTEXT:
    {context}
    """
    return (
        "You are an expert code auditor. "
        "Return ONLY a single JSON object with the following keys: "
        "summary, critical, high, medium, low, root_cause, patch_candidates, files_to_review, risk_if_unchanged, confidence. "
        "Each of critical, high, medium, low, patch_candidates, files_to_review must be a list of strings. "
        "NEVER invent facts or filenames. "
        "If evidence is insufficient, use the string 'NOT PROVEN' ONLY for text fields. "
        "For list fields, return empty lists ([]). "
        "The confidence field MUST always be numeric between 0.0 and 1.0. If evidence is insufficient, set confidence to 0.0. "
        "The first character of the response MUST be '{'. "
        "The last character of the response MUST be '}'. "
        "Return exactly one JSON object. "
        "Do not output markdown, code fences, explanations, reasoning, or any text before or after the JSON. "
        "If the model cannot comply, return '{}'. "
        "CONTEXT:\n"
        f"{context_text}"
    )