"""Install reviewed semantic records ahead of a source's generated positions."""

import argparse
from pathlib import Path

from schemer.hints import replace_hint_preamble


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="explicit .zen source to update")
    parser.add_argument("hints", type=Path, help="comments-only .zen hint file")
    args = parser.parse_args()
    before = args.source.read_text()
    after = replace_hint_preamble(before, args.hints.read_text())
    if before != after:
        args.source.write_text(after)
    print(args.source)


if __name__ == "__main__":
    main()
