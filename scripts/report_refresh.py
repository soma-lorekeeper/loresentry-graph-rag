"""저장된 평가 결과로 Markdown 재생성: .venv/bin/python -m scripts.report_refresh 경로."""

import argparse
from pathlib import Path

from evaluation.markdown import write_report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="평가 JSON이 저장된 디렉터리")
    args = parser.parse_args(argv)
    try:
        target = write_report(args.directory)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
