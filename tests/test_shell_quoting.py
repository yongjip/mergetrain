from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any

from mergetrain.command_runner import _posix_shell, expand_command
from mergetrain.errors import ConfigError
from mergetrain.shell_quoting import expand_path_placeholders

SHELL_PYTHON = sys.executable.replace("\\", "/")
DUMP = f"{SHELL_PYTHON} -c 'import json,sys; print(json.dumps(sys.argv[1:]))'"
SAFE = "/srv/build/repo/.mergetrain/worktrees/demo-1"
_HERE = "after a here-document or here-string"


def shells() -> list[str]:
    """The gate shell, plus dash and bash when present, which lex differently."""

    found = [_posix_shell()]
    if os.name == "posix":
        for name in ("dash", "bash"):
            path = shutil.which(name)
            if path and os.path.realpath(path) not in map(os.path.realpath, found):
                found.append(path)
    return found


def argv(shell: str, command: str) -> list[str]:
    completed = subprocess.run(
        [shell, "-c", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    if completed.returncode != 0:
        raise AssertionError(f"{shell} failed: {completed.stderr}")
    return json.loads(completed.stdout.splitlines()[-1])


def unsafe_path(root: str) -> str:
    """A path that splits, globs, and injects if it is ever left unquoted."""

    name = "My Projects/it's $HOME `x`; a&b #c"
    if os.name == "posix":
        name += ' "q" \\z'
    return str(Path(root) / name)


def expand(command: str, worktree: str) -> str:
    return expand_path_placeholders(command, {"${worktree}": worktree})


class ApostropheInCommentTests(unittest.TestCase):
    """Regression for #219: a quote inside a comment must not flip the scan."""

    def test_placeholders_after_an_apostrophe_in_a_comment(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            worktree = unsafe_path(td)
            for context in ("${worktree}", "'${worktree}'", '"${worktree}"'):
                for comment in (
                    "# don't reuse stale build output\n",
                    "true # it's fine\n",
                    'true;# a "quoted comment\n',
                    "# trailing backslash \\\n",
                ):
                    command = expand(f"{comment}{DUMP} {context}", worktree)
                    for shell in shells():
                        with self.subTest(context=context, comment=comment, shell=shell):
                            self.assertEqual(argv(shell, command), [worktree])

    def test_issue_reproduction_quotes_the_path(self) -> None:
        config = SimpleNamespace(
            repo=PurePosixPath("/home/u/My Projects/repo"),
            project=SimpleNamespace(name="demo"),
            git=SimpleNamespace(integration_ref="origin/main"),
        )
        expanded = expand_command(
            "# don't reuse stale build output\nrm -rf ${worktree}/build",
            config=config,  # type: ignore[arg-type]
            worktree=PurePosixPath("/home/u/My Projects/repo/.mergetrain/wt"),  # type: ignore[arg-type]
        )
        self.assertEqual(
            expanded,
            "# don't reuse stale build output\n"
            "rm -rf '/home/u/My Projects/repo/.mergetrain/wt'/build",
        )

    @unittest.skipUnless(os.name == "posix", "exercises rm through the POSIX shell")
    def test_rm_after_a_comment_deletes_only_the_worktree_build(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            canary = root / "My" / "canary.txt"
            canary.parent.mkdir()
            canary.write_text("keep\n", encoding="utf-8")
            worktree = root / "My Projects" / "wt"
            (worktree / "build").mkdir(parents=True)
            (worktree / "build" / "stale.o").write_text("x", encoding="utf-8")

            command = expand(
                "# don't reuse stale build output\nrm -rf ${worktree}/build",
                str(worktree),
            )
            subprocess.run([_posix_shell(), "-c", command], cwd=root, check=True)

            self.assertTrue(canary.exists())
            self.assertFalse((worktree / "build").exists())


class ShellContextTests(unittest.TestCase):
    def test_every_proven_context_delivers_the_exact_path(self) -> None:
        prefixes = (
            "",
            "x=$(echo \"it's\") # don't\n",
            "x=\"$(echo 'a\"b')\"\n",
            "x=$(echo \\)) # it's\n",
            "x=\"$(echo \")\" ')')\" # it's\n",
            "echo \\' >/dev/null\n",
            "echo 'a\\' >/dev/null\n",
            "echo \"a\\\"b'\" >/dev/null\n",
            "echo a\\\n#b's >/dev/null; true'\n",
            "true \\\n# a comment's\n",
            "echo a#b's >/dev/null 2>&1; true'\n",
            "case a in a) echo 'x';; esac >/dev/null # it's\n",
            "x=$(echo $((1+(2)))) # it's\n",
            "x=${HOME} # it's\n",
        )
        commands = {
            f"{DUMP} ${{worktree}}": lambda path: [path],
            f"{DUMP} '${{worktree}}'": lambda path: [path],
            f'{DUMP} "${{worktree}}"': lambda path: [path],
            f"{DUMP} x${{worktree}}y": lambda path: [f"x{path}y"],
            f'{DUMP} "a ${{worktree}} b"': lambda path: [f"a {path} b"],
            f'{DUMP} "$(printf %s ${{worktree}})"': lambda path: [path],
            f'{DUMP} "$(printf %s "${{worktree}}")"': lambda path: [path],
        }
        with tempfile.TemporaryDirectory() as td:
            worktree = unsafe_path(td)
            for prefix in prefixes:
                for template, expected in commands.items():
                    command = expand(prefix + template, worktree)
                    for shell in shells():
                        with self.subTest(prefix=prefix, template=template, shell=shell):
                            self.assertEqual(argv(shell, command), expected(worktree))

    def test_nested_constructs_end_where_the_shell_ends_them(self) -> None:
        quoted = "'/a b'"
        for prefix in (
            "echo $HOME; ",
            "x=$( (echo a) ); ",
            "x=`echo \\`date\\``; ",
            "x=${y:-`date`}; ",
            "x=${y:-$(echo \"it's\")}; ",
            "x=$((1+$y)); ",
            "x=$((1+$(echo 2))); ",
        ):
            with self.subTest(prefix=prefix):
                self.assertEqual(
                    expand(prefix + "echo ${worktree}", "/a b"), f"{prefix}echo {quoted}"
                )

    def test_escaped_placeholders_stay_literal(self) -> None:
        self.assertEqual(expand(r"echo \${worktree}", "/a b"), r"echo \${worktree}")
        self.assertEqual(expand(r'echo "\${worktree}"', "/a b"), r'echo "\${worktree}"')
        self.assertEqual(expand("# \\${worktree}", "/a b"), "# \\${worktree}")

    def test_comment_placeholders_render_quoted(self) -> None:
        self.assertEqual(expand("# see ${worktree}\n", "/a b"), "# see '/a b'\n")

    def test_one_pass_does_not_expand_a_placeholder_inside_a_value(self) -> None:
        expanded = expand_path_placeholders(
            "echo ${repo} ${worktree}",
            {"${repo}": "/a b/${worktree}", "${worktree}": "/w t"},
        )
        self.assertEqual(expanded, "echo '/a b/${worktree}' '/w t'")

    def test_context_free_paths_expand_verbatim_everywhere(self) -> None:
        for command in (
            "cat <<EOF\n${worktree}\nEOF",
            "cat <<'EOF'\n${worktree}\nEOF",
            "echo `ls ${worktree}`",
            "echo ${x:-${worktree}}",
            "echo $((${worktree}))",
            "echo $'${worktree}'",
            "x=$(case a in a) echo;; esac)\necho ${worktree}",
            "# ${worktree}",
            "echo $${worktree}",
        ):
            with self.subTest(command=command):
                self.assertEqual(expand(command, SAFE), command.replace("${worktree}", SAFE))


class RefusalTests(unittest.TestCase):
    def assert_refused(self, command: str, where: str, path: str = "/My Projects/wt") -> None:
        with self.assertRaises(ConfigError) as caught:
            expand(command, path)
        message = str(caught.exception)
        self.assertIn(where, message)
        self.assertIn("${worktree}", message)
        self.assertIn('"$MERGETRAIN_WORKTREE"', message)

    def test_unprovable_contexts_refuse_a_path_that_needs_quoting(self) -> None:
        cases: dict[str, Any] = {
            "cat <<EOF\n${worktree}\nEOF": _HERE,
            "cat <<'EOF'\nrm -rf ${worktree}\nEOF": _HERE,
            "sh <<EOF\nrm -rf ${worktree}/build\nEOF": _HERE,
            "cat <<${worktree}\nx\n": _HERE,
            "echo `ls ${worktree}`": "inside a backquoted command substitution",
            "echo \"`ls ${worktree}`\"": "inside a backquoted command substitution",
            "echo ${x:-${worktree}}": "inside a ${...} expansion",
            "echo $((${worktree}))": "inside an arithmetic expansion",
            "echo $'${worktree}'": "inside a $'...' string",
            # bash would read the quoted path as "$'...'", a string with escapes.
            "echo $${worktree}": "right after a '$'",
            'echo "$${worktree}"': "right after a '$'",
        }
        for command, where in cases.items():
            with self.subTest(command=command):
                self.assert_refused(command, where)

    def test_constructs_shells_disagree_on_make_later_placeholders_unprovable(self) -> None:
        cases = {
            "x=$(case a in a) echo;; esac)\necho ${worktree}": "a case statement inside $(...)",
            "x=$(echo a # c\n)\necho ${worktree}": "a comment inside $(...)",
            "echo ${x:-'a'}\necho ${worktree}": "quoting inside a ${...} expansion",
            'echo ${x:-"a"}\necho ${worktree}': "quoting inside a ${...} expansion",
            "echo $((1+\"2\"))\necho ${worktree}": "quoting inside an arithmetic expansion",
            "echo $((1+`echo 2`))\necho ${worktree}": "quoting inside an arithmetic expansion",
            "echo $((1+\\2))\necho ${worktree}": "quoting inside an arithmetic expansion",
            "echo ${x:-a)b}\necho ${worktree}": "quoting inside a ${...} expansion",
            "echo `echo 'a'`\necho ${worktree}": "quoting inside a backquoted",
            "echo $'a\\'b'\necho ${worktree}": "a backslash inside a $'...' string",
            "echo $((1+'2'))\necho ${worktree}": "quoting inside an arithmetic expansion",
            "echo $((1)+(2))\necho ${worktree}": "unbalanced parentheses",
            "((x = 1))\necho ${worktree}": "'(('",
            # bash pairs quotes inside these and has no comments there, so the
            # quote after '#' would otherwise leave the path unquoted.
            "echo $[1 # ' ${worktree} ' ]": "a $[...] arithmetic expansion",
            'echo "$[1 # \' ${worktree} \' ]"': "a $[...] arithmetic expansion",
            "echo $(echo $[1 ) # ' ${worktree} ' ])": "a $[...] arithmetic expansion",
            "a[1 # ' ${worktree} ' ]=x": "an array subscript",
            "declare a[1 # ' ${worktree} ' ]=x": "an array subscript",
            # bash reads these '<<' as a shift; the scan once read a here-doc.
            "echo $[1<<2] >/dev/null\necho ${worktree}": "a $[...] arithmetic expansion",
            "a[1<<2]=x\necho ${worktree}": "an array subscript",
            # Joined across the continuation, these spell '<<', '$(', and '(('.
            "cat <\\\n<EOF\nx\nEOF\necho ${worktree}": "a line continuation that joins",
            'echo "$\\\n(echo a)"\necho ${worktree}': "a line continuation that joins",
            "echo $\\\n(echo a)\necho ${worktree}": "a line continuation that joins",
            "x=$(\\\n(echo a))\necho ${worktree}": "a line continuation that joins",
            "(\\\n(x = 1))\necho ${worktree}": "a line continuation that joins",
        }
        for command, where in cases.items():
            with self.subTest(command=command):
                self.assert_refused(command, where)

    def test_every_here_document_or_shift_makes_later_placeholders_unprovable(self) -> None:
        for prefix in (
            "cat <<EOF >/dev/null\nbody\nEOF\n",
            "cat <<'EOF' >/dev/null\nbody\nEOF\n",
            "cat <<-EOF >/dev/null\n\tbody\n\tEOF\n",
            "cat <<A <<B >/dev/null\na\nA\nb\nB\n",
            'cat <<<"it\'s" >/dev/null; ',
            "x=$(cat <<EOF\na)\nEOF\n)\n",
        ):
            with self.subTest(prefix=prefix):
                self.assert_refused(prefix + "echo ${worktree}", _HERE)

    @unittest.skipUnless(os.name == "posix", "runs the command through POSIX shells")
    def test_a_bash_shift_cannot_hide_a_quote_from_a_later_path(self) -> None:
        # 3.1.0 skipped the lines after '$[1<<2]' as a here-document body, so it
        # missed the quote they open and left the path unquoted inside it.
        with tempfile.TemporaryDirectory() as td:
            marker = Path(td) / "ran"
            path = f"/tmp/x $(touch {marker})"
            for first in ("echo $[1<<2] >/dev/null", "a[1<<2]=x"):
                command = f"{first}\n{DUMP} 'it\n2]\nx ${{worktree}}'"
                with self.subTest(first=first):
                    with self.assertRaises(ConfigError):
                        expand(command, path)
                    self.assertEqual(
                        expand(command, SAFE),
                        f"{first}\n{DUMP} 'it\n2]\nx {SAFE}'",
                    )
            self.assertFalse(marker.exists())

    def test_a_newline_in_a_path_cannot_end_a_comment(self) -> None:
        self.assert_refused("# uses ${worktree}\ntrue", "inside a comment", "/a\nrm -rf /")

    def test_a_placeholder_without_an_environment_equivalent_gives_no_advice(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            expand_path_placeholders("cat <<EOF\n${custom}\nEOF", {"${custom}": "/a b"})
        self.assertNotIn("MERGETRAIN", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
