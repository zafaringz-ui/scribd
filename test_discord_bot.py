#!/usr/bin/env python3
"""
Offline checks for discord_bot.py. No Discord, no network, no browser.

    python test_discord_bot.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import discord_bot as db  # noqa: E402

fails = 0
checks = 0


def check(name: str, ok: bool, extra: object = "") -> None:
    global fails, checks
    checks += 1
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   [{extra}]" if not ok else ""))
    if not ok:
        fails += 1


tmp = Path(tempfile.mkdtemp())

# --------------------------------------------------------------------------- #
print("-- token handling --")

_saved_env = {k: os.environ.get(k) for k in ("DISCORD_TOKEN", "BOT_TOKEN")}
_saved_file = db.TOKEN_FILE
db.TOKEN_FILE = tmp / "token"

for key in _saved_env:
    os.environ.pop(key, None)

try:
    db.read_token()
    check("missing token exits with a message", False)
except SystemExit as exc:
    check("missing token exits with a message", "no Discord token" in str(exc), exc)

check("explicit --token wins", db.read_token("abc.def") == "abc.def")
os.environ["DISCORD_TOKEN"] = "  from-env  "
check("env token is stripped", db.read_token() == "from-env", db.read_token())
os.environ["BOT_TOKEN"] = "from-bot-env"
os.environ.pop("DISCORD_TOKEN")
check("BOT_TOKEN is a fallback", db.read_token() == "from-bot-env")
os.environ.pop("BOT_TOKEN")

db.TOKEN_FILE.write_text("  file-token  \n", encoding="utf-8")
check("token file works", db.read_token() == "file-token", db.read_token())
db.TOKEN_FILE.write_text("DISCORD_TOKEN=paste-me\n", encoding="utf-8")
check("token file tolerates KEY=value", db.read_token() == "paste-me", db.read_token())
check("explicit beats the file", db.read_token("mine") == "mine")

for key, val in _saved_env.items():
    if val is not None:
        os.environ[key] = val
db.TOKEN_FILE = _saved_file

# --------------------------------------------------------------------------- #
print("-- .env parsing --")

import envfile  # noqa: E402

values = envfile.parse(
    "\ufeff# a comment\n"
    "\n"
    "DISCORD_TOKEN=MTIz.abc.def\n"
    "export EXPORTED=yes\n"
    'QUOTED="value with spaces"\n'
    "SINGLE='single quoted'\n"
    "TRAILING=abc   # inline note\n"
    "EMPTY=\n"
    "BARE_KEY\n"
    "WITH_EQUALS=a=b=c\n"
    "SPACED = padded \n"
    "DISCORD_TOKEN=last-wins\n"
)
check(
    "plain value",
    values.get("DISCORD_TOKEN") == "last-wins",
    values.get("DISCORD_TOKEN"),
)
check("export prefix stripped", values.get("EXPORTED") == "yes")
check("double quotes stripped", values.get("QUOTED") == "value with spaces")
check("single quotes stripped", values.get("SINGLE") == "single quoted")
check(
    "trailing comment stripped", values.get("TRAILING") == "abc", values.get("TRAILING")
)
check("empty value kept as empty string", values.get("EMPTY") == "")
check("bare key ignored", "BARE_KEY" not in values)
check("equals inside a value kept", values.get("WITH_EQUALS") == "a=b=c")
check(
    "whitespace around key/value trimmed",
    values.get("SPACED") == "padded",
    values.get("SPACED"),
)
check("last assignment wins", values.get("DISCORD_TOKEN") == "last-wins")

crlf = envfile.parse("A=1\r\nB=2\r\n")
check("CRLF files (Windows) parse", crlf == {"A": "1", "B": "2"}, crlf)

check("comment-only file is empty", envfile.parse("# nothing\n\n") == {})
check("no $ expansion", envfile.parse("X=$HOME")["X"] == "$HOME")

# --------------------------------------------------------------------------- #
print("-- .env precedence --")

envdir = tmp / "envtest"
envdir.mkdir()
(envdir / "dot.env").write_text(
    "DISCORD_TOKEN=from-file\nALLOWED_GUILD_IDS=9\n", encoding="utf-8"
)

os.environ.pop("DISCORD_TOKEN", None)
envfile.APPLIED.clear()
envfile._FOUND.clear()
applied = envfile.load(str(envdir / "dot.env"))
check("load applies values", applied.get("DISCORD_TOKEN") == "from-file", applied)
check("load sets the environment", os.environ.get("DISCORD_TOKEN") == "from-file")
check("APPLIED records what the file set", "DISCORD_TOKEN" in envfile.APPLIED)

os.environ["DISCORD_TOKEN"] = "from-env"
os.environ.pop("ALLOWED_GUILD_IDS", None)  # only DISCORD_TOKEN should conflict
envfile.APPLIED.clear()
envfile._FOUND.clear()
applied2 = envfile.load(str(envdir / "dot.env"))
check("real environment wins over .env", os.environ["DISCORD_TOKEN"] == "from-env")
check(
    "conflicting key not reported as applied", "DISCORD_TOKEN" not in applied2, applied2
)
check(
    "non-conflicting key still applied",
    applied2.get("ALLOWED_GUILD_IDS") == "9",
    applied2,
)

try:
    envfile.load(str(tmp / "missing.env"))
    check("explicit missing file raises", False)
except FileNotFoundError:
    check("explicit missing file raises", True)

# no .env anywhere -> no error, nothing applied
_saved_cands = envfile.candidates
envfile.candidates = lambda explicit=None: [tmp / "definitely-absent.env"]
try:
    check("no .env at all is silent", envfile.load() == {})
finally:
    envfile.candidates = _saved_cands

os.environ.pop("DISCORD_TOKEN", None)
os.environ.pop("ALLOWED_GUILD_IDS", None)
envfile.APPLIED.clear()
envfile._FOUND.clear()
envfile.LOADED_FROM = None

# --------------------------------------------------------------------------- #
print("-- token source reporting --")

botenv = tmp / "botenv"
botenv.mkdir()
(botenv / ".env").write_text("DISCORD_TOKEN=dotenv-token\n", encoding="utf-8")
_saved_here = db.HERE
_saved_token_file = db.TOKEN_FILE
db.TOKEN_FILE = botenv / "token"
os.chdir(botenv)
try:
    envfile.APPLIED.clear()
    envfile._FOUND.clear()
    envfile.load()
    db.TOKEN_SOURCE = "unset"
    check("token comes from .env", db.read_token() == "dotenv-token", db.read_token())
    check("source reported as .env", db.TOKEN_SOURCE == ".env", db.TOKEN_SOURCE)

    os.environ["DISCORD_TOKEN"] = "env-token"
    envfile.APPLIED.clear()
    db.TOKEN_SOURCE = "unset"
    check("real env beats .env", db.read_token() == "env-token")
    check("source reported as environment", db.TOKEN_SOURCE == "environment variable")
    os.environ.pop("DISCORD_TOKEN")

    db.TOKEN_SOURCE = "unset"
    check("--token beats everything", db.read_token("cli-token") == "cli-token")
    check("source reported as --token", db.TOKEN_SOURCE == "--token")
finally:
    os.chdir(Path(_saved_here))
    db.HERE = _saved_here
    db.TOKEN_FILE = _saved_token_file
    envfile.APPLIED.clear()
    envfile._FOUND.clear()
    envfile.LOADED_FROM = None
    for _k in ("DISCORD_TOKEN", "ALLOWED_GUILD_IDS"):
        os.environ.pop(_k, None)

# --------------------------------------------------------------------------- #
print("-- guild allowlist --")

os.environ.pop("ALLOWED_GUILD_IDS", None)
os.environ.pop("ALLOWED_GUILDS", None)
check("no allowlist -> empty set (anyone)", db.allowed_guilds() == set())
os.environ["ALLOWED_GUILD_IDS"] = "123, 456  789"
check(
    "comma and space separated",
    db.allowed_guilds() == {123, 456, 789},
    db.allowed_guilds(),
)
os.environ["ALLOWED_GUILD_IDS"] = "abc, 42"
check("junk is ignored", db.allowed_guilds() == {42}, db.allowed_guilds())
os.environ.pop("ALLOWED_GUILD_IDS")

# --------------------------------------------------------------------------- #
print("-- formatting --")

check("KB formatting", db.human_mb(400 * 1024) == "400 KB", db.human_mb(400 * 1024))
check(
    "MB formatting",
    db.human_mb(9 * 1024 * 1024) == "9.0 MB",
    db.human_mb(9 * 1024 * 1024),
)
check("unknown size", db.human_mb(None) == "?")

LIMIT = 8 * 1024 * 1024
check("7 MB fits", db.fits_upload(7 * 1024 * 1024, LIMIT))
check("8 MB fits exactly", db.fits_upload(8 * 1024 * 1024, LIMIT))
check("9 MB does not fit", not db.fits_upload(9 * 1024 * 1024, LIMIT))
check("no size does not fit", not db.fits_upload(None, LIMIT))

items = [
    {"id": "1", "title": "A" * 200, "pages": 12, "views": "3K", "url": "https://s/1"},
    {"id": "2", "title": "Second", "url": "https://s/2"},
]
text = db.result_lines(items)
check("result line has a link", "https://s/1" in text)
check("result line has the id", "`1`" in text)
check("long titles are truncated", "A" * 200 not in text, text[:80])
check("page count shown", "12 pp" in text)
check("missing metadata does not crash", "Second" in text)
check("count is limited", db.result_lines(items * 5, limit=5).count("**") == 10)
check("empty results", db.result_lines([]) == "no results")

# --------------------------------------------------------------------------- #
print("-- pages argument (it reaches a command line) --")

check("plain range", db.clean_pages("1-20") == "1-20")
check("list and range", db.clean_pages(" 3,7,9-12 ") == "3,7,9-12")
check("empty stays empty", db.clean_pages("") == "")
check("None stays empty", db.clean_pages(None) == "")
for bad in ("1-20; rm -rf /", "$(whoami)", "1..20", "all", "1-20 && curl evil"):
    try:
        db.clean_pages(bad)
        check(f"rejects {bad!r}", False)
    except ValueError:
        check(f"rejects {bad!r}", True)

# --------------------------------------------------------------------------- #
print("-- misc --")

check(
    "find_tool finds the bundle",
    db.find_tool().name in ("scribd_all.py", "scribd_shot.py"),
)
check("pid_alive on ourselves", db.pid_alive(os.getpid()))
check("pid_alive on a dead pid", not db.pid_alive(999_999))

# --------------------------------------------------------------------------- #
print("-- command tree (needs discord.py) --")

try:
    import discord  # noqa: F401

    bot = db.ScribdBot("fake.token.value", guild=12345, outdir=tmp)
    names = sorted(c.name for c in bot.tree.get_commands())
    check(
        "three slash commands registered", names == ["pdf", "scribd", "status"], names
    )
    check(
        "no privileged message_content intent", bot.bot.intents.message_content is False
    )
    check("guilds intent on (needed for guild sync)", bot.bot.intents.guilds is True)
    check("no members intent", bot.bot.intents.members is False)
    check("outdir honoured", bot.outdir == tmp)
    check("tool resolves", bot._tool().exists())

    # _last_pdf prefers the CLI's line, then the newest file
    (tmp / "older.pdf").write_bytes(b"%PDF")
    import time as _t

    _t.sleep(0.01)
    (tmp / "newer.pdf").write_bytes(b"%PDF")
    check("falls back to the newest pdf", bot._last_pdf("").name == "newer.pdf")
    check(
        "uses the CLI's reported path when present",
        bot._last_pdf(f"  done in 1.0s -> {tmp / 'older.pdf'}").name == "older.pdf",
    )
except ImportError:
    check("discord.py not installed - tree checks skipped", True)

# --------------------------------------------------------------------------- #
print("-- with-web wiring --")

_web_cmd = []
_orig_popen = db.subprocess.Popen


class _FakeProc:
    stdout = None

    def poll(self):
        return 0

    def terminate(self):
        pass


def _fake_popen(cmd, **kwargs):
    _web_cmd.append(cmd)
    return _FakeProc()


db.subprocess.Popen = _fake_popen
try:
    db.start_web(25899)
finally:
    db.subprocess.Popen = _orig_popen

check("web child is launched", bool(_web_cmd))
check("web command uses --port", "--port" in _web_cmd[0] and "25899" in _web_cmd[0])
check("web command sets a token", "--token" in _web_cmd[0])
check("web command points at scribd_web.py", "scribd_web.py" in " ".join(_web_cmd[0]))
check("state records the port", db.WEB["port"] == 25899)

# --------------------------------------------------------------------------- #
print("-- setup docs mention the bot --")

readme = (Path(__file__).resolve().parent / "README.md").read_text(encoding="utf-8")
check("README documents the bot", "discord_bot.py" in readme)
check("README warns about the token", "DISCORD_TOKEN" in readme)
check("README documents .env", ".env" in readme)

example = Path(__file__).resolve().parent / ".env.example"
check(".env.example exists", example.is_file())
check(
    ".env.example lists DISCORD_TOKEN",
    "DISCORD_TOKEN=" in example.read_text(encoding="utf-8"),
)
gitignore = (Path(__file__).resolve().parent / ".gitignore").read_text(encoding="utf-8")
check(".env is gitignored", "\n.env\n" in "\n" + gitignore)

import shutil  # noqa: E402

shutil.rmtree(tmp, ignore_errors=True)

print()
print(f"{checks} checks")
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
