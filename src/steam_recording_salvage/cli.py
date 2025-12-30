import argparse
from pathlib import Path

from .scan import discover_sessions, find_sessions
from .exporter import export_session, ExportError


def main() -> int:
    parser = argparse.ArgumentParser(prog="steam-recording-salvage")
    subcommands = parser.add_subparsers(dest="command", required=True)

    scan_cmd = subcommands.add_parser("scan", help="Find Steam recording sessions.")
    scan_cmd.add_argument("--root", type=Path, help="Folder to scan instead of defaults.")

    export_cmd = subcommands.add_parser("export", help="Export a session to MP4.")
    export_cmd.add_argument("mpd", type=Path, help="Path to session.mpd")
    export_cmd.add_argument("-o", "--out", type=Path, required=True, help="Output MP4 path")
    export_cmd.add_argument("--ffmpeg", type=Path, help="Path to ffmpeg binary")
    export_cmd.add_argument("--overwrite", action="store_true")

    args = parser.parse_args()

    if args.command == "scan":
        sessions = find_sessions(args.root) if args.root else discover_sessions()

        if not sessions:
            print("No recording sessions found.")
            return 0

        for i, session in enumerate(sessions, start=1):
            print(f"{i:03d}  {session.mpd_path}")
        return 0

    if args.command == "export":
        try:
            export_session(
                args.mpd,
                args.out,
                ffmpeg_path=args.ffmpeg,
                overwrite=args.overwrite,
            )
        except ExportError as err:
            print(err)
            return 2

        print(f"Exported: {args.out}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
