"""Allow running as: python -m itasca_mcp_bridge

The `main()` call at the bottom sits under an `if __name__` guard, and that
is load-bearing rather than conventional. The console script's entry point is
`itasca_mcp_bridge.__main__:main`, so the wrapper pip generates imports this
module in order to reach `main`. A bare call at module level therefore runs
during that import -- the bridge binds its port and starts serving while the
wrapper is still importing -- and the wrapper's own `sys.exit(main())` then
calls `main()` a second time on top of it, against a port it already owns.
"""

import argparse
import sys

from itasca_mcp_bridge import __version__, start


def main():
    parser = argparse.ArgumentParser(
        prog="itasca-mcp-bridge",
        description="Itasca MCP Bridge - HTTP bridge for ITASCA products (PFC, FLAC3D, 3DEC, MPoint, MassFlow)",
    )
    parser.add_argument(
        "--version", "-v", action="version", version="itasca-mcp-bridge {}".format(__version__)
    )
    parser.add_argument("--host", default="localhost", help="server host (default: localhost)")
    parser.add_argument("--port", type=int, default=9001, help="server port (default: 9001)")
    parser.add_argument("--mode", choices=["auto", "gui", "console"], default="auto",
                        help="task pump mode (default: auto)")
    parser.add_argument("--no-upgrade", action="store_true",
                        help="skip the PyPI update check and start the installed version")
    args = parser.parse_args()

    start(host=args.host, port=args.port, mode=args.mode, auto_upgrade=not args.no_upgrade)
    return 0


if __name__ == "__main__":
    sys.exit(main())
