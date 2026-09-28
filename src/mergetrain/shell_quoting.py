"""Expand path placeholders with the quoting their shell context requires.

Gate, verify-hook, and reuse-fingerprint commands run through ``/bin/sh -c``.
``${repo}`` and ``${worktree}`` must each become exactly one path, so every
occurrence is escaped for the context the shell reads it in. The scanner below
lexes the command the way POSIX ``sh`` does -- quotes, backslashes, comments,
``$(...)`` command substitutions, and here-documents -- to find that context.

Some contexts cannot be escaped with certainty. A here-document body is data
whose consumer may itself be a shell; backquotes re-parse their contents; and
``${...}``, ``$((...))``, and ``$'...'`` differ between shells. A path that
needs quoting is refused there, and anywhere after a construct whose extent the
scanner cannot pin down, instead of being guessed. A path made only of
characters that are literal in every context is inserted verbatim anywhere.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass

from .errors import ConfigError

# Literal in every shell context: unquoted words, both kinds of quotes,
# comments, here-documents, and every expansion. It is the set shlex.quote
# leaves bare, so such a value renders the same way in every context.
_CONTEXT_FREE = re.compile(r"[A-Za-z0-9_@%+=:,./-]+")
_BLANKS = " \t"
# Characters that end an unquoted word; a '#' right after one starts a comment.
_WORD_END = " \t\n;&|()<>"
# After a backslash inside double quotes, only these characters are escaped.
_DOUBLE_QUOTE_ESCAPABLE = '$`"\\\n'
# After a backslash in an unquoted here-document body, only these are escaped.
_HERE_DOCUMENT_ESCAPABLE = "$`\\\n"
# A ${...} that is only a parameter name cannot span lines.
_SIMPLE_PARAMETER = re.compile(r"\$\{(?:[A-Za-z_][A-Za-z0-9_]*|[0-9]+|[#?$!@*-])\}")
# A line continuation after one of these can spell '$(', '<<', or '((' across
# the joined lines, which the scan has already read as separate characters.
_JOINABLE = "$<("
_JOINED = "a line continuation that joins '$', '<', or '(' to the next line"

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


def _ends_with_escaped_newline(line: str) -> bool:
    """Whether an unquoted here-document line continues onto the next one."""

    trailing = len(line) - len(line.rstrip("\\"))
    return trailing % 2 == 1


@dataclass(frozen=True, slots=True)
class _HereDocument:
    delimiter: str
    strip_tabs: bool
    quoted: bool


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
        # Here-documents waiting for the next newline, one list per nesting level.
        self.pending: list[list[_HereDocument]] = []

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
        pending: list[_HereDocument] = []
        self.pending.append(pending)
        word_start = True
        word = ""
        plain = True
        depth = 0
        try:
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
                if char == "\n":
                    self.copy(1)
                    if any(outer for outer in self.pending[:-1]):
                        self.doubt("a here-document whose body starts inside $(...)")
                    for document in pending:
                        self.here_document_body(document)
                    pending.clear()
                elif char == "(":
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
                elif self.at("<<<"):
                    self.copy(3)
                elif self.at("<<"):
                    self.here_document_operator(pending)
                else:
                    self.copy(1)
        finally:
            self.pending.pop()

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
        else:
            self.copy(1)
            for key, value in self.values.items():
                if self.at(key) and not _CONTEXT_FREE.fullmatch(value):
                    # The '$' would join the quoting the path gets, and bash
                    # reads "$'...'" as a string with escapes.
                    raise self.refusal(key, value, "right after a '$'")

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

    # -- here-documents -------------------------------------------------------

    def here_document_operator(self, pending: list[_HereDocument]) -> None:
        text = self.text
        if len(self.pending) > 1:
            # bash 3.2 closes a substitution at a ')' inside the body.
            self.doubt("a here-document inside $(...)")
        self.copy(2)
        strip_tabs = self.at("-")
        if strip_tabs:
            self.copy(1)
        while self.pos < len(text) and text[self.pos] in _BLANKS:
            self.copy(1)
        start = self.pos
        delimiter: list[str] = []
        quoted = False
        quote = ""
        while self.pos < len(text) and (quote or text[self.pos] not in _WORD_END):
            before = len(self.out)
            if self.placeholder("a here-document delimiter"):
                self.doubt("a here-document delimiter built from a placeholder")
                delimiter.extend(self.out[before:])
                continue
            char = text[self.pos]
            if char == quote:
                quote = ""
                self.copy(1)
                continue
            if not quote and char in "'\"":
                quoted = True
                quote = char
                self.copy(1)
                continue
            if quote == '"' and char in "$`":
                self.doubt("a here-document delimiter with escapes")
            if char == "\\" and quote != "'":
                quoted = True
                if quote or self.at("\\\n"):
                    self.doubt("a here-document delimiter with escapes")
                delimiter.append(text[self.pos + 1 : self.pos + 2])
                self.copy(2)
                continue
            if not quote and char in "$`":
                self.doubt("a here-document delimiter that contains an expansion")
            delimiter.append(char)
            self.copy(1)
        if self.pos == start:
            self.doubt("a here-document operator without a delimiter")
        pending.append(_HereDocument("".join(delimiter), strip_tabs, quoted))

    def here_document_body(self, document: _HereDocument) -> None:
        """Copy one body up to and including its delimiter line."""

        text = self.text
        while self.pos < len(text):
            parts: list[str] = []
            while True:
                end = text.find("\n", self.pos)
                if end < 0:
                    end = len(text)
                before = len(self.out)
                self.here_document_text(end, expands=not document.quoted)
                line = "".join(self.out[before:])
                continued = (
                    not document.quoted
                    and end < len(text)
                    and _ends_with_escaped_newline(line)
                )
                if not continued:
                    parts.append(line)
                    break
                # bash joins the lines before it looks for the delimiter, and
                # dash does not, so the shells can end the body in different
                # places.
                self.doubt("a continued line inside a here-document")
                parts.append(line[:-1])
                self.copy(1)
            logical = "".join(parts)
            if document.strip_tabs:
                logical = logical.lstrip("\t")
            self.copy(1)
            if logical == document.delimiter:
                return

    def here_document_text(self, end: int, *, expands: bool) -> None:
        text = self.text
        while self.pos < end:
            if self.placeholder("a here-document"):
                continue
            if expands:
                char = text[self.pos]
                if char == "\\" and self.pos + 1 < end:
                    if text[self.pos + 1] in _HERE_DOCUMENT_ESCAPABLE:
                        self.copy(2)
                        continue
                elif (char == "`" or self.at("$(")) or (
                    self.at("${") and not _SIMPLE_PARAMETER.match(text, self.pos)
                ):
                    # dash lets a substitution run past the delimiter line;
                    # bash ends the body there first.
                    self.doubt("an expansion inside a here-document")
            self.copy(1)


def expand_path_placeholders(command: str, values: Mapping[str, str]) -> str:
    """Replace each placeholder with its value, escaped for its shell context.

    Raises ``ConfigError`` when a value that needs quoting appears where the
    shell's quoting cannot be proven.
    """

    return _Scanner(command, values).expand()
