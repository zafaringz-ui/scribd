#!/usr/bin/env python3
"""
discord_bot.py — a very small Discord bot that sits next to the web UI.

Two reasons it exists:

1. Hosts that sell "Discord bot hosting" suspend containers that are not actually
   running a bot. Keeping a real gateway connection alive is what makes the
   server look like what it is billed as.
2. It is a convenient second front end: search and grab a PDF from Discord
   without opening the web page.

It is deliberately tiny: slash commands only (so no privileged "message content"
intent, no approval from Discord), one browser at a time, and everything heavy is
done by the existing tool.

    export DISCORD_TOKEN=...            # or put the token in .discord-token
    python discord_bot.py               # bot only
    python discord_bot.py --with-web    # bot + the web UI in one process tree
    python discord_bot.py --guild 123   # sync commands to one guild instantly

Commands
    /scribd <query>            search, returns the top results as buttons
    /pdf <id> [pages] [engine] download one document and upload the PDF
    /status                    browser, queue and disk state

Note: this does nothing about Scribd's per-IP rate limiting - a Discord
connection is not a proxy. It exists so the *host* keeps the server online.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOKEN_FILE = HERE / ".discord-token"
WEB_PORT_DEFAULT = 8080

# test mode: no gateway connection, just check the wiring
DRY = False


def _log(msg: str) -> None:
    print(f"[bot] {msg}", flush=True)


TOKEN_SOURCE = "unset"


def _dotenv_applied() -> dict:
    try:
        import envfile

        return envfile.APPLIED
    except Exception:  # noqa: BLE001
        return {}


def _from_dotenv(key: str) -> str:
    """Value from .env only - not from the real environment."""
    try:
        import envfile

        return (envfile._FOUND.get(key) or "").strip()  # noqa: SLF001
    except Exception:  # noqa: BLE001
        return ""


def read_token(explicit: str | None = None) -> str:
    """Token from --token, the real environment, ./.env, or ./.discord-token.

    Precedence matters: a panel sets real environment variables, and those must
    win over a stale .env file left in the container.
    """
    global TOKEN_SOURCE
    if explicit:
        TOKEN_SOURCE = "--token"
        return explicit.strip()

    env = os.environ.get("DISCORD_TOKEN") or os.environ.get("BOT_TOKEN")
    if env and env.strip():
        served_by_file = any(
            key in _dotenv_applied() for key in ("DISCORD_TOKEN", "BOT_TOKEN")
        )
        TOKEN_SOURCE = ".env" if served_by_file else "environment variable"
        return env.strip()

    for key in ("DISCORD_TOKEN", "BOT_TOKEN"):
        value = _from_dotenv(key)
        if value:
            TOKEN_SOURCE = ".env"
            return value

    if TOKEN_FILE.exists():
        text = TOKEN_FILE.read_text(encoding="utf-8-sig").strip()
        if "=" in text and "\n" not in text:
            text = text.split("=", 1)[1].strip()
        if text:
            TOKEN_SOURCE = TOKEN_FILE.name
            return text

    raise SystemExit(
        "no Discord token.\n"
        "  create a .env file next to this script:\n"
        "\n"
        "      DISCORD_TOKEN=your-token-here\n"
        "\n"
        "  (a real environment variable also works and takes precedence;\n"
        f"   the legacy file {TOKEN_FILE.name} is still read last)\n"
        "  never commit the token."
    )


def allowed_guilds() -> set[int]:
    raw = os.environ.get("ALLOWED_GUILD_IDS") or os.environ.get("ALLOWED_GUILDS") or ""
    if not raw:
        try:
            import envfile

            raw = (
                envfile.get("ALLOWED_GUILD_IDS") or envfile.get("ALLOWED_GUILDS") or ""
            )
        except Exception:  # noqa: BLE001
            raw = ""
    out: set[int] = set()
    for part in re.split(r"[,\s]+", raw.strip()):
        if part.isdigit():
            out.add(int(part))
    return out


# --------------------------------------------------------------------------- #
# formatting helpers (pure functions - tested offline)
# --------------------------------------------------------------------------- #


def human_mb(size: int | None) -> str:
    if not size:
        return "?"
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"
    return f"{size / 1024:.0f} KB"


def fits_upload(size: int | None, limit: int = 8 * 1024 * 1024) -> bool:
    """Discord's attachment limit is 8 MB (25 MB on boosted servers)."""
    return bool(size) and size <= limit


def result_lines(items: list[dict], limit: int = 5) -> str:
    """Turn scribd_web search results into one compact markdown block."""
    if not items:
        return "no results"
    lines = []
    for i, it in enumerate(items[:limit], 1):
        title = (it.get("title") or it.get("id") or "?")[:70]
        bits = []
        if it.get("pages"):
            bits.append(f"{it['pages']} pp")
        if it.get("views"):
            bits.append(f"{it['views']} views")
        meta = f" · {' · '.join(bits)}" if bits else ""
        lines.append(f"**{i}.** [{title}]({it.get('url', '')}) `{it.get('id')}`{meta}")
    return "\n".join(lines)


def clean_pages(arg: str | None) -> str:
    """Accept '1-20', '1,3,7-9'; reject anything else (it goes into a CLI)."""
    if not arg:
        return ""
    arg = arg.strip()
    if not re.fullmatch(r"[\d,\-\s]+", arg):
        raise ValueError("pages must look like 1-20 or 3,7,9-12")
    return arg


def find_tool() -> Path:
    for name in ("scribd_all.py", "scribd_shot.py"):
        if (HERE / name).exists():
            return HERE / name
    raise SystemExit("scribd_all.py / scribd_shot.py not found next to the bot")


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------- #
# the bot
# --------------------------------------------------------------------------- #


class ScribdBot:
    """Wraps discord.py so the import stays optional."""

    def __init__(
        self, token: str, *, guild: int | None = None, outdir: Path | None = None
    ):
        import discord

        self.discord = discord
        intents = discord.Intents.none()  # no privileged intents at all
        intents.guilds = True
        self.bot = discord.Client(intents=intents)
        self.tree = discord.app_commands.CommandTree(self.bot)
        self.token = token
        self.guild = guild
        self.outdir = outdir or (HERE / "output")
        self.started = time.time()
        self._register()

    # -- registration --------------------------------------------------- #
    def _register(self) -> None:
        discord = self.discord
        tree = self.tree

        @tree.command(name="scribd", description="search scribd")
        @discord.app_commands.describe(query="what to search for")
        async def scribd(interaction, query: str):
            await self._cmd_search(interaction, query)

        @tree.command(name="pdf", description="download a document as a PDF")
        @discord.app_commands.describe(
            doc="document id or scribd url",
            pages="optional range, e.g. 1-20",
            engine="auto (live reader only) or fragment",
        )
        async def pdf(interaction, doc: str, pages: str = "", engine: str = "auto"):
            await self._cmd_pdf(interaction, doc, pages, engine)

        @tree.command(name="status", description="is the converter healthy?")
        async def status(interaction):
            await self._cmd_status(interaction)

        @self.bot.event
        async def on_ready():
            _log(f"logged in as {self.bot.user} ({(self.bot.user.id)})")
            target = discord.Object(id=self.guild) if self.guild else None
            try:
                if target:
                    self.tree.copy_global_to(guild=target)
                    synced = await self.tree.sync(guild=target)
                else:
                    synced = await self.tree.sync()
                _log(f"{len(synced)} command(s) synced")
            except Exception as exc:  # noqa: BLE001
                _log(f"command sync failed: {exc}")
            await self.bot.change_presence(
                activity=discord.Game(name="scribd → pdf | /scribd")
            )
            _log("ready")

    # -- guards --------------------------------------------------------- #
    async def _guard(self, interaction) -> bool:
        allowed = allowed_guilds()
        if allowed and interaction.guild_id not in allowed:
            await interaction.response.send_message(
                "this bot is not enabled on this server", ephemeral=True
            )
            return False
        return True

    # -- commands ------------------------------------------------------- #
    async def _cmd_search(self, interaction, query: str) -> None:
        if not await self._guard(interaction):
            return
        await interaction.response.defer(thinking=True)
        discord = self.discord
        try:
            import scribd_web

            data = await asyncio.to_thread(scribd_web.scribd_search, query)
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(f"search failed: {exc}", ephemeral=True)
            return

        items = data.get("items") or []
        embed = discord.Embed(
            title=f"results for “{query}”",
            description=result_lines(items),
            colour=0x4DA3FF,
        )
        if not items:
            embed.description = "nothing found"
        embed.set_footer(text="use /pdf <id> to convert one")
        await interaction.followup.send(embed=embed)

    async def _cmd_pdf(self, interaction, doc: str, pages: str, engine: str) -> None:
        if not await self._guard(interaction):
            return
        try:
            pages = clean_pages(pages)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        # the fragment engine is the only reliable renderer; anything else falls
        # back to it so a stray argument cannot produce a broken PDF
        engine = engine if engine in ("fragment", "auto") else "fragment"

        await interaction.response.defer(thinking=True)
        cmd = [
            sys.executable,
            "-u",
            str(self._tool()),
            doc,
            "-o",
            str(self.outdir),
            "--engine",
            engine,
            "--no-offload",  # we attach it to Discord first, then offload
        ]
        if pages:
            cmd += ["--pages", pages]
        code, out = await asyncio.to_thread(self._run, cmd)
        pdf = self._last_pdf(out)

        if code != 0 or pdf is None:
            tail = "\n".join(out.strip().splitlines()[-6:])
            await interaction.followup.send(f"failed:\n```\n{tail[:1500]}\n```")
            return

        size = pdf.stat().st_size
        if fits_upload(size):
            await interaction.followup.send(
                f"`{pdf.name}` · {human_mb(size)}",
                file=self.discord.File(str(pdf)),
            )
        else:
            await interaction.followup.send(
                f"`{pdf.name}` · {human_mb(size)} — too big for a Discord upload "
                f"(8 MB limit).\nGrab it from the web UI, or use `/pdf {doc} "
                f"pages:1-20` to split it."
            )
        await asyncio.to_thread(self._offload, pdf)

    async def _cmd_status(self, interaction) -> None:
        if not await self._guard(interaction):
            return
        await interaction.response.defer(thinking=True)
        try:
            import chromium_launch

            browser = chromium_launch.describe().replace("chromium: ", "")
        except Exception as exc:  # noqa: BLE001
            browser = f"unknown ({exc})"
        ups = int(time.time() - self.started)
        pdfs = len(list(self.outdir.glob("*.pdf"))) if self.outdir.exists() else 0
        await interaction.followup.send(
            f"**browser** {browser}\n"
            f"**uptime** {ups // 3600}h {(ups % 3600) // 60}m\n"
            f"**pdfs on disk** {pdfs}\n"
            f"**web ui** {'running' if WEB.get('proc') else 'not started here'}"
        )

    # -- helpers -------------------------------------------------------- #
    def _tool(self) -> Path:
        return find_tool()

    def _run(self, cmd: list[str]) -> tuple[int, str]:
        proc = subprocess.run(  # noqa: S603
            cmd, cwd=str(HERE), capture_output=True, text=True, timeout=1800
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    def _offload(self, pdf: Path) -> None:
        """Free the panel's disk once the PDF has been delivered."""
        try:
            import drime
        except Exception:  # noqa: BLE001 - optional module
            return
        entry = drime.offload(pdf, log=_log)
        if entry:
            _log(f"offloaded {pdf.name} -> drime id {entry.get('id')}")

    def _last_pdf(self, out: str) -> Path | None:
        found = re.findall(r"-> (\S.*\.pdf)\s*$", out, re.M)
        if found:
            path = Path(found[-1])
            if not path.is_absolute():
                path = HERE / path
            if path.exists():
                return path
        if self.outdir.exists():
            pdfs = sorted(self.outdir.glob("*.pdf"), key=lambda p: p.stat().st_mtime)
            return pdfs[-1] if pdfs else None
        return None

    def run(self) -> None:
        discord = self.discord
        try:
            self.bot.run(self.token, log_handler=None)
        except discord.LoginFailure:
            raise SystemExit(
                "Discord rejected the token.\n"
                "  - copy it again from the Developer Portal > Bot > Reset Token\n"
                "  - no quotes, no 'Bot ' prefix, no spaces around it"
            ) from None
        except discord.PrivilegedIntentsRequired:
            raise SystemExit(
                "this bot asks for a privileged intent; it should not.\n"
                "  the code uses Intents.none() - check you downloaded the latest "
                "discord_bot.py"
            ) from None
        except Exception as exc:  # network, DNS, rate limit on login
            raise SystemExit(f"could not connect to Discord: {exc}") from None


# --------------------------------------------------------------------------- #
# optional: run the web UI in the same process tree
# --------------------------------------------------------------------------- #

WEB: dict = {"proc": None, "port": None}


def start_web(port: int, token: str = "auto", extra: list[str] | None = None):
    """Start scribd_web.py as a child process so one panel command runs both."""
    cmd = [
        sys.executable,
        "-u",
        str(HERE / "scribd_web.py"),
        "--port",
        str(port),
        "--token",
        token,
    ] + (extra or [])
    _log(f"starting web UI on :{port}")
    proc = subprocess.Popen(  # noqa: S603
        cmd,
        cwd=str(HERE),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    WEB["proc"] = proc
    WEB["port"] = port

    def pump() -> None:
        if proc.stdout is None:  # nothing to forward (should not happen in practice)
            return
        for line in proc.stdout:
            print(f"[web] {line.rstrip()}", flush=True)

    threading.Thread(target=pump, daemon=True, name="web-log").start()
    return proc


def main(argv: list[str] | None = None) -> int:
    global WEB_PORT_DEFAULT
    ap = argparse.ArgumentParser(
        prog="discord_bot.py",
        description="Tiny Discord bot for the scribd converter.",
    )
    ap.add_argument("--token", default=None, help="bot token (else $DISCORD_TOKEN)")
    ap.add_argument(
        "--guild", type=int, default=None, help="sync commands to one guild"
    )
    ap.add_argument("--outdir", default=None, help="where PDFs are written")
    ap.add_argument("--with-web", action="store_true", help="also run the web UI")
    ap.add_argument("--web-port", type=int, default=None, help="web UI port")
    ap.add_argument("--check", action="store_true", help="validate setup and exit")
    args = ap.parse_args(argv)

    # .env first: it may set SCRIBD_CHROMIUM, ALLOWED_GUILD_IDS, SERVER_PORT...
    try:
        import envfile

        applied = envfile.load()
        if applied:
            keys = ", ".join(sorted(applied))
            _log(f"loaded {envfile.LOADED_FROM.name} ({keys})")
    except Exception as exc:  # noqa: BLE001
        _log(f"could not read .env: {exc}")

    token = read_token(args.token)
    if args.check:
        _log(f"token looks ok ({len(token)} chars, ...{token[-4:]})")
        _log(f"token source: {TOKEN_SOURCE}")
        if TOKEN_SOURCE != ".env" and (HERE / ".env").is_file():
            _log("note: .env exists but the environment variable took precedence")
        _log(f"tool: {find_tool().name}")
        _log(f"allowed guilds: {allowed_guilds() or 'any'}")
        return 0

    if args.with_web:
        port = args.web_port or int(os.environ.get("SERVER_PORT") or WEB_PORT_DEFAULT)
        start_web(port)

    bot = ScribdBot(
        token, guild=args.guild, outdir=Path(args.outdir) if args.outdir else None
    )
    try:
        bot.run()
    except KeyboardInterrupt:
        _log("stopping")
    finally:
        proc = WEB.get("proc")
        if proc and proc.poll() is None:
            proc.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
