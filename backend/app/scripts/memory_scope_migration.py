"""E43 CLI: report (default) or apply the move of old shared memory.

    python -m app.scripts.memory_scope_migration --report /tmp/e43.json
    python -m app.scripts.memory_scope_migration --apply --approved-by <who> \
        --report /tmp/e43.json [--batch 100] [--after <fact id>]

Applying reads the decisions from a reviewed report file; it never classifies
afresh, so what was reviewed is what moves.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.domain.memory_migration import Decision, Report, apply_report, build_report


async def _main(args: argparse.Namespace) -> int:
    from app.db.session import _get_session_factory

    async with _get_session_factory()() as db:
        if not args.apply:
            report = await build_report(db)
            data = report.as_dict()
            with open(args.report, "w", encoding="utf-8") as out:
                json.dump(data, out, ensure_ascii=False, indent=2)
            print(json.dumps({"counts": data["counts"], "report": args.report}, ensure_ascii=False))
            return 0
        with open(args.report, encoding="utf-8") as src:
            data = json.load(src)
        report = Report(
            rule_version=data["rule_version"],
            generated_at=data["generated_at"],
            decisions=[Decision(**d) for d in data["decisions"]],
        )
        result = await apply_report(
            db, report, approved_by=args.approved_by, batch=args.batch, after=args.after
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approved-by", default="")
    parser.add_argument("--batch", type=int, default=100)
    parser.add_argument("--after")
    return asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
