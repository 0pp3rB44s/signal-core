"""Run the CGC Audit Engine V2 JSON pipeline."""

from pathlib import Path
from agents_v2.ai.ollama_client import ask
from agents_v2.ai.prompt_builder import build_prompt
from agents_v2.ai.response_parser import normalize, parse_json_response, validate_audit, invalid_audit
from agents_v2.shared.report_writer import write_reports


def run(context_text: str, allowed_files: set[str]) -> dict:
    prompt = build_prompt(context_text)
    raw = ask(prompt)
    if not raw.strip():
        raw = "{}"
    debug_dir = Path("agents_v2/reports")
    debug_dir.mkdir(parents=True, exist_ok=True)
    (debug_dir / "raw_response.txt").write_text(raw, encoding="utf-8")
    norm = normalize(raw)
    parsed, parse_issues = parse_json_response(norm)
    valid, validation_issues = validate_audit(parsed, allowed_files)
    all_issues = parse_issues + validation_issues
    audit = parsed if valid and parsed is not None else invalid_audit(all_issues)
    audit.setdefault("audit_source", "ai_engine_v2")
    audit.setdefault("ai_status", "validated" if valid else "fallback")
    json_path, markdown_path = write_reports(audit)
    status = "VALIDATED" if valid else "FALLBACK"
    print(f"AI audit status: {status}")
    if not valid:
        print(f"Validation issues: {all_issues}")
    print(f"AI JSON audit written to: {json_path}")
    print(f"AI Markdown audit written to: {markdown_path}")
    print(f"Raw AI response written to: {debug_dir / 'raw_response.txt'}")
    return audit


if __name__ == "__main__":
    run("TEST_CONTEXT", set())