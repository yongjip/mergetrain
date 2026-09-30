"""Expand path placeholders with the quoting their shell context requires.

Gate, verify-hook, and reuse-fingerprint commands run through ``/bin/sh -c``.
``${repo}`` and ``${worktree}`` must each become exactly one path, so every
occurrence is escaped for the context the shell reads it in. The scanner below
lexes the command the way POSIX ``sh`` does -- quotes, backslashes, comments,
and ``$(...)`` command substitutions -- to find that context.

Some contexts cannot be escaped with certainty. Backquotes re-parse their
contents, and ``${...}``, ``$((...))``, and ``$'...'`` differ between shells. A
``<<`` may open a here-document, whose body is data that the scan cannot follow,
or may be a shift inside bash arithmetic such as ``$[1<<2]``. A path that needs
quoting is refused inside those constructs, and anywhere after one whose extent
the scanner cannot pin down, instead of being guessed. A path made only of
characters that are literal in every context is inserted verbatim anywhere.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping

from .errors import ConfigError

# Literal in every shell context: unquoted words, both kinds of quotes,
# comments, here-documents, and every expansion. It is the set shlex.quote
# leaves bare, so such a value renders the same way in every context.
_CONTEXT_FREE = re.compile(r"[A-Za-z0-9_@%+=:,./-]+")
# Characters that end an unquoted word; a '#' right after one starts a comment.
_WORD_END = " \t\n;&|()<>"
# A shell variable name; bash reads 'name[' as the start of an array subscript.
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# After a backslash inside double quotes, only these characters are escaped.
_DOUBLE_QUOTE_ESCAPABLE = '$`"\\\n'
# A line continuation after one of these can spell '$(', '<<', or '((' across
# the joined lines, which the scan has already read as separate characters.
_JOINABLE = "$<("
_JOINED = "a line continuation that joins '$', '<', or '(' to the next line"
_PROCESS_ID_BRACKET = "'$$(', '$${', or '$$[' inside double quotes"

# Contexts whose quoting rules are exact; any other context names a construct.
_UNQUOTED = "unquoted"
_SINGLE = "single"
_DOUBLE = "double"
_COMMENT = "comment"

ENVIRONMENT_EQUIVALENTS = {
    "${repo}": "MERGETRAIN_REPO",
    "${worktree}": "MERGETRAIN_WORKTREE",
}


def _escape_double_quoted(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "\\$")
        .replace("`", "\\`")
    )


class _Scanner:
    """Copy a command while escaping each placeholder for its shell context."""

    def __init__(self, text: str, values: Mapping[str, str]) -> None:
        self.text = text
        self.values = values
        self.pos = 0
        self.out: list[str] = []
        # Once set, the scanner can no longer prove where later text lands, so
        # every later placeholder that needs quoting is refused.
        self.unproven = ""

    def expand(self) -> str:
        self.code(nested=False)
        return "".join(self.out)

    # -- helpers -------------------------------------------------------------

    def copy(self, count: int) -> None:
        self.out.append(self.text[self.pos : self.pos + count])
        self.pos = min(len(self.text), self.pos + count)

    def at(self, prefix: str) -> bool:
        return self.text.startswith(prefix, self.pos)

    def doubt(self, construct: str) -> None:
        if not self.unproven:
            self.unproven = construct

    def placeholder(self, context: str) -> bool:
        """Expand a placeholder at the cursor, if one starts there."""

        for key, value in self.values.items():
            if self.at(key):
                self.out.append(self.render(key, value, context))
                self.pos += len(key)
                return True
        return False

    def render(self, key: str, value: str, context: str) -> str:
        if _CONTEXT_FREE.fullmatch(value):
            return value
        if self.unproven:
            raise self.refusal(key, value, f"after {self.unproven}")
        if context == _UNQUOTED:
            return shlex.quote(value)
        if context == _SINGLE:
            return value.replace("'", "'\"'\"'")
        if context == _DOUBLE:
            return _escape_double_quoted(value)
        if context == _COMMENT:
            if "\n" in value:
                # A newline would end the comment and run the rest of the path.
                raise self.refusal(key, value, "inside a comment")
            return shlex.quote(value)
        raise self.refusal(key, value, f"inside {context}")

    @staticmethod
    def refusal(key: str, value: str, where: str) -> ConfigError:
        variable = ENVIRONMENT_EQUIVALENTS.get(key)
        advice = f'; use "${variable}" there instead' if variable else ""
        return ConfigError(
            f"cannot safely expand {key} {where}: mergetrain cannot prove how the "
            f"shell quotes it there, and the path {value!r} needs quoting{advice}"
        )

    # -- shell code ----------------------------------------------------------

    def code(self, *, nested: bool) -> None:
        """Scan shell code: the whole command, or the body of ``$(...)``.

        A nested scan returns at the ``)`` that closes its substitution and
        leaves it for the caller.
        """

        text = self.text
        word_start = True
        word = ""
        plain = True
        depth = 0
        while self.pos < len(text):
            if self.placeholder(_UNQUOTED):
                word_start = plain = False
                continue
            char = text[self.pos]
            if char == "\\":
                if self.at("\\\n"):
                    # Line continuation joins lines without ending the word.
                    if self.pos and text[self.pos - 1] in _JOINABLE:
                        self.doubt(_JOINED)
                    self.copy(2)
                    continue
                self.copy(2)
                word_start = plain = False
                continue
            if char in "'\"`$":
                if char == "'":
                    self.single_quoted()
                elif char == '"':
                    self.double_quoted()
                elif char == "`":
                    self.backquoted()
                else:
                    self.dollar(quoted=False)
                word_start = plain = False
                continue
            if char == "#" and word_start:
                if nested:
                    # bash 3.2, macOS's /bin/sh, misses comments while it
                    # looks for the ')' that closes a substitution.
                    self.doubt("a comment inside $(...)")
                self.comment()
                continue
            if char not in _WORD_END:
                if char == "[" and plain and _NAME.fullmatch(word):
                    # bash reads 'name[...]' as an array subscript, which pairs
                    # quotes and has no comments, unlike an ordinary word.
                    self.doubt("an array subscript")
                if plain:
                    word += char
                self.copy(1)
                word_start = False
                continue

            if nested and plain and word == "case":
                # A case pattern's ')' does not close the substitution, and
                # a pattern scan cannot tell which ')' does.
                self.doubt("a case statement inside $(...)")
            word = ""
            plain = True
            word_start = True
            if char == "(":
                if self.at("(("):
                    # bash reads '((' as arithmetic, dash as two subshells.
                    self.doubt("'(('")
                depth += 1
                self.copy(1)
            elif char == ")":
                if nested:
                    if depth == 0:
                        return
                    depth -= 1
                self.copy(1)
            elif self.at("<<"):
                # A here-document or here-string, or a shift in bash
                # arithmetic ('$[1<<2]', 'a[1<<2]=x'): the shells read the
                # following text in different ways.
                self.doubt("a here-document or here-string")
                self.copy(2)
            else:
                self.copy(1)

    def comment(self) -> None:
        text = self.text
        while self.pos < len(text) and text[self.pos] != "\n":
            if self.placeholder(_COMMENT):
                continue
            if text[self.pos] == "\\" and not self.at("\\\n"):
                # The shell ignores this backslash, but keeping the escaped
                # placeholder literal stays safe if the comment is misread.
                self.copy(2)
                continue
            self.copy(1)

    def single_quoted(self) -> None:
        self.copy(1)
        while self.pos < len(self.text):
            if self.placeholder(_SINGLE):
                continue
            closing = self.text[self.pos] == "'"
            self.copy(1)
            if closing:
                return

    def double_quoted(self) -> None:
        text = self.text
        self.copy(1)
        while self.pos < len(text):
            if self.placeholder(_DOUBLE):
                continue
            char = text[self.pos]
            if char == '"':
                self.copy(1)
                return
            if char == "\\":
                following = text[self.pos + 1 : self.pos + 2]
                if following == "\n" and text[self.pos - 1] == "$":
                    self.doubt(_JOINED)
                self.copy(2 if following and following in _DOUBLE_QUOTE_ESCAPABLE else 1)
            elif char == "`":
                self.backquoted()
            elif char == "$":
                self.dollar(quoted=True)
            else:
                self.copy(1)

    def dollar(self, *, quoted: bool) -> None:
        """Scan an expansion that starts with ``$`` (never a placeholder)."""

        if self.at("$(("):
            self.arithmetic()
        elif self.at("$("):
            self.copy(2)
            self.code(nested=True)
            self.copy(1)
        elif self.at("${"):
            self.parameter()
        elif not quoted and self.at("$'"):
            self.ansi_c_quoted()
        elif self.at("$["):
            # bash arithmetic: it pairs quotes and has no comments inside, so
            # a '#' or ')' in it would mislead this scan about what follows.
            self.doubt("a $[...] arithmetic expansion")
            self.copy(2)
        else:
            self.copy(1)
            for key, value in self.values.items():
                if self.at(key) and not _CONTEXT_FREE.fullmatch(value):
                    # The '$' would join the quoting the path gets, and bash
                    # reads "$'...'" as a string with escapes.
                    raise self.refusal(key, value, "right after a '$'")
            if self.at("$") and not any(self.at(key) for key in self.values):
                # '$$' is the shell's process ID, so this '$' starts nothing:
                # dash, and bash when it expands the word, read the '(' of
                # '$$(' as text. Inside double quotes, though, bash's parser
                # opens a '$(', '${', or '$[' there to find the closing quote.
                self.copy(1)
                if quoted and any(self.at(bracket) for bracket in "({["):
                    self.doubt(_PROCESS_ID_BRACKET)

    # -- constructs whose contents stay unproven ------------------------------

    def backquoted(self) -> None:
        construct = "a backquoted command substitution"
        text = self.text
        self.copy(1)
        while self.pos < len(text):
            if self.placeholder(construct):
                continue
            char = text[self.pos]
            if char == "\\":
                self.copy(2)
                continue
            if char == "`":
                self.copy(1)
                return
            if char in "'\"" or self.at("$(") or self.at("<<"):
                # Shells disagree on where a backquote nested in these ends.
                self.doubt(f"quoting inside {construct}")
            self.copy(1)

    def parameter(self) -> None:
        construct = "a ${...} expansion"
        text = self.text
        self.copy(2)
        while self.pos < len(text):
            if self.placeholder(construct):
                continue
            char = text[self.pos]
            if char == "}":
                self.copy(1)
                return
            if char in "'\"\\{()\n":
                # Quotes, braces, and parentheses inside ${...} mean different
                # things in different shells, so where it ends is unproven.
                self.doubt(f"quoting inside {construct}")
                if char == "'":
                    self.single_quoted()
                elif char == '"':
                    self.double_quoted()
                else:
                    self.copy(2 if char == "\\" else 1)
            elif char == "`":
                self.backquoted()
            elif char == "$":
                self.dollar(quoted=True)
            else:
                self.copy(1)

    def arithmetic(self) -> None:
        construct = "an arithmetic expansion"
        text = self.text
        self.copy(3)
        depth = 0
        while self.pos < len(text):
            if self.placeholder(construct):
                continue
            char = text[self.pos]
            if char == "(":
                depth += 1
                self.copy(1)
            elif char == ")":
                if depth:
                    depth -= 1
                    self.copy(1)
                elif self.at("))"):
                    self.copy(2)
                    return
                else:
                    self.doubt(f"unbalanced parentheses inside {construct}")
                    self.copy(1)
                    return
            elif char in "'\"\\`":
                self.doubt(f"quoting inside {construct}")
                if char == "'":
                    self.single_quoted()
                elif char == '"':
                    self.double_quoted()
                elif char == "`":
                    self.backquoted()
                else:
                    self.copy(2)
            elif char == "$":
                self.dollar(quoted=True)
            else:
                self.copy(1)

    def ansi_c_quoted(self) -> None:
        construct = "a $'...' string"
        self.copy(2)
        while self.pos < len(self.text):
            if self.placeholder(construct):
                continue
            char = self.text[self.pos]
            if char == "\\":
                # bash reads \' as an escaped quote; older dash ends the string.
                self.doubt(f"a backslash inside {construct}")
                self.copy(2)
                continue
            self.copy(1)
            if char == "'":
                return


def expand_path_placeholders(command: str, values: Mapping[str, str]) -> str:
    """Replace each placeholder with its value, escaped for its shell context.

    Raises ``ConfigError`` when a value that needs quoting appears where the
    shell's quoting cannot be proven.
    """

    return _Scanner(command, values).expand()
