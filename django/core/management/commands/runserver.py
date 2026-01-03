import errno
import os
import re
import socket
import sys
from datetime import datetime


from pathlib import Path
from typing import Iterable, List, Sequence, Tuple, Union
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.core.servers.basehttp import WSGIServer, get_internal_wsgi_application, run
from django.db import connections
from django.utils import autoreload
from django.utils.regex_helper import _lazy_re_compile
from django.utils.version import get_docs_version
from django.dispatch import receiver
from django.utils.autoreload import autoreload_started
import warnings

naiveip_re = _lazy_re_compile(
    r"""^(?:
(?P<addr>
    (?P<ipv4>\d{1,3}(?:\.\d{1,3}){3}) |         # IPv4 address
    (?P<ipv6>\[[a-fA-F0-9:]+\]) |               # IPv6 address
    (?P<fqdn>[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*) # FQDN
):)?(?P<port>\d+)$""",
    re.X,
)

PathLike = Union[str, Path]
WatchDirSpec = Tuple[PathLike, str]

def _validate_watch_config() -> Tuple[List[Path], List[Tuple[Path, str]]]:
    """
    Validate and normalize runserver watch configuration.

    Supported development-only settings:

    - RUNSERVER_WATCHFILES:
        Iterable of file paths (str or Path) that influence project settings.

    - RUNSERVER_WATCHDIRS:
        Iterable of (directory, glob_pattern) tuples used to watch
        groups of configuration files.

    Paths are normalized to Path objects. Relative paths are accepted
    but emit a warning; absolute paths are recommended.

    Returns:
        A tuple (files, dirs) where:
        - files is a list of Path objects.
        - dirs is a list of (Path, pattern) tuples.

    Raises:
        TypeError: If configuration values have invalid types or structure.
    """
    files = getattr(settings, "RUNSERVER_WATCHFILES", [])
    dirs = getattr(settings, "RUNSERVER_WATCHDIRS", [])

    if not isinstance(files, (list, tuple)):
        raise TypeError(
            "RUNSERVER_WATCHFILES must be a list or tuple of paths.\n"
            "Example:\n"
            "    RUNSERVER_WATCHFILES = [BASE_DIR / '.env', BASE_DIR / 'config/settings.yaml']"
        )

    if not isinstance(dirs, (list, tuple)):
        raise TypeError(
            "RUNSERVER_WATCHDIRS must be a list or tuple of (path, pattern) tuples.\n"
            "Example:\n"
            "    RUNSERVER_WATCHDIRS = [(BASE_DIR / 'config', '*.toml')]"
        )

    norm_files: List[Path] = []
    for item in files:
        path = Path(item).expanduser()
        if not path.is_absolute():
            warnings.warn(
                f"Relative path detected in RUNSERVER_WATCHFILES: {path!s}. "
                "Absolute paths (e.g. BASE_DIR / ...) are recommended.",
                RuntimeWarning,
            )
        norm_files.append(path)

    norm_dirs: List[Tuple[Path, str]] = []
    for entry in dirs:
        if not (isinstance(entry, (list, tuple)) and len(entry) == 2):
            raise TypeError(
                "Each RUNSERVER_WATCHDIRS entry must be a (path, pattern) tuple.\n"
                "Example:\n"
                "    RUNSERVER_WATCHDIRS = [(BASE_DIR / 'config', '*.toml')]"
            )

        directory, pattern = entry
        directory = Path(directory).expanduser()
        pattern = str(pattern)

        if not directory.is_absolute():
            warnings.warn(
                f"Relative directory detected in RUNSERVER_WATCHDIRS: {directory!s}. "
                "Absolute paths (e.g. BASE_DIR / ...) are recommended.",
                RuntimeWarning,
            )

        if pattern in {"*", "**", "**/*"}:
            warnings.warn(
                f"Broad glob pattern detected in RUNSERVER_WATCHDIRS: {pattern!r}. "
                "Such patterns may significantly degrade autoreload performance.",
                RuntimeWarning,
            )

        norm_dirs.append((directory, pattern))

    return norm_files, norm_dirs

def _file_family_pattern(path: Path) -> str:
    """
    Return a glob pattern suitable for watching a single configuration file.

    The returned pattern accounts for:
    - dotfile variants (e.g. '.env', '.env.local'),
    - atomic save strategies based on temporary files and rename operations.

    The pattern is intended for use with watch_dir() on the parent directory
    and avoids overly broad directory scans.
    """
    name = path.name

    if name.startswith("."):
        return f"{name}*"

    stem = path.stem
    suffix = path.suffix
    if suffix:
        return f"{stem}*{suffix}"

    return name

@receiver(autoreload_started)
def _watch_declared_settings_inputs(sender, **kwargs) -> None:
    """
    Register declared non-Python configuration inputs with the autoreloader.

    When any declared input changes, the development server process is
    restarted, ensuring that updated settings are applied consistently.
    """
    files, dirs = _validate_watch_config()

    for file_path in files:
        pattern = _file_family_pattern(file_path)
        sender.watch_dir(file_path.parent, pattern)

    for directory, pattern in dirs:
        sender.watch_dir(directory, pattern)

class Command(BaseCommand):
    help = "Starts a lightweight web server for development."

    stealth_options = ("shutdown_message",)
    suppressed_base_arguments = {"--verbosity", "--traceback"}

    default_addr = "127.0.0.1"
    default_addr_ipv6 = "::1"
    default_port = "8000"
    protocol = "http"
    server_cls = WSGIServer

    def add_arguments(self, parser):
        parser.add_argument(
            "addrport", nargs="?", help="Optional port number, or ipaddr:port"
        )
        parser.add_argument(
            "--ipv6",
            "-6",
            action="store_true",
            dest="use_ipv6",
            help="Tells Django to use an IPv6 address.",
        )
        parser.add_argument(
            "--nothreading",
            action="store_false",
            dest="use_threading",
            help="Tells Django to NOT use threading.",
        )
        parser.add_argument(
            "--noreload",
            action="store_false",
            dest="use_reloader",
            help="Tells Django to NOT use the auto-reloader.",
        )

    def execute(self, *args, **options):
        if options["no_color"]:
            # We rely on the environment because it's currently the only
            # way to reach WSGIRequestHandler. This seems an acceptable
            # compromise considering `runserver` runs indefinitely.
            os.environ["DJANGO_COLORS"] = "nocolor"
        super().execute(*args, **options)

    def get_handler(self, *args, **options):
        """Return the default WSGI handler for the runner."""
        return get_internal_wsgi_application()

    def get_check_kwargs(self, options):
        """Validation is called explicitly each time the server reloads."""
        return {"tags": set()}

    def handle(self, *args, **options):
        if not settings.DEBUG and not settings.ALLOWED_HOSTS:
            raise CommandError("You must set settings.ALLOWED_HOSTS if DEBUG is False.")

        self.use_ipv6 = options["use_ipv6"]
        if self.use_ipv6 and not socket.has_ipv6:
            raise CommandError("Your Python does not support IPv6.")
        self._raw_ipv6 = False
        if not options["addrport"]:
            self.addr = ""
            self.port = self.default_port
        else:
            m = re.match(naiveip_re, options["addrport"])
            if m is None:
                raise CommandError(
                    '"%s" is not a valid port number '
                    "or address:port pair." % options["addrport"]
                )
            self.addr, _ipv4, _ipv6, _fqdn, self.port = m.groups()
            if not self.port.isdigit():
                raise CommandError("%r is not a valid port number." % self.port)
            if self.addr:
                if _ipv6:
                    self.addr = self.addr[1:-1]
                    self.use_ipv6 = True
                    self._raw_ipv6 = True
                elif self.use_ipv6 and not _fqdn:
                    raise CommandError('"%s" is not a valid IPv6 address.' % self.addr)
        if not self.addr:
            self.addr = self.default_addr_ipv6 if self.use_ipv6 else self.default_addr
            self._raw_ipv6 = self.use_ipv6
        self.run(**options)

    def run(self, **options):
        """Run the server, using the autoreloader if needed."""
        use_reloader = options["use_reloader"]

        if use_reloader:
            autoreload.run_with_reloader(self.inner_run, **options)
        else:
            self.inner_run(None, **options)

    def inner_run(self, *args, **options):
        # If an exception was silenced in ManagementUtility.execute in order
        # to be raised in the child process, raise it now.
        autoreload.raise_last_exception()

        threading = options["use_threading"]
        # 'shutdown_message' is a stealth option.
        shutdown_message = options.get("shutdown_message", "")

        if not options["skip_checks"]:
            self.stdout.write("Performing system checks...\n\n")
            check_kwargs = super().get_check_kwargs(options)
            check_kwargs["display_num_errors"] = True
            self.check(**check_kwargs)
        # Need to check migrations here, so can't use the
        # requires_migrations_check attribute.
        self.check_migrations()
        # Close all connections opened during migration checking.
        for conn in connections.all(initialized_only=True):
            conn.close()

        try:
            handler = self.get_handler(*args, **options)
            run(
                self.addr,
                int(self.port),
                handler,
                ipv6=self.use_ipv6,
                threading=threading,
                on_bind=self.on_bind,
                server_cls=self.server_cls,
            )
        except OSError as e:
            # Use helpful error messages instead of ugly tracebacks.
            ERRORS = {
                errno.EACCES: "You don't have permission to access that port.",
                errno.EADDRINUSE: "That port is already in use.",
                errno.EADDRNOTAVAIL: "That IP address can't be assigned to.",
            }
            try:
                error_text = ERRORS[e.errno]
            except KeyError:
                error_text = e
            self.stderr.write("Error: %s" % error_text)
            # Need to use an OS exit because sys.exit doesn't work in a thread
            os._exit(1)
        except KeyboardInterrupt:
            if shutdown_message:
                self.stdout.write(shutdown_message)
            sys.exit(0)

    def on_bind(self, server_port):
        quit_command = "CTRL-BREAK" if sys.platform == "win32" else "CONTROL-C"

        if self._raw_ipv6:
            addr = f"[{self.addr}]"
        elif self.addr == "0":
            addr = "0.0.0.0"
        else:
            addr = self.addr

        now = datetime.now().strftime("%B %d, %Y - %X")
        version = self.get_version()
        print(
            f"{now}\n"
            f"Django version {version}, using settings {settings.SETTINGS_MODULE!r}\n"
            f"Starting development server at {self.protocol}://{addr}:{server_port}/\n"
            f"Quit the server with {quit_command}.",
            file=self.stdout,
        )
        docs_version = get_docs_version()
        if os.environ.get("DJANGO_RUNSERVER_HIDE_WARNING") != "true":
            self.stdout.write(
                self.style.WARNING(
                    "WARNING: This is a development server. Do not use it in a "
                    "production setting. Use a production WSGI or ASGI server "
                    "instead.\nFor more information on production servers see: "
                    f"https://docs.djangoproject.com/en/{docs_version}/howto/"
                    "deployment/"
                )
            )
