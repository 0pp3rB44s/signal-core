"""Run the JSON-first CGC AI audit pipeline."""

from agents.ai.ollama_client import ask
from agents.ai.prompt_builder import build_prompt
from agents.ai.response_parser import invalid_audit, normalize, parse_json_response, validate_audit
from agents.shared.context_builder import build_context
from agents.shared.report_writer import write_reports


def _format_named_blocks(blocks: dict[str, str]) -> str:
    return "\n\n".join(
        f"===== {name} =====\n{content}"
        for name, content in blocks.items()
    )


def run() -> None:
    context = build_context()
    loaded_logs = context["logs"]

    if not loaded_logs:
        print("No supported log files found.")
        return

    sections = ["## LOG_CONTEXT\n" + _format_named_blocks(loaded_logs)]

    if context.get("code"):
        sections.append("## CODE_CONTEXT\n" + _format_named_blocks(context["code"]))

    if context.get("roadmap"):
        sections.append("## ROADMAP_CONTEXT\n" + _format_named_blocks(context["roadmap"]))

    combined_context = "\n\n".join(sections)
    prompt = build_prompt(combined_context)
    raw_result = normalize(ask(prompt))

    parsed_audit, parse_issues = parse_json_response(raw_result)
    allowed_files = set(context.get("code", {}).keys())
    valid, validation_issues = validate_audit(parsed_audit, allowed_files=allowed_files)

    all_issues = parse_issues + validation_issues
    audit = parsed_audit if valid and parsed_audit is not None else invalid_audit(all_issues)

    json_path, markdown_path = write_reports(audit)

    if valid:
        print("AI validation passed.")
    else:
        print(f"AI validation failed: {all_issues}")

    print(f"AI JSON audit written to: {json_path}")
    print(f"AI Markdown audit written to: {markdown_path}")


if __name__ == "__main__":
    run()
