"""Bash directory boundary enforcement for Claude tool calls."""

import fnmatch
import os
import re
import shlex
from pathlib import Path
from typing import Iterable, List, Optional, Set, Tuple

# Subdirectories under ~/.claude/ that Claude Code uses internally.
_CLAUDE_INTERNAL_SUBDIRS: Set[str] = {"plans", "todos"}

# Commands that modify the filesystem or change context and should have paths checked
_FS_MODIFYING_COMMANDS: Set[str] = {
    "mkdir",
    "touch",
    "cp",
    "mv",
    "rm",
    "rmdir",
    "ln",
    "install",
    "tee",
    "cd",
    "chmod",
    "chown",
    "chgrp",
    "truncate",
}

# Commands that are read-only or don't take filesystem paths
_READ_ONLY_COMMANDS: Set[str] = {
    "cat",
    "ls",
    "head",
    "tail",
    "less",
    "more",
    "which",
    "whoami",
    "pwd",
    "echo",
    "printf",
    "env",
    "printenv",
    "date",
    "wc",
    "sort",
    "uniq",
    "diff",
    "file",
    "stat",
    "du",
    "df",
    "tree",
    "realpath",
    "dirname",
    "basename",
}

# Actions / expressions that make ``find`` a filesystem-modifying command
_FIND_MUTATING_ACTIONS: Set[str] = {"-delete", "-exec", "-execdir", "-ok", "-okdir"}

# Bash command separators
_COMMAND_SEPARATORS: Set[str] = {
    "&&",
    "||",
    ";",
    ";;",
    "|",
    "|&",
    "&",
    "(",
    ")",
    "{",
    "}",
}

# Backslash-newline preceded by an even run of backslashes (group 1).
_LINE_CONTINUATION_RE = re.compile(r"(?<!\\)((?:\\\\)*)\\\n")
# Shell operators, used to split punctuation runs such as ``)>`` or ``>&``.
_OPERATOR_RE = re.compile(r"&&|\|\||;;|\|&|&>>|&>|>>|>\||>&|<<<|<<|<&|<>|[();|&<>]")
_OUTPUT_REDIRECTS: Set[str] = {">", ">>", ">|", "&>", "&>>", ">&", "<>"}
_INPUT_REDIRECTS: Set[str] = {"<", "<<", "<<<", "<&"}
_SAFE_REDIRECT_TARGETS: Set[str] = {"/dev/null", "/dev/stdout", "/dev/stderr"}

# Prefixes that run the command that follows them.
_COMMAND_WRAPPERS: Set[str] = {
    "env",
    "sudo",
    "nohup",
    "time",
    "nice",
    "command",
    "exec",
    "builtin",
    "stdbuf",
    "timeout",
    "noglob",
}
_SHELL_COMMANDS: Set[str] = {"sh", "bash", "zsh", "dash", "ksh"}
_INTERPRETER_RE = re.compile(r"^(?:python[\d.]*|node|perl|ruby|php)$")
_INLINE_CODE_FLAGS: Set[str] = {"-c", "-e", "-E", "-p", "-r", "--eval", "--print"}
_QUOTED_PATH_RE = re.compile(r"""(['"])((?:~|\$HOME|\$\{HOME\}|\.\.?)?/[^'"\s]+)\1""")
_HOME_VAR_RE = re.compile(r"\$(?:\{HOME\}|HOME(?![A-Za-z0-9_]))")

# Claude Code reads permission rules, hooks, skills, commands, agents and MCP
# servers from these paths. Writing one of them can pre-approve tools for the
# next session, which turns the can_use_tool checks off, so tool calls may
# read them but never write them.
_AGENT_CONFIG_TEXT_RE = re.compile(
    r"\.claude(?:\.json)?(?![\w.-])|\.mcp\.json(?![\w.-])", re.IGNORECASE
)
_AGENT_CONFIG_FILES: Set[str] = {".claude.json", ".mcp.json"}

# Commands that only read, so they may name an agent config file.
_CONFIG_READ_COMMANDS: Set[str] = {
    "cat",
    "head",
    "tail",
    "less",
    "more",
    "ls",
    "stat",
    "file",
    "wc",
    "grep",
    "egrep",
    "fgrep",
    "diff",
    "jq",
    "realpath",
    "dirname",
    "basename",
    "echo",
    "printf",
}

_AGENT_CONFIG_ERROR = (
    "Writing Claude Code settings, hooks or MCP configuration is not allowed"
)

_HARD_LINK_ERROR = (
    "Creating hard links is not allowed: a write through one changes the "
    "linked file, which the path checks cannot see"
)


class _Command:
    """One simple command of a bash command line."""

    def __init__(self) -> None:
        self.tokens: List[str] = []
        self.redirect_targets: List[str] = []


def _parse_command_line(command: str) -> Optional[List[_Command]]:
    """Split a bash command line into simple commands and redirection targets.

    Returns None when the command cannot be tokenized.
    """
    # A backslash-newline joins lines, unless the backslash is itself escaped
    # (an even run of backslashes); other newlines and backticks start a new
    # command in bash.
    text = _LINE_CONTINUATION_RE.sub(r"\1", command)
    text = text.replace("\n", " ; ").replace("`", " ; ")
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        raw_tokens = list(lexer)
    except ValueError:
        return None

    tokens: List[str] = []
    for token in raw_tokens:
        if token and all(c in "();<>|&" for c in token):
            tokens.extend(_OPERATOR_RE.findall(token))
        else:
            tokens.append(token)

    commands: List[_Command] = [_Command()]
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in _COMMAND_SEPARATORS:
            commands.append(_Command())
        elif token in _OUTPUT_REDIRECTS or token in _INPUT_REDIRECTS:
            target = tokens[i + 1] if i + 1 < len(tokens) else ""
            is_fd = target.isdigit() or target == "-"
            if token in _OUTPUT_REDIRECTS and target and not is_fd:
                commands[-1].redirect_targets.append(target)
            i += 1
        else:
            commands[-1].tokens.append(token)
        i += 1

    for cmd in commands:
        cmd.tokens = _strip_command_prefixes(cmd.tokens)
    return [cmd for cmd in commands if cmd.tokens or cmd.redirect_targets]


def _strip_command_prefixes(tokens: List[str]) -> List[str]:
    """Drop variable assignments and wrappers such as ``env`` or ``sudo``."""
    tokens = list(tokens)
    while tokens:
        head = tokens[0]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", head):
            tokens.pop(0)
        elif Path(head).name in _COMMAND_WRAPPERS and len(tokens) > 1:
            tokens.pop(0)
            while tokens and (
                tokens[0].startswith("-") or "=" in tokens[0] or tokens[0].isdigit()
            ):
                tokens.pop(0)
        else:
            break
    return tokens


def _expand_home(token: str) -> str:
    """Expand a leading ``~`` and ``$HOME`` the way the shell would."""
    home = str(Path.home())
    if token == "~" or token.startswith("~/"):
        token = home + token[1:]
    return _HOME_VAR_RE.sub(lambda _m: home, token)


def _resolve_token(token: str, cwd: Path) -> Path:
    """Resolve a path argument against *cwd*, following symlinks."""
    expanded = _expand_home(token)
    if expanded.startswith("/"):
        return Path(expanded).resolve()
    return (cwd / expanded).resolve()


def _agent_config_dirs() -> List[Path]:
    """Return the user-level Claude Code configuration directories."""
    dirs = [Path.home() / ".claude"]
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        dirs.append(Path(config_dir).expanduser())
    return dirs


def _folded(path: Path) -> Path:
    """Case-fold every component: APFS and NTFS match names case-insensitively."""
    return Path(*[part.casefold() for part in path.parts])


def _is_agent_config(path: Path) -> bool:
    """Return True if *path* is Claude Code configuration.

    That is anything under a ``.claude`` directory (settings, hooks, skills,
    commands, agents...), the directory itself, the user-level config dir,
    ``.claude.json`` and ``.mcp.json``. Names are compared case-folded, since
    ``.CLAUDE/SETTINGS.JSON`` is the same file on a case-insensitive
    filesystem. The scratch dirs Claude Code writes to on its own (``plans``
    and ``todos`` in the user config dir) are not configuration.
    """
    if not path.parts:
        return False
    folded = _folded(path)
    for config_dir in _agent_config_dirs():
        for candidate in {config_dir, config_dir.resolve()}:
            try:
                rel = folded.relative_to(_folded(candidate))
            except ValueError:
                continue
            return not rel.parts or rel.parts[0] not in _CLAUDE_INTERNAL_SUBDIRS
    return folded.name in _AGENT_CONFIG_FILES or ".claude" in folded.parts


def _is_hard_link_to_agent_config(path: Path, working_directory: Path) -> bool:
    """Return True if *path* is the same file as a settings file Claude loads.

    Those are the user-level settings and the settings of the project in
    *working_directory*.
    """
    try:
        target = os.stat(path)
    except OSError:
        return False
    if target.st_nlink < 2:
        return False
    candidates = [
        Path.home() / ".claude.json",
        working_directory / ".claude" / "settings.json",
        working_directory / ".claude" / "settings.local.json",
        working_directory / ".mcp.json",
    ]
    for config_dir in _agent_config_dirs():
        candidates += [
            config_dir / "settings.json",
            config_dir / "settings.local.json",
        ]
    for candidate in candidates:
        try:
            if os.path.samestat(target, os.stat(candidate)):
                return True
        except OSError:
            continue
    return False


def is_agent_config_path(file_path: str, working_directory: Path) -> bool:
    """Return True if writing *file_path* would change Claude Code configuration.

    Covers anything in a ``.claude`` directory at any depth, ``.claude.json``,
    ``.mcp.json`` and the user-level config directory (see ``_is_agent_config``). The
    path is checked as written and after following symlinks, so a link that
    points at one of these files is covered as well.
    """
    if not file_path:
        return False
    expanded = Path(_expand_home(file_path))
    if not expanded.is_absolute():
        expanded = working_directory / expanded
    lexical = Path(os.path.normpath(expanded))
    try:
        resolved = expanded.resolve()
    except (OSError, RuntimeError):
        resolved = lexical
    return (
        _is_agent_config(lexical)
        or _is_agent_config(resolved)
        or _is_hard_link_to_agent_config(resolved, working_directory)
    )


def is_hard_linked_file(file_path: str, working_directory: Path) -> bool:
    """Return True if *file_path* is an existing file with other hard links.

    Writing it also changes every other name of the file, including one the
    path checks would refuse, such as a settings file.
    """
    path = Path(_expand_home(file_path))
    if not path.is_absolute():
        path = working_directory / path
    try:
        return os.stat(path).st_nlink > 1 and path.is_file()
    except OSError:
        return False


def _creates_hard_link(command_name: str, args: List[str]) -> bool:
    """Return True if the command creates a hard link (``ln``, ``link``, ``cp -l``)."""
    short_flags = "".join(a[1:] for a in args if a.startswith("-") and a[1:2] != "-")
    if command_name == "link":
        return True
    if command_name == "ln":
        return "s" not in short_flags and "--symbolic" not in args
    if command_name == "cp":
        return "l" in short_flags or "--link" in args
    return False


def _matches_excluded_command(tokens: List[str], entry: str) -> bool:
    """Return True if Claude Code could match the command *tokens* to *entry*.

    Follows the ``sandbox.excludedCommands`` formats: ``cmd:*`` is a prefix
    (``cmd`` with any arguments), an entry with ``*`` is a wildcard, and any
    other entry must equal the whole command. Errs towards a match: a wildcard
    is compared on the program name only, and digit-only words (file
    descriptors such as the ``2`` in ``2>/dev/null``) are ignored.
    """
    prefix = entry.endswith(":*")
    words = (entry[:-2] if prefix else entry).split()
    if not words or not tokens:
        return False
    if "*" in entry and not prefix:
        return fnmatch.fnmatchcase(tokens[0], words[0])
    if tokens[0] != words[0]:
        return False
    args = [t for t in tokens[1:] if not t.isdigit()]
    expected = [w for w in words[1:] if not w.isdigit()]
    if prefix:
        return args[: len(expected)] == expected
    return args == expected


def find_unsandboxed_command_chain(
    command: str, excluded_commands: Iterable[str]
) -> Optional[str]:
    """Return the excluded entry that takes this command line out of the sandbox.

    Claude Code splits a command line into its simple commands and runs the
    whole line outside the sandbox when any of them matches an entry of
    ``sandbox.excludedCommands``. A line that joins such a command with other
    commands would run those unsandboxed too, so it is reported. An excluded
    command on its own is the operator's choice and is not reported.
    """
    entries = [entry.strip() for entry in excluded_commands if entry.strip()]
    if not entries:
        return None
    commands = _parse_command_line(command)
    if commands is None:
        # Bash may still run what shlex cannot split (a quote in a comment),
        # so look for the excluded program anywhere in the line.
        for entry in entries:
            program = entry.removesuffix(":*").split()[0]
            if "*" in program or re.search(
                rf"(?<![\w./-]){re.escape(program)}(?![\w./-])", command
            ):
                return entry
        return None
    simple = [cmd for cmd in commands if cmd.tokens]
    if len(simple) < 2:
        return None
    for cmd in simple:
        for entry in entries:
            if _matches_excluded_command(cmd.tokens, entry):
                return entry
    return None


def _find_shell_script(args: List[str]) -> Optional[str]:
    """Return the script passed to ``sh -c`` (or ``-lc``, ``-ec``...)."""
    for i, arg in enumerate(args):
        if arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:]:
            return args[i + 1] if i + 1 < len(args) else None
    return None


def _inline_code(args: List[str]) -> List[str]:
    """Return the code passed inline to an interpreter (``python -c``...)."""
    return [args[i + 1] for i, arg in enumerate(args[:-1]) if arg in _INLINE_CODE_FLAGS]


def _boundary_error(command_name: str, target: str, approved: Path) -> str:
    return (
        f"Directory boundary violation: '{command_name}' targets "
        f"'{target}' which is outside approved directory '{approved}'"
    )


def check_bash_directory_boundary(
    command: str,
    working_directory: Path,
    approved_directory: Path,
) -> Tuple[bool, Optional[str]]:
    """Check if a bash command's paths stay within the approved directory.

    Also refuses commands that write Claude Code settings, hooks or MCP
    configuration, inside the approved directory or not. This is a best-effort
    static check; the OS-level sandbox is the boundary for code it cannot see.
    """
    commands = _parse_command_line(command)
    names_agent_config = bool(_AGENT_CONFIG_TEXT_RE.search(command))

    if commands is None:
        # If we can't parse the command, let it through —
        # the sandbox will catch it at the OS level
        if names_agent_config:
            return False, _AGENT_CONFIG_ERROR
        return True, None

    if not commands:
        return True, None

    only_reads = all(
        not cmd.redirect_targets
        and (not cmd.tokens or Path(cmd.tokens[0]).name in _CONFIG_READ_COMMANDS)
        for cmd in commands
    )
    if names_agent_config and not only_reads:
        return False, _AGENT_CONFIG_ERROR

    resolved_approved = approved_directory.resolve()
    cwd = working_directory

    # Check each command in the chain
    for cmd in commands:
        # Output redirection writes a file whatever the command is.
        for target in cmd.redirect_targets:
            if target in _SAFE_REDIRECT_TARGETS:
                continue
            try:
                resolved = _resolve_token(target, cwd)
            except (ValueError, OSError):
                continue
            if is_agent_config_path(target, cwd):
                return False, _AGENT_CONFIG_ERROR
            if not _is_within_directory(resolved, resolved_approved):
                return False, _boundary_error(">", target, resolved_approved)

        if not cmd.tokens:
            continue

        base_command = Path(cmd.tokens[0]).name
        args = cmd.tokens[1:]

        if _creates_hard_link(base_command, args):
            return False, _HARD_LINK_ERROR

        if not only_reads:
            for token in args:
                if token.startswith("-"):
                    continue
                if is_agent_config_path(token, cwd):
                    return False, _AGENT_CONFIG_ERROR

        if base_command in _SHELL_COMMANDS or base_command == "eval":
            script = " ".join(args) if base_command == "eval" else None
            script = script or _find_shell_script(args)
            if script:
                valid, error = check_bash_directory_boundary(
                    script, cwd, approved_directory
                )
                if not valid:
                    return valid, error
            continue

        if _INTERPRETER_RE.match(base_command):
            for code in _inline_code(args):
                for match in _QUOTED_PATH_RE.finditer(code):
                    literal = match.group(2)
                    try:
                        resolved = _resolve_token(literal, cwd)
                    except (ValueError, OSError):
                        continue
                    if is_agent_config_path(literal, cwd):
                        return False, _AGENT_CONFIG_ERROR
                    if not _is_within_directory(resolved, resolved_approved):
                        return False, _boundary_error(
                            base_command, literal, resolved_approved
                        )
            continue

        # Read-only commands are always allowed
        if base_command in _READ_ONLY_COMMANDS:
            continue

        # Determine if this specific command in the chain needs path validation
        needs_check = False
        if base_command == "find":
            needs_check = any(t in _FIND_MUTATING_ACTIONS for t in args)
        elif base_command in _FS_MODIFYING_COMMANDS:
            needs_check = True

        if not needs_check:
            continue

        path_args = [t for t in args if not t.startswith("-")]
        if base_command == "cd":
            if "-" in args:
                return False, "Directory boundary violation: 'cd -' is not supported"
            # A bare ``cd`` goes to the home directory.
            path_args = path_args[:1] or ["~"]

        # Check each argument for paths outside the boundary
        for token in path_args:
            # Resolve both absolute and relative paths against the working
            # directory so that traversal sequences like ``../../evil`` are
            # caught instead of being silently allowed.
            try:
                resolved = _resolve_token(token, cwd)
            except (ValueError, OSError):
                # If path resolution fails, the command might be malformed or
                # using bash features we can't statically analyze.
                # We skip checking this token and rely on the OS-level sandbox.
                continue

            if not _is_within_directory(resolved, resolved_approved):
                return False, _boundary_error(base_command, token, resolved_approved)

            # Later relative paths resolve against the new directory. Keep
            # it unresolved so a symlinked ``.claude`` is still recognised.
            if base_command == "cd":
                cwd = Path(os.path.normpath(cwd / _expand_home(token)))

    return True, None


def _is_claude_internal_path(file_path: str) -> bool:
    """Check whether *file_path* points inside ``~/.claude/`` (allowed subdirs only)."""
    try:
        resolved = Path(file_path).resolve()
        home = Path.home().resolve()
        claude_dir = home / ".claude"

        # Path must be inside ~/.claude/
        try:
            rel = resolved.relative_to(claude_dir)
        except ValueError:
            return False

        # Must be in one of the known subdirectories
        top_part = rel.parts[0] if rel.parts else ""
        return top_part in _CLAUDE_INTERNAL_SUBDIRS

    except Exception:
        return False


def _is_within_directory(path: Path, directory: Path) -> bool:
    """Check if path is within directory."""
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False
