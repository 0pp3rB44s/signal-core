"""Morning audit entrypoint for CGC Audit Engine V2."""

from agents_v2.shared.context_builder import build_context
from agents_v2.ai.ai_audit_runner import run
from agents_v2.shared.rule_audit import run_rule_audit
from agents_v2.shared.report_writer import write_reports


def format_blocks(title: str, blocks: dict[str, str]) -> str:
    if not blocks:
        return f"## {title}\nNOT PROVEN\n"

    body = "\n\n".join(
        f"===== {name} =====\n{content}"
        for name, content in blocks.items()
    )
    return f"## {title}\n{body}\n"


def main() -> None:
    context = build_context()

    rule_audit = run_rule_audit(context)

    context_text = "\n\n".join(
        [
            format_blocks("LOG_CONTEXT", context.get("logs", {})),
            format_blocks("CODE_CONTEXT", context.get("code", {})),
            format_blocks("ROADMAP_CONTEXT", context.get("roadmap", {})),
            format_blocks("DATASET_CONTEXT", context.get("dataset", {})),
            format_blocks("SETTINGS_CONTEXT", context.get("settings", {})),
        ]
    )

    allowed_files = set(context.get("code", {}).keys())

    ai_audit = run(context_text, allowed_files)

    final_audit = ai_audit
    if ai_audit.get("ai_status") != "validated":
        final_audit = rule_audit
        final_audit["ai_status"] = "fallback"

    write_reports(final_audit)


if __name__ == "__main__":
    main()
