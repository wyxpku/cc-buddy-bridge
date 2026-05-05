"""Entry point. `cc-buddy-bridge [daemon|install|uninstall|status]`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import socket
import sys
from pathlib import Path

from . import __version__
from .daemon import Daemon
from .ipc import DEFAULT_SOCKET_PATH


def _default_log_path() -> Path:
    """Platform-appropriate log file path."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "cc-buddy-bridge.log"
    if sys.platform == "win32":
        return Path.home() / "AppData" / "Local" / "cc-buddy-bridge" / "daemon.log"
    return Path.home() / ".local" / "share" / "cc-buddy-bridge" / "daemon.log"


PID_PATH = "/tmp/cc-buddy-bridge.pid" if sys.platform != "win32" else str(
    Path(os.environ.get("TEMP", "/tmp")) / "cc-buddy-bridge.pid"
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cc-buddy-bridge")
    parser.add_argument("--version", action="version", version=f"cc-buddy-bridge {__version__}")
    sub = parser.add_subparsers(dest="cmd")

    p_daemon = sub.add_parser("daemon", help="Run the bridge daemon (connects to BLE device, serves hooks)")
    p_daemon.add_argument("--socket", default=None, help="Unix socket path (default /tmp/cc-buddy-bridge.sock)")
    p_daemon.add_argument("--device-name", default="Claude", help="BLE name prefix to match (default: Claude)")
    p_daemon.add_argument("--device-address", default=None, help="BLE address to connect to (skips scan)")
    p_daemon.add_argument("--log-level", default="INFO")
    p_daemon.add_argument(
        "--foreground", "-f", action="store_true",
        help="Run in foreground (do not daemonize; for launchd/systemd)",
    )
    p_daemon.add_argument(
        "--log-file", default=None,
        help="Log file path (default: platform-specific path)",
    )

    p_install = sub.add_parser("install", help="Register hooks in ~/.claude/settings.json")
    p_install.add_argument(
        "--service", action="store_true",
        help="Install a user-level service so the daemon auto-starts on login "
             "(macOS: launchd agent; Linux: systemd user unit) instead of registering hooks",
    )
    p_uninstall = sub.add_parser("uninstall", help="Remove cc-buddy-bridge hooks from ~/.claude/settings.json")
    p_uninstall.add_argument(
        "--service", action="store_true",
        help="Remove the user-level service (launchd agent / systemd unit) instead of removing hooks",
    )
    sub.add_parser("stop", help="Stop the running background daemon")
    sub.add_parser("status", help="Show install status")

    p_hud = sub.add_parser(
        "hud",
        help="Print a one-line stick status summary (stdout; designed for Claude Code's statusLine)",
    )
    p_hud.add_argument("--ascii", action="store_true", help="ASCII-only output (no emoji)")
    p_hud.add_argument("--socket", default=None, help="Unix socket path override")

    sub.add_parser(
        "unpair",
        help="Clear the stick's stored BLE bond (you must also Forget on the macOS side afterwards)",
    )

    p_push = sub.add_parser(
        "push-character",
        help="Upload a GIF character pack folder to the stick (manifest.json + *.gif)",
    )
    p_push.add_argument("path", help="Path to the character folder")

    args = parser.parse_args(argv)
    if args.cmd is None:
        parser.print_help()
        return 1

    if args.cmd == "daemon":
        return _run_daemon(args)
    if args.cmd == "stop":
        return _stop_daemon()
    if args.cmd == "install":
        if getattr(args, "service", False):
            from .service import install_service
            return install_service()
        from .installer import install_hooks
        return install_hooks()
    if args.cmd == "uninstall":
        if getattr(args, "service", False):
            from .service import uninstall_service
            return uninstall_service()
        from .installer import uninstall_hooks
        return uninstall_hooks()
    if args.cmd == "status":
        from .installer import show_status
        return show_status()
    if args.cmd == "hud":
        from .hud import run as hud_run
        return hud_run(ascii_only=args.ascii, socket_path=args.socket)
    if args.cmd == "unpair":
        return _run_unpair()
    if args.cmd == "push-character":
        return _run_push_character(args.path)

    return 1


def _run_daemon(args: argparse.Namespace) -> int:
    socket_path = args.socket or DEFAULT_SOCKET_PATH
    if _socket_in_use(socket_path):
        print(
            f"cc-buddy-bridge: another daemon is already listening at {socket_path}.\n"
            f"  Stop it first, or pass --socket to use a different path.",
            file=sys.stderr,
        )
        return 2

    if not args.foreground:
        return _daemonize_and_run(args)

    # Foreground mode (for launchd / systemd / debugging)
    log_file = args.log_file or str(_default_log_path())
    _setup_logging(args.log_level, log_file)
    return _run_daemon_loop(args)


def _daemonize_and_run(args: argparse.Namespace) -> int:
    """Double-fork to background, redirect stdout/stderr to log file, write PID."""
    log_file = args.log_file or str(_default_log_path())
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    if sys.platform == "win32":
        return _daemonize_windows(args, log_file)

    # --- Unix double-fork ---
    pid = os.fork()
    if pid > 0:
        # Parent: wait briefly for child to write PID, then exit.
        import time
        time.sleep(0.2)
        if os.path.exists(PID_PATH):
            child_pid = Path(PID_PATH).read_text().strip()
            print(f"cc-buddy-bridge: daemon started (pid {child_pid})")
            print(f"  logs: {log_file}")
            print(f"  stop: cc-buddy-bridge stop")
        else:
            print("cc-buddy-bridge: daemon may have failed to start", file=sys.stderr)
            return 1
        return 0

    # First child: become session leader.
    os.setsid()

    pid2 = os.fork()
    if pid2 > 0:
        os._exit(0)

    # Second child: write PID, redirect fds, run.
    os.chdir("/")
    Path(PID_PATH).write_text(str(os.getpid()))

    # Close stdio and redirect to log file.
    log_fd = os.open(log_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(log_fd, 1)  # stdout
    os.dup2(log_fd, 2)  # stderr
    os.close(log_fd)
    # Redirect stdin to /dev/null
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)

    _setup_logging(args.log_level, log_file)

    # Remove PID file on exit.
    import atexit
    atexit.register(_cleanup_pid)

    return _run_daemon_loop(args)


def _daemonize_windows(args: argparse.Namespace, log_file: str) -> int:
    """Windows: spawn a detached subprocess, write PID, return."""
    import subprocess
    cmd = [sys.executable, "-m", "cc_buddy_bridge.cli", "daemon", "--foreground",
           "--log-file", log_file, "--log-level", args.log_level]
    if args.socket:
        cmd += ["--socket", args.socket]
    cmd += ["--device-name", args.device_name]
    if args.device_address:
        cmd += ["--device-address", args.device_address]

    with open(log_file, "a") as lf:
        proc = subprocess.Popen(
            cmd,
            stdout=lf,
            stderr=lf,
            stdin=subprocess.DEVNULL,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,
        )

    Path(PID_PATH).write_text(str(proc.pid))
    print(f"cc-buddy-bridge: daemon started (pid {proc.pid})")
    print(f"  logs: {log_file}")
    print(f"  stop: cc-buddy-bridge stop")
    return 0


def _setup_logging(log_level: str, log_file: str) -> None:
    """Configure root logger to write to both console and file."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # File handler — always active in daemon mode.
    fh = logging.FileHandler(log_file)
    fh.setLevel(root.level)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(fh)

    # Console handler — only if stdout is a terminal (foreground mode).
    if sys.stdout.isatty():
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(root.level)
        ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(ch)


def _cleanup_pid() -> None:
    try:
        os.unlink(PID_PATH)
    except OSError:
        pass


def _stop_daemon() -> int:
    """Stop the running background daemon."""
    pid_path = Path(PID_PATH)
    if not pid_path.exists():
        print("cc-buddy-bridge: no PID file found — daemon not running?")
        return 1

    pid_str = pid_path.read_text().strip()
    if not pid_str:
        print("cc-buddy-bridge: empty PID file")
        pid_path.unlink(missing_ok=True)
        return 1

    try:
        pid = int(pid_str)
    except ValueError:
        print(f"cc-buddy-bridge: invalid PID file content: {pid_str!r}")
        pid_path.unlink(missing_ok=True)
        return 1

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        print(f"cc-buddy-bridge: process {pid} not found (stale PID file)")
        pid_path.unlink(missing_ok=True)
        return 1
    except PermissionError:
        print(f"cc-buddy-bridge: no permission to kill process {pid}", file=sys.stderr)
        return 1

    print(f"cc-buddy-bridge: sent SIGTERM to daemon (pid {pid})")
    pid_path.unlink(missing_ok=True)
    return 0


def _run_daemon_loop(args: argparse.Namespace) -> int:
    """Create the Daemon and run the asyncio event loop."""
    daemon = Daemon(
        socket_path=args.socket,
        device_name_prefix=args.device_name,
        device_address=args.device_address,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _sigterm(*_: object) -> None:
        asyncio.ensure_future(daemon.shutdown(), loop=loop)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _sigterm)
        except NotImplementedError:
            pass

    try:
        loop.run_until_complete(daemon.run())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()
    return 0


def _run_push_character(path: str) -> int:
    from .hooks._client import post

    # Pushing a full 1.8 MB pack at BLE speeds can take 1-2 minutes with the
    # per-chunk ack requirement. Give the IPC call plenty of headroom.
    resp = post({"evt": "push_character", "path": path}, timeout=600.0)
    if resp is None:
        print(
            "cc-buddy-bridge: daemon not reachable. Start it first.",
            file=sys.stderr,
        )
        return 2
    if not resp.get("ok"):
        print(f"push failed: {resp.get('error', 'unknown')}", file=sys.stderr)
        return 2

    name = resp.get("name", "?")
    files = resp.get("files", 0)
    size = resp.get("total_bytes", 0)
    print(f"pushed '{name}': {files} files, {size:,} bytes")
    print("the stick has switched to the new character.")
    return 0


def _run_unpair() -> int:
    """Tell the running daemon to send cmd:unpair to the stick."""
    from .hooks._client import post

    resp = post({"evt": "unpair"}, timeout=2.0)
    if resp is None:
        print(
            "cc-buddy-bridge: daemon not reachable. Start it with "
            "`cc-buddy-bridge daemon` (or via the launchd agent).",
            file=sys.stderr,
        )
        return 2
    if not resp.get("ok"):
        err = resp.get("error", "unknown")
        print(f"cc-buddy-bridge: unpair failed ({err})", file=sys.stderr)
        return 2

    print("sent cmd:unpair to the stick — its stored bond is cleared.")
    print("")
    print("Next: open macOS System Settings → Bluetooth → Claude-5C66 → ⓘ →")
    print("'Forget This Device' to purge the cached LTK. Then the next reconnect")
    print("will prompt for a fresh 6-digit passkey (displayed on the stick).")
    print("")
    print("Watch `tail -f ~/Library/Logs/cc-buddy-bridge.log` for the moment of truth:")
    print("  \"stick link: ENCRYPTED (was None)\"")
    return 0


def _socket_in_use(path: str) -> bool:
    """True iff a process is actively accepting on ``path``.

    On Unix: checks Unix socket file
    On Windows: reads port from file and checks TCP socket
    """
    if not os.path.exists(path):
        return False

    if sys.platform == "win32":
        # Windows: path is a port file, read port and check TCP socket
        try:
            from pathlib import Path
            port = int(Path(path).read_text().strip())
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.5)
            try:
                s.connect(("127.0.0.1", port))
                return True
            except (ConnectionRefusedError, OSError):
                # Stale port file
                try:
                    os.unlink(path)
                except OSError:
                    pass
                return False
            finally:
                try:
                    s.close()
                except OSError:
                    pass
        except (ValueError, OSError):
            return False
    else:
        # Unix: check Unix socket
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.5)
        try:
            s.connect(path)
        except (ConnectionRefusedError, FileNotFoundError):
            # Stale socket file — clean up and proceed.
            try:
                os.unlink(path)
            except OSError:
                pass
            return False
        except OSError:
            # Some other error (permissions, socket unreadable). Be conservative
            # and treat as in-use so we don't clobber something.
            return True
        else:
            return True
        finally:
            try:
                s.close()
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
