"""Test bash directory boundary checking."""

from pathlib import Path
from unittest.mock import patch

import pytest

from src.claude.monitor import (
    _is_claude_internal_path,
    check_bash_directory_boundary,
    find_unsandboxed_command_chain,
    is_agent_config_path,
    is_hard_linked_file,
)


class TestCheckBashDirectoryBoundary:
    """Test the check_bash_directory_boundary function."""

    def setup_method(self) -> None:
        self.approved = Path("/root/projects")
        self.cwd = Path("/root/projects/myapp")

    def test_mkdir_outside_approved_directory(self) -> None:
        valid, error = check_bash_directory_boundary(
            "mkdir -p /root/web1", self.cwd, self.approved
        )
        assert not valid
        assert "directory boundary violation" in error.lower()
        assert "/root/web1" in error

    def test_mkdir_inside_approved_directory(self) -> None:
        valid, error = check_bash_directory_boundary(
            "mkdir -p /root/projects/newdir", self.cwd, self.approved
        )
        assert valid
        assert error is None

    def test_touch_outside_approved_directory(self) -> None:
        valid, error = check_bash_directory_boundary(
            "touch /tmp/evil.txt", self.cwd, self.approved
        )
        assert not valid
        assert "/tmp/evil.txt" in error

    def test_cp_outside_approved_directory(self) -> None:
        valid, error = check_bash_directory_boundary(
            "cp file.txt /etc/passwd", self.cwd, self.approved
        )
        assert not valid
        assert "/etc/passwd" in error

    def test_mv_outside_approved_directory(self) -> None:
        valid, error = check_bash_directory_boundary(
            "mv /root/projects/file.txt /tmp/file.txt", self.cwd, self.approved
        )
        assert not valid
        assert "/tmp/file.txt" in error

    def test_relative_paths_inside_approved_pass(self) -> None:
        valid, error = check_bash_directory_boundary(
            "mkdir -p subdir/nested", self.cwd, self.approved
        )
        assert valid
        assert error is None

    def test_relative_path_traversal_escaping_approved_dir(self) -> None:
        """mkdir ../../evil from /root/projects/myapp resolves to /root/evil."""
        valid, error = check_bash_directory_boundary(
            "mkdir ../../evil", self.cwd, self.approved
        )
        assert not valid
        assert "directory boundary violation" in error.lower()
        assert "../../evil" in error

    def test_relative_path_traversal_staying_inside_approved_dir(self) -> None:
        """mkdir ../sibling from /root/projects/myapp -> /root/projects/sibling (ok)."""
        valid, error = check_bash_directory_boundary(
            "mkdir ../sibling", self.cwd, self.approved
        )
        assert valid
        assert error is None

    def test_relative_path_dot_dot_at_boundary_root(self) -> None:
        """mkdir .. from approved root itself should be blocked."""
        cwd_at_root = Path("/root/projects")
        valid, error = check_bash_directory_boundary(
            "touch ../outside.txt", cwd_at_root, self.approved
        )
        assert not valid
        assert "directory boundary violation" in error.lower()

    def test_read_only_commands_pass(self) -> None:
        for cmd in ["cat /etc/hosts", "ls /tmp", "head /var/log/syslog"]:
            valid, error = check_bash_directory_boundary(cmd, self.cwd, self.approved)
            assert valid, f"Expected read-only command to pass: {cmd}"
            assert error is None

    def test_non_fs_commands_pass(self) -> None:
        """Commands not in the filesystem-modifying set pass through."""
        for cmd in ["python script.py", "node app.js", "cargo build"]:
            valid, error = check_bash_directory_boundary(cmd, self.cwd, self.approved)
            assert valid, f"Expected non-fs command to pass: {cmd}"
            assert error is None

    def test_empty_command(self) -> None:
        valid, error = check_bash_directory_boundary("", self.cwd, self.approved)
        assert valid
        assert error is None

    def test_flags_are_skipped(self) -> None:
        valid, error = check_bash_directory_boundary(
            "mkdir -p -v /root/projects/dir", self.cwd, self.approved
        )
        assert valid
        assert error is None

    def test_unparseable_command_passes_through(self) -> None:
        """Malformed quoting should pass through (sandbox catches it at OS level)."""
        valid, error = check_bash_directory_boundary(
            "mkdir 'unclosed quote", self.cwd, self.approved
        )
        assert valid
        assert error is None

    def test_rm_outside_approved_directory(self) -> None:
        valid, error = check_bash_directory_boundary(
            "rm /var/tmp/somefile", self.cwd, self.approved
        )
        assert not valid
        assert "/var/tmp/somefile" in error

    def test_ln_outside_approved_directory(self) -> None:
        valid, error = check_bash_directory_boundary(
            "ln -s /root/projects/file /tmp/link", self.cwd, self.approved
        )
        assert not valid
        assert "/tmp/link" in error

    # --- find command handling ---

    def test_find_without_mutating_flags_passes(self) -> None:
        """Plain find (read-only) should pass regardless of search path."""
        valid, error = check_bash_directory_boundary(
            "find /tmp -name '*.log'", self.cwd, self.approved
        )
        assert valid
        assert error is None

    def test_find_delete_outside_approved_dir(self) -> None:
        """find /tmp -delete should be blocked because /tmp is outside."""
        valid, error = check_bash_directory_boundary(
            "find /tmp -name '*.log' -delete", self.cwd, self.approved
        )
        assert not valid
        assert "directory boundary violation" in error.lower()
        assert "/tmp" in error

    def test_find_exec_outside_approved_dir(self) -> None:
        """find /var -exec rm {} ; should be blocked."""
        valid, error = check_bash_directory_boundary(
            "find /var -exec rm {} ;", self.cwd, self.approved
        )
        assert not valid
        assert "/var" in error

    def test_find_delete_inside_approved_dir(self) -> None:
        """find inside approved dir with -delete should pass."""
        valid, error = check_bash_directory_boundary(
            "find /root/projects/myapp -name '*.pyc' -delete",
            self.cwd,
            self.approved,
        )
        assert valid
        assert error is None

    def test_find_delete_relative_path_inside(self) -> None:
        """find . -delete from inside approved dir should pass."""
        valid, error = check_bash_directory_boundary(
            "find . -name '*.pyc' -delete", self.cwd, self.approved
        )
        assert valid
        assert error is None

    def test_find_execdir_outside_approved_dir(self) -> None:
        """find with -execdir outside approved dir should be blocked."""
        valid, error = check_bash_directory_boundary(
            "find /etc -execdir cat {} ;", self.cwd, self.approved
        )
        assert not valid
        assert "/etc" in error

    # --- cd and command chaining handling ---

    def test_cd_outside_approved_directory(self) -> None:
        """cd to an outside directory should be blocked."""
        valid, error = check_bash_directory_boundary("cd /tmp", self.cwd, self.approved)
        assert not valid
        assert "directory boundary violation" in error.lower()
        assert "/tmp" in error

    def test_cd_inside_approved_directory(self) -> None:
        """cd to an inside directory should pass."""
        valid, error = check_bash_directory_boundary(
            "cd subdir", self.cwd, self.approved
        )
        assert valid
        assert error is None

    def test_chained_commands_outside_blocked(self) -> None:
        """Any command in a chain targeting outside should be blocked."""
        # Chained with &&
        valid, error = check_bash_directory_boundary(
            "ls && rm /etc/passwd", self.cwd, self.approved
        )
        assert not valid
        assert "/etc/passwd" in error

        # Chained with ;
        valid, error = check_bash_directory_boundary(
            "mkdir newdir; mv file.txt /tmp/", self.cwd, self.approved
        )
        assert not valid
        assert "/tmp/" in error

    def test_chained_commands_inside_pass(self) -> None:
        """Chain of valid commands should pass."""
        valid, error = check_bash_directory_boundary(
            "cd subdir && touch file.txt && ls -la", self.cwd, self.approved
        )
        assert valid
        assert error is None

    def test_chained_cd_outside_blocked(self) -> None:
        """cd /tmp && something should be blocked."""
        valid, error = check_bash_directory_boundary(
            "cd /tmp && ls", self.cwd, self.approved
        )
        assert not valid
        assert "/tmp" in error


class TestIsClaudeInternalPath:
    """Test the _is_claude_internal_path helper function."""

    def test_plan_file_is_internal(self, tmp_path: Path) -> None:
        """~/.claude/plans/some-plan.md should be recognised as internal."""
        with patch("src.claude.monitor.Path.home", return_value=tmp_path):
            (tmp_path / ".claude" / "plans").mkdir(parents=True)
            plan_file = tmp_path / ".claude" / "plans" / "my-plan.md"
            plan_file.touch()
            assert _is_claude_internal_path(str(plan_file)) is True

    def test_todo_file_is_internal(self, tmp_path: Path) -> None:
        """~/.claude/todos/todo.md should be recognised as internal."""
        with patch("src.claude.monitor.Path.home", return_value=tmp_path):
            (tmp_path / ".claude" / "todos").mkdir(parents=True)
            todo_file = tmp_path / ".claude" / "todos" / "todo.md"
            todo_file.touch()
            assert _is_claude_internal_path(str(todo_file)) is True

    def test_settings_json_is_not_internal(self, tmp_path: Path) -> None:
        """~/.claude/settings.json holds permission rules: never auto-allowed."""
        with patch("src.claude.monitor.Path.home", return_value=tmp_path):
            (tmp_path / ".claude").mkdir(parents=True)
            settings_file = tmp_path / ".claude" / "settings.json"
            settings_file.touch()
            assert _is_claude_internal_path(str(settings_file)) is False

    def test_arbitrary_file_under_claude_dir_rejected(self, tmp_path: Path) -> None:
        """Files directly under ~/.claude/ (not in known subdirs) are rejected."""
        with patch("src.claude.monitor.Path.home", return_value=tmp_path):
            (tmp_path / ".claude").mkdir(parents=True)
            secret = tmp_path / ".claude" / "credentials.json"
            secret.touch()
            assert _is_claude_internal_path(str(secret)) is False

    def test_path_outside_claude_dir_rejected(self, tmp_path: Path) -> None:
        """Paths outside ~/.claude/ entirely are rejected."""
        with patch("src.claude.monitor.Path.home", return_value=tmp_path):
            assert _is_claude_internal_path("/etc/passwd") is False
            assert _is_claude_internal_path("/tmp/evil.txt") is False

    def test_empty_path_rejected(self, tmp_path: Path) -> None:
        """Empty paths are rejected."""
        assert _is_claude_internal_path("") is False

    def test_unknown_subdir_rejected(self, tmp_path: Path) -> None:
        """Unknown subdirectories under ~/.claude/ are rejected."""
        with patch("src.claude.monitor.Path.home", return_value=tmp_path):
            (tmp_path / ".claude" / "secrets").mkdir(parents=True)
            bad_file = tmp_path / ".claude" / "secrets" / "key.pem"
            bad_file.touch()
            assert _is_claude_internal_path(str(bad_file)) is False


@pytest.fixture
def layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A fake home with Claude settings, an approved dir and an outside dir."""
    home = tmp_path / "home"
    approved = tmp_path / "approved"
    project = approved / "project"
    outside = tmp_path / "outside"
    for directory in (home / ".claude", project / ".claude", outside):
        directory.mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text("{}")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return {
        "home": home,
        "approved": approved,
        "project": project,
        "outside": outside,
    }


class TestIsAgentConfigPath:
    """Claude Code settings, hooks and MCP files are recognised wherever they are."""

    @pytest.mark.parametrize(
        "relative",
        [
            ".claude/settings.json",
            ".claude/settings.local.json",
            ".claude/./settings.json",
            ".Claude/Settings.JSON",
            "sub/.claude/settings.json",
            ".claude/hooks/pre-tool.sh",
            ".claude/skills/deploy/SKILL.md",
            ".claude/commands/deploy.md",
            ".claude/agents/reviewer.md",
            ".CLAUDE/SKILLS/x/SKILL.MD",
            ".mcp.json",
        ],
    )
    def test_project_config_files(self, layout: dict, relative: str) -> None:
        assert is_agent_config_path(relative, layout["project"]) is True

    def test_user_config_files(self, layout: dict) -> None:
        home = layout["home"]
        for path in (
            home / ".claude" / "settings.json",
            home / ".claude" / "settings.local.json",
            home / ".claude" / "hooks" / "x.sh",
            home / ".claude" / "agents" / "x.md",
            home / ".claude.json",
        ):
            assert is_agent_config_path(str(path), layout["project"]) is True, path

    def test_tilde_is_expanded(self, layout: dict) -> None:
        assert is_agent_config_path("~/.claude/settings.json", layout["project"])
        assert is_agent_config_path("$HOME/.claude/settings.json", layout["project"])

    def test_symlink_to_user_settings(self, layout: dict) -> None:
        project = layout["project"]
        (project / "cfg.json").symlink_to(layout["home"] / ".claude" / "settings.json")
        (project / "cfgdir").symlink_to(layout["home"] / ".claude")
        assert is_agent_config_path("cfg.json", project) is True
        assert is_agent_config_path("cfgdir/settings.json", project) is True

    def test_hard_link_to_user_settings(self, layout: dict) -> None:
        project = layout["project"]
        (project / "hard.json").hardlink_to(
            layout["home"] / ".claude" / "settings.json"
        )
        assert is_agent_config_path("hard.json", project) is True

    def test_claude_config_dir_env(
        self, layout: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_dir = layout["outside"] / "claude-config"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
        assert is_agent_config_path(
            str(config_dir / "settings.json"), layout["project"]
        )

    @pytest.mark.parametrize(
        "relative",
        [
            "src/main.py",
            "settings.json",
            "config/settings.json",
            "claude/settings.json",
            ".claude-notes/todo.md",
            "CLAUDE.md",
        ],
    )
    def test_ordinary_files(self, layout: dict, relative: str) -> None:
        assert is_agent_config_path(relative, layout["project"]) is False

    def test_internal_scratch_dirs_are_not_config(self, layout: dict) -> None:
        plan = layout["home"] / ".claude" / "plans" / "plan.md"
        assert is_agent_config_path(str(plan), layout["project"]) is False


class TestBashAgentConfigWrites:
    """Bash may read agent settings but never write them."""

    @pytest.mark.parametrize(
        "command",
        [
            "echo '{}' > ~/.claude/settings.json",
            "echo '{}' > $HOME/.claude/settings.json",
            "cd ~/.claude && echo '{}' > settings.json",
            "cd .claude && tee settings.local.json",
            "cp evil.json .claude/settings.json",
            "echo '{}'>.claude/settings.json",
            "sed -i s/a/b/ .claude/settings.json",
            "python3 -c \"open('.claude/settings.json','w')\"",
            "ln -s ~/.claude/settings.json s.json",
            "mkdir -p .claude/hooks",
            "mkdir -p .claude/commands",
            "echo x > .CLAUDE/SETTINGS.JSON",
            "echo '{}' > .mcp.json",
            "echo '{}' > ~/.claude.json",
            "mv settings.json .claude/",
            "cp -r evil .claude",
            "cd .claude && cp ../evil.json ./",
        ],
    )
    def test_write_is_denied(self, layout: dict, command: str) -> None:
        valid, error = check_bash_directory_boundary(
            command, layout["project"], layout["approved"]
        )
        assert not valid, command
        assert "Claude Code settings" in error

    def test_write_through_symlink_is_denied(self, layout: dict) -> None:
        project = layout["project"]
        (project / "cfg.json").symlink_to(layout["home"] / ".claude" / "settings.json")
        valid, error = check_bash_directory_boundary(
            "echo '{}' > cfg.json", project, layout["approved"]
        )
        assert not valid
        assert "Claude Code settings" in error

    def test_cd_into_symlinked_claude_dir_is_denied(self, layout: dict) -> None:
        project = layout["project"]
        target = project / "real-config"
        target.mkdir()
        (project / "sub").mkdir()
        (project / "sub" / ".claude").symlink_to(target)
        valid, _ = check_bash_directory_boundary(
            "cd sub/.claude && touch settings.json", project, layout["approved"]
        )
        assert not valid

    @pytest.mark.parametrize(
        "command",
        [
            "cat .claude/settings.json",
            "grep allow ~/.claude/settings.json",
            "jq . .claude/settings.local.json",
        ],
    )
    def test_read_is_allowed(self, layout: dict, command: str) -> None:
        valid, error = check_bash_directory_boundary(
            command, layout["project"], layout["approved"]
        )
        assert valid, error


class TestBashBoundaryParsing:
    """The boundary check follows cd, ~, redirections and nested shells."""

    @pytest.mark.parametrize(
        "command",
        [
            "cd {approved} && rm -rf ../outside",
            "rm -rf ~/victim",
            "rm -rf $HOME/victim",
            "rm -rf ${{HOME}}/victim",
            "cd && rm -rf victim",
            "cd -",
            "echo x > {outside}/f",
            "ls >> {outside}/f 2>&1",
            "python3 -c \"open('{outside}/f','w')\"",
            'bash -c "echo x > {outside}/f"',
            "echo hi\nrm -rf {outside}",
            "true;rm -rf {outside}",
            "(cd {outside} && touch y)",
            "FOO=1 rm -rf {outside}",
            "env rm -rf {outside}",
            "chmod 777 {outside}/f",
            "rm -rf \\\n{outside}",
            "echo \\\\\nrm -rf {outside}",
            "echo \\\\\ntouch {outside}/x",
        ],
    )
    def test_escape_is_denied(self, layout: dict, command: str) -> None:
        command = command.format(approved=layout["approved"], outside=layout["outside"])
        valid, error = check_bash_directory_boundary(
            command, layout["project"], layout["approved"]
        )
        assert not valid, command
        assert error

    @pytest.mark.parametrize(
        "command",
        [
            "ls -la && echo done",
            "rm -rf build",
            "make test 2>&1 | tail -5",
            "ls > /dev/null 2>&1",
            "echo ok > out.txt",
            "cd {approved} && mkdir -p project/new",
            "mkdir sub && cd sub && touch f",
            'python3 -c "print(1)"',
            'bash -c "ls -la"',
            "git status",
        ],
    )
    def test_inside_is_allowed(self, layout: dict, command: str) -> None:
        command = command.format(approved=layout["approved"])
        valid, error = check_bash_directory_boundary(
            command, layout["project"], layout["approved"]
        )
        assert valid, error


class TestUnsandboxedCommandChain:
    """A sandbox-excluded command takes its whole command line out of the sandbox."""

    @pytest.mark.parametrize(
        "command, excluded",
        [
            ("git; sh evil.sh", ["git"]),
            ("npm; bash ./evil.sh", ["npm"]),
            ("docker && sh evil.sh", ["docker"]),
            ("git 2>/dev/null; sh evil.sh", ["git"]),
            ("timeout 5 make; sh evil.sh", ["make"]),
            ("noglob git; sh evil.sh", ["git"]),
            ("FOO=1 pip | sh evil.sh", ["pip"]),
            ("poetry &\nsh evil.sh", ["poetry"]),
            ("git \\\n; sh evil.sh", ["git"]),
            ("git status && sh evil.sh", ["git:*"]),
            ("git status | sh evil.sh", ["git *"]),
            ("npm run build; sh evil.sh", ["npm run build"]),
            # Unparseable here (quote inside a comment), but bash runs both.
            ("git # it's\nsh evil.sh", ["git"]),
        ],
    )
    def test_chain_with_excluded_command_is_found(
        self, command: str, excluded: list
    ) -> None:
        assert find_unsandboxed_command_chain(command, excluded) is not None

    @pytest.mark.parametrize(
        "command, excluded",
        [
            ("git; sh evil.sh", []),
            ("git", ["git"]),
            ("make", ["make"]),
            ("git status", ["git:*"]),
            ("git status", ["git"]),
            # An exact entry matches only the bare command, so these stay sandboxed.
            ("git status && git diff", ["git"]),
            ("make test 2>&1 | tail -5", ["make"]),
            ("npm test; npm run lint", ["npm"]),
            ("ls && echo done", ["git", "make"]),
            ('bash -c "git; sh evil.sh"', ["git"]),
        ],
    )
    def test_other_commands_are_not_flagged(self, command: str, excluded: list) -> None:
        assert find_unsandboxed_command_chain(command, excluded) is None


class TestHardLinks:
    """A hard link shares the inode, so a write through it changes the target."""

    def test_hard_link_to_project_settings_is_agent_config(self, layout: dict) -> None:
        project = layout["project"]
        (project / ".claude" / "settings.json").write_text("{}")
        (project / "phard.json").hardlink_to(project / ".claude" / "settings.json")
        assert is_agent_config_path("phard.json", project)
        valid, error = check_bash_directory_boundary(
            "echo '{}' > phard.json", project, layout["approved"]
        )
        assert not valid
        assert "Claude Code settings" in error

    def test_hard_linked_file(self, layout: dict) -> None:
        project = layout["project"]
        (project / "a.txt").write_text("x")
        (project / "b.txt").hardlink_to(project / "a.txt")
        (project / "c.txt").write_text("x")
        assert is_hard_linked_file("a.txt", project)
        assert is_hard_linked_file(str(project / "b.txt"), project)
        assert not is_hard_linked_file("c.txt", project)
        assert not is_hard_linked_file("missing.txt", project)

    @pytest.mark.parametrize(
        "command",
        [
            "x=.cla; ln ${x}ude/settings.json h2.json",
            "ln a.txt b.txt",
            "ln -f a.txt b.txt",
            "link a.txt b.txt",
            "cp -l a.txt b.txt",
            "cp -al src dst",
            "cp --link a.txt b.txt",
        ],
    )
    def test_creating_a_hard_link_is_denied(self, layout: dict, command: str) -> None:
        valid, error = check_bash_directory_boundary(
            command, layout["project"], layout["approved"]
        )
        assert not valid, command
        assert "hard link" in error

    @pytest.mark.parametrize(
        "command", ["ln -s src/a b", "ln -sf a.txt b.txt", "cp -a src dst"]
    )
    def test_symlinks_and_copies_are_allowed(self, layout: dict, command: str) -> None:
        valid, error = check_bash_directory_boundary(
            command, layout["project"], layout["approved"]
        )
        assert valid, error
