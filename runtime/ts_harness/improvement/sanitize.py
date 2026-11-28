"""Keep client data out of what the harness files against its own repository.

The harness repository must never receive client data: no client repository names, ticket keys,
code, ticket or work order text, design content or customer names. Every text that goes into a
harness self-issue (title, signature, diagnosis, stack, component) passes through here first.

Rules, in order (:func:`sanitize_text`):

1. Code goes: fenced blocks, diff lines, quoted (``>``) lines and lines that look like source
   code are dropped; a Python traceback keeps only its ``File "...", line N, in fn`` frames.
2. URLs go: a GitHub URL of a known client repository becomes its role (``<web repo>``), any
   other GitHub URL ``<repo>``, any other URL ``<url>``; e-mail addresses become ``<email>``.
3. Known client identifiers are replaced: repository slugs and names by their role
   (``<api repo>``), ticket keys and work order ids by ``<ticket>``, project keys, project names
   and customer names by ``<name>``.
4. Anything shaped like a ticket key (``ABC-123``) becomes ``<ticket>``; ``owner/repo`` becomes
   ``<repo>``.
5. Paths: a harness path (``runtime/ts_harness/...``, or a path that exists in the harness
   checkout under ``scripts/``, ``config/``, ``skills/``, ``database/``, ``admin-ui/``,
   ``docs/``) is kept, repo-relative; every other path becomes ``<path>``.
6. Quoted text goes: a quoted string is kept only when it is one identifier (``'pr_url'``) or a
   harness path; otherwise it becomes ``'<text>'``.
7. Distinctive words from the client texts we know about (the work order, the ticket: CamelCase
   words, capitalised words that are not the first of a sentence, ALLCAPS words) become ``<name>``.

Then :func:`leaks` checks the result. :func:`safe_field` withholds a field entirely (a fixed
note pointing at the internal references) when anything known to be client data is still in it,
or when it shares a run of five words with a client text. When in doubt, leave it out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

WITHHELD = "(withheld: may contain client data; see the internal references)"
HARNESS_PATH_ROOTS = ("scripts/", "config/", "skills/", "database/", "admin-ui/", "docs/")
OVERLAP_WORDS = 5
CODE_REMOVED = "<code removed>"
TEXT_REMOVED = "<text removed>"

_FENCE = re.compile(r"```.*?(```|$)", re.S)
_URL = re.compile(r"\b(?:https?|ssh|git)://[^\s<>\"'`)\]]+|\bgit@[\w.-]+:[\w./-]+", re.I)
_GITHUB = re.compile(r"(?:https?://|ssh://git@|git@)(?:www\.)?github\.com[:/]([\w.-]+)/([\w.-]+)", re.I)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_TICKET = re.compile(r"\b[A-Z][A-Z0-9_]{1,9}-\d+\b")
_PATHLIKE = re.compile(r"(?<![\w<])(?:[A-Za-z]:)?/?(?:[\w.@-]+/)+[\w.@-]+")
_QUOTED = re.compile(r"(['\"`])([^'\"`\n]{1,300}?)\1")
_IDENT = re.compile(r"^[A-Za-z_][\w.:-]{0,60}$")
_FRAME = re.compile(r'^\s*File "([^"]+)", line (\d+), in ([\w<>.]+)\s*$')
_EXC_LINE = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning|Timeout|Conflict|Exhausted))\b:?\s?(.*)$")
_CODE_HINTS = re.compile(
    r"(^\s*(def |class |import |from \S+ import|function |const |let |var |export |return |if \(|for \(|<\w+[ >/]|</\w+>|#include|public |private |@\w+\()"
    r"|[;{}]\s*$|=>|\)\s*\{)"
)
_PLACEHOLDER = re.compile(r"<[a-z][a-z ]{0,20}>")
_WORD = re.compile(r"[A-Za-z][\w'-]*")
_COMMON = frozenset(
    """
    a an and are as at be but by can cannot could did do does done for from had has have i if in into is it its
    may might must no not of on or our should so that the their them then there these they this those to was we
    were what when where which while who will with would you your yes ok please also only just all any each
    error failed failure exception traceback warning step task job workflow work order ticket repository repo
    branch commit pull request pr review code coding test tests qa ci check checks harness agent arbiter run
    runs running retry revise stuck blocked waiting timeout lost missing access secret token config configuration
    github jira slack python json api web http https file files path line none null true false the a
    monday tuesday wednesday thursday friday saturday sunday january february march april june july august
    september october november december utc
    """.split()
)


@dataclass
class ClientContext:
    """What we know is client data, gathered by the caller.

    ``repos``: client repository slug (``owner/name``, any case) to role (``web``, ``api``; empty for
    unknown). ``names``: project keys, project names, customer and team names. ``tickets``: ticket
    keys and work order ids. ``texts``: client texts (work order body, ticket title and text), used
    to recognise their distinctive words and any run of words copied from them. ``harness_repo``:
    the harness repository's own slug, which may be named.
    """

    repos: dict[str, str] = field(default_factory=dict)
    names: set[str] = field(default_factory=set)
    tickets: set[str] = field(default_factory=set)
    texts: list[str] = field(default_factory=list)
    harness_repo: str = ""

    def merged(self, other: "ClientContext") -> "ClientContext":
        return ClientContext(
            repos={**other.repos, **{slug: role for slug, role in self.repos.items() if role or slug not in other.repos}},
            names=set(self.names) | set(other.names),
            tickets=set(self.tickets) | set(other.tickets),
            texts=list(self.texts) + list(other.texts),
            harness_repo=self.harness_repo or other.harness_repo,
        )

    def add_repo(self, slug: Any, role: Any = "") -> None:
        text = str(slug or "").strip()
        match = _GITHUB.search(text)
        if match:
            text = f"{match.group(1)}/{match.group(2)}"
        text = text.removesuffix(".git").strip("/")
        if text.count("/") == 1 and text.lower() != self.harness_repo.lower():
            self.repos[text.lower()] = str(role or "").strip().lower()

    def add_names(self, values: Iterable[Any]) -> None:
        for value in values:
            text = str(value or "").strip()
            if len(text) >= 2:
                self.names.add(text)

    def add_tickets(self, values: Iterable[Any]) -> None:
        for value in values:
            text = str(value or "").strip()
            if len(text) >= 2:
                self.tickets.add(text)


class _Vault:
    """Replacement labels kept out of reach of the later rules (a label like ``<web repo>`` must not
    itself be rewritten by the rule for a repository named ``web``)."""

    def __init__(self) -> None:
        self.labels: list[str] = []

    def put(self, label: str) -> str:
        self.labels.append(label)
        return f"\x01{len(self.labels) - 1}\x02"

    def restore(self, text: str) -> str:
        return re.sub(r"\x01(\d+)\x02", lambda m: self.labels[int(m.group(1))], text)


def _role_label(role: str) -> str:
    role = re.sub(r"[^a-z ]", "", (role or "").lower()).strip()
    return f"<{role} repo>" if role else "<client repo>"


@lru_cache(maxsize=1)
def _harness_root() -> Path | None:
    """The harness checkout (the directory holding ``runtime/ts_harness``), when it is on disk."""
    package = Path(__file__).resolve().parents[1]  # .../runtime/ts_harness
    root = package.parents[1]
    return root if (root / "runtime" / "ts_harness").is_dir() else None


def harness_path(raw: str) -> str:
    """``raw`` as a repo-relative harness path, or ``""`` when it is not one of the harness's files.

    Only files that exist in the harness itself count: ``.../ts_harness/<rest>`` that exists in the
    installed package, or a path under ``scripts/``, ``config/``, ``skills/``, ``database/``,
    ``admin-ui/`` or ``docs/`` that exists in the harness checkout.
    """
    text = str(raw or "").strip().strip("'\"`").replace("\\", "/")
    text = re.sub(r":\d+(?::\d+)?$", "", text)
    if ".." in text:
        return ""
    marker = text.find("ts_harness/")
    if marker >= 0 and (marker == 0 or text[marker - 1] == "/"):
        rest = text[marker:]
        package_parent = Path(__file__).resolve().parents[2]  # the directory holding ts_harness/
        if re.fullmatch(r"[\w./-]+", rest) and (package_parent / rest).exists():
            return f"runtime/{rest}"
        return ""
    base = _harness_root()
    if base is None:
        return ""
    for root in HARNESS_PATH_ROOTS:
        index = text.find(root)
        if index < 0 or (index > 0 and text[index - 1] != "/"):
            continue
        candidate = text[index:]
        if re.fullmatch(r"[\w./@-]+", candidate) and (base / candidate).exists():
            return candidate
    return ""


def harness_paths(text: str) -> list[str]:
    """Harness files named in ``text`` (traceback frames and paths), in order, without repeats."""
    found: list[str] = []
    for line in str(text or "").splitlines():
        frame = _FRAME.match(line)
        candidates = [frame.group(1)] if frame else _PATHLIKE.findall(line)
        for candidate in candidates:
            path = harness_path(candidate)
            if path and path not in found:
                found.append(path)
    return found


def harness_stack(text: str, ctx: ClientContext | None = None, *, max_frames: int = 12) -> str:
    """The harness frames of the last Python traceback in ``text`` and its exception type.

    Frames outside the harness collapse into one ``(frames outside the harness)`` line; source
    lines are dropped; the exception message is sanitised.
    """
    lines = str(text or "").splitlines()
    start = max((i for i, line in enumerate(lines) if line.strip().startswith("Traceback (most recent call last)")),
                default=-1)
    if start < 0:
        return ""
    out: list[str] = []
    exc_line = ""
    for line in lines[start + 1:]:
        frame = _FRAME.match(line)
        if frame:
            path = harness_path(frame.group(1))
            entry = f"{path}:{frame.group(2)} in {frame.group(3)}" if path else "(frames outside the harness)"
            if not out or out[-1] != entry:
                out.append(entry)
            continue
        match = _EXC_LINE.match(line.strip())
        if match and not line.startswith((" ", "\t")):
            message = sanitize_text(match.group(2), ctx or ClientContext(), limit=300) if match.group(2) else ""
            exc_line = f"{match.group(1).rsplit('.', 1)[-1]}: {message}".rstrip(": ")
            break
    frames = out[-max_frames:]
    if exc_line:
        frames.append(exc_line)
    return "\n".join(frames)


def exception_line(text: str) -> tuple[str, str]:
    """(exception type, raw message) of the last ``SomeError: message`` line, or ("", "")."""
    for line in reversed(str(text or "").splitlines()):
        match = _EXC_LINE.match(line.strip())
        if match:
            return match.group(1).rsplit(".", 1)[-1], match.group(2)
    return "", ""


def _looks_like_code(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if _FRAME.match(line):
        return False
    if stripped.startswith(("+", "-", "@@", ">", "|")):
        return True  # diff, quote and table lines (a bullet list goes too: when in doubt, leave it out)
    if stripped.startswith(("diff --git", "index ", "--- a/", "+++ b/")):
        return True
    return bool(_CODE_HINTS.search(line))


def _drop_code(text: str, vault: _Vault) -> str:
    token = vault.put(CODE_REMOVED)
    text = _FENCE.sub(lambda _m: f" {token} ", text)
    kept: list[str] = []
    in_traceback = False
    for line in text.splitlines():
        if line.strip().startswith("Traceback (most recent call last)"):
            in_traceback = True
            kept.append(line.strip())
            continue
        if in_traceback:
            if _FRAME.match(line):
                kept.append(line.strip())
                continue
            if line.startswith((" ", "\t")):
                continue  # the frame's source line
            in_traceback = False
        if _looks_like_code(line):
            if not kept or kept[-1] != token:
                kept.append(token)
            continue
        kept.append(line)
    return "\n".join(kept)


def _replace_urls(text: str, ctx: ClientContext, vault: _Vault) -> str:
    def github(match: re.Match[str]) -> str:
        slug = f"{match.group(1)}/{match.group(2).removesuffix('.git')}".lower()
        if ctx.harness_repo and slug == ctx.harness_repo.lower():
            return vault.put("<harness repo>")
        return vault.put(_role_label(ctx.repos[slug]) if slug in ctx.repos else "<repo>")

    def url(match: re.Match[str]) -> str:
        value = match.group(0)
        hit = _GITHUB.match(value)
        return github(hit) if hit else vault.put("<url>")

    return _EMAIL.sub(lambda _m: vault.put("<email>"), _URL.sub(url, text))


def _sub_ci(text: str, needle: str, replacement: str) -> str:
    if not needle:
        return text
    pattern = rf"(?<![\w-]){re.escape(needle)}(?![\w-])"
    return re.sub(pattern, lambda _m: replacement, text, flags=re.I)


def _replace_known(text: str, ctx: ClientContext, vault: _Vault) -> str:
    for slug, role in sorted(ctx.repos.items(), key=lambda item: -len(item[0])):
        label = vault.put(_role_label(role))
        text = _sub_ci(text, slug, label)
        name = slug.split("/", 1)[1]
        owner = slug.split("/", 1)[0]
        if len(name) >= 3:
            text = _sub_ci(text, name, label)
        if len(owner) >= 3 and (not ctx.harness_repo or owner != ctx.harness_repo.split("/", 1)[0].lower()):
            text = _sub_ci(text, owner, vault.put("<name>"))
    for ticket in sorted(ctx.tickets, key=len, reverse=True):
        # Also inside a branch name or slug (``wo/proj-42-fix``).
        text = re.sub(rf"(?<![A-Za-z0-9]){re.escape(ticket)}(?![0-9])", lambda _m: vault.put("<ticket>"), text, flags=re.I)
    for key in sorted(_project_keys(ctx), key=len, reverse=True):
        # Every ticket of a known project, in any case.
        text = re.sub(rf"(?<![A-Za-z0-9]){re.escape(key)}-\d+", lambda _m: vault.put("<ticket>"), text, flags=re.I)
    for name in sorted(_name_words(ctx), key=len, reverse=True):
        text = _sub_ci(text, name, vault.put("<name>"))
    return text


def _name_words(ctx: ClientContext) -> set[str]:
    """Known names, and each distinctive word of a name of several words (``Acme`` of ``Acme Shop``)."""
    words = set(ctx.names)
    for name in ctx.names:
        parts = _WORD.findall(name)
        if len(parts) > 1:
            words.update(p for p in parts if len(p) >= 4 and p.lower() not in _COMMON)
    return words


def _drop_copied(text: str, sources: list[str]) -> str:
    """Lines that share a run of :data:`OVERLAP_WORDS` words with a client text are removed."""
    if not sources:
        return text
    kept = []
    for line in text.splitlines():
        kept.append(TEXT_REMOVED if _shared_run(line, sources) else line)
    return "\n".join(kept)


def _project_keys(ctx: ClientContext) -> set[str]:
    return {name for name in ctx.names if re.fullmatch(r"[A-Z][A-Z0-9_]{1,9}", name)}


def _replace_paths(text: str, ctx: ClientContext, vault: _Vault) -> str:
    def path(match: re.Match[str]) -> str:
        value = match.group(0)
        kept = harness_path(value)
        if kept:
            return vault.put(kept)
        if ctx.harness_repo and value.lower() == ctx.harness_repo.lower():
            return vault.put("<harness repo>")
        bare = value.strip("/")
        if bare.count("/") == 1 and "." not in bare.split("/", 1)[1] and not value.startswith("/"):
            return vault.put("<repo>")
        return vault.put("<path>")

    return _PATHLIKE.sub(path, text)


def _replace_quoted(text: str, vault: _Vault) -> str:
    def quoted(match: re.Match[str]) -> str:
        quote, inner = match.group(1), match.group(2)
        if _IDENT.match(inner) and inner.lower() not in _COMMON or harness_path(inner):
            return match.group(0)
        return f"{quote}{vault.put('<text>')}{quote}"

    return _QUOTED.sub(quoted, text)


def distinctive_words(texts: Iterable[str]) -> set[str]:
    """Words in client texts that are likely names: CamelCase, ALLCAPS (3+), or capitalised mid-sentence."""
    found: set[str] = set()
    for text in texts:
        for sentence in re.split(r"(?<=[.!?:\n])\s+|\n", str(text or "")):
            words = _WORD.findall(sentence)
            for index, word in enumerate(words):
                if len(word) < 3 or word.lower() in _COMMON:
                    continue
                camel = bool(re.search(r"[a-z][A-Z]", word))
                caps = word.isupper() and len(word) >= 3
                proper = index > 0 and word[0].isupper()
                if camel or caps or proper:
                    found.add(word)
    return found


def _replace_distinctive(text: str, ctx: ClientContext, vault: _Vault) -> str:
    for word in sorted(distinctive_words(ctx.texts), key=len, reverse=True):
        text = _sub_ci(text, word, vault.put("<name>"))
    return text


def _words(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(_PLACEHOLDER.sub(" ", str(text or "")))]


def _shared_run(text: str, sources: Iterable[str], size: int = OVERLAP_WORDS) -> str:
    words = _words(text)
    if len(words) < size:
        return ""
    grams = {" ".join(words[i:i + size]) for i in range(len(words) - size + 1)}
    for source in sources:
        src = _words(source)
        for i in range(len(src) - size + 1):
            gram = " ".join(src[i:i + size])
            if gram in grams:
                return gram
    return ""


def sanitize_text(text: Any, ctx: ClientContext, *, limit: int = 1500) -> str:
    """``text`` with client data replaced or removed (rules in the module docstring), at most ``limit`` chars."""
    vault = _Vault()
    value = str(text or "")
    value = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", value)
    value = _drop_code(value, vault)
    value = _replace_urls(value, ctx, vault)
    value = _replace_known(value, ctx, vault)
    value = _TICKET.sub(lambda _m: vault.put("<ticket>"), value)
    value = _replace_quoted(value, vault)
    value = _replace_paths(value, ctx, vault)
    value = _replace_distinctive(value, ctx, vault)
    value = vault.restore(value)
    value = _drop_copied(value, ctx.texts)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"(?:<code removed>\s*){2,}", "<code removed> ", value)
    value = "\n".join(line.strip() for line in value.splitlines() if line.strip())
    if len(value) > limit:
        value = value[: max(0, limit - 1)].rstrip() + "…"
    return value


def leaks(text: str, ctx: ClientContext) -> list[str]:
    """What in ``text`` is known client data, or looks like it (empty when nothing)."""
    found: list[str] = []
    text = str(text or "")
    if ctx.harness_repo:
        # Links to the harness's own issues and PRs are fine.
        text = re.sub(rf"https://github\.com/{re.escape(ctx.harness_repo)}(?:/[\w./#-]*)?", " ", text, flags=re.I)
    lowered = _PLACEHOLDER.sub(" ", text).lower()

    def contains(needle: str) -> bool:
        return bool(needle) and re.search(rf"(?<![\w-]){re.escape(needle.lower())}(?![\w-])", lowered) is not None

    for slug in ctx.repos:
        if contains(slug):
            found.append(f"repo {slug}")
        name = slug.split("/", 1)[1]
        if len(name) >= 3 and contains(name):
            found.append(f"repo name {name}")
    for ticket in ctx.tickets:
        if re.search(rf"(?<![a-z0-9]){re.escape(ticket.lower())}(?![0-9])", lowered):
            found.append(f"ticket {ticket}")
    for key in _project_keys(ctx):
        if re.search(rf"(?<![a-z0-9]){re.escape(key.lower())}-\d+", lowered):
            found.append(f"ticket of {key}")
    for name in _name_words(ctx):
        if len(name) >= 2 and contains(name):
            found.append(f"name {name}")
    if _TICKET.search(text):
        found.append("ticket-shaped key")
    if _URL.search(text) or _EMAIL.search(text):
        found.append("url or e-mail")
    run = _shared_run(text, ctx.texts)
    if run:
        found.append(f"copied client text ({run!r})")
    return found


def safe_field(text: Any, ctx: ClientContext, *, limit: int = 1500) -> str:
    """Sanitised ``text``, or :data:`WITHHELD` when anything client-looking survives sanitising."""
    value = sanitize_text(text, ctx, limit=limit)
    if not value:
        return ""
    return WITHHELD if leaks(value, ctx) else value


def assert_clean(text: str, ctx: ClientContext) -> None:
    """Raise ``ValueError`` when ``text`` leaks client data (a last check before anything is sent)."""
    found = leaks(text, ctx)
    if found:
        raise ValueError(f"refusing to send text with client data: {', '.join(found[:3])}")
