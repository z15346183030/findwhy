#!/usr/bin/env python3
"""findwhy - find files, and explain every path that did not show up.

The tool does the same job as any file finder: walk a tree, skip what is
ignored, print what matches.  The difference is that it never loses the
information about *why* something was skipped.  Every skipped path keeps the
rule that removed it - which file, which line, which pattern - so a search that
returns nothing can always be turned into a search that returns an answer.

Standard library only.  No configuration file.  No environment variables.

    python findwhy.py build
    python findwhy.py --skipped build
    python findwhy.py --explain src/build/out.log
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys
from dataclasses import dataclass

__version__ = "1.0.0"

# Windows paths are compared case-insensitively, the same way git does when
# core.ignorecase is on (its default there).  Ignore patterns are therefore
# compiled with re.IGNORECASE and paths are folded before matching.
CASE_INSENSITIVE = os.name == "nt"
REGEX_FLAGS = re.IGNORECASE if CASE_INSENSITIVE else 0

IGNORE_FILE_NAMES = (".gitignore", ".ignore", ".findwhyignore")


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------


def norm(path):
    """Normalise a path for pattern matching: forward slashes, folded case."""
    text = path.replace("\\", "/")
    return text.lower() if CASE_INSENSITIVE else text


def is_hidden_name(name):
    return name.startswith(".") and name not in (".", "..")


def show(relative):
    """Paths are printed with forward slashes everywhere, like fd does."""
    return relative


def human_size(count):
    for unit in ("B", "KB", "MB", "GB"):
        if count < 1024 or unit == "GB":
            if unit == "B":
                return "%d B" % count
            return "%.1f %s" % (count, unit)
        count /= 1024.0
    return "%d B" % count


# ---------------------------------------------------------------------------
# pattern translation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PatternSpec:
    """One logical line of an ignore file, already split into its parts."""

    text: str
    body: str
    negated: bool
    dir_only: bool
    anchored: bool


def _strip_trailing_spaces(line):
    """Trailing spaces are ignored by git unless the last one is escaped."""
    end = len(line)
    while end > 0 and line[end - 1] == " ":
        backslashes = 0
        probe = end - 2
        while probe >= 0 and line[probe] == "\\":
            backslashes += 1
            probe -= 1
        if backslashes % 2 == 1:
            break
        end -= 1
    return line[:end]


def parse_pattern(line):
    """Turn a raw ignore-file line into a PatternSpec, or None to skip it."""
    line = line.rstrip("\r\n")
    if not line:
        return None
    if line.startswith("#"):
        return None
    text = _strip_trailing_spaces(line)
    if not text:
        return None

    body = text
    negated = False
    if body.startswith("!"):
        negated = True
        body = body[1:]
    elif body.startswith("\\!") or body.startswith("\\#"):
        body = body[1:]

    dir_only = body.endswith("/")
    if dir_only:
        body = body.rstrip("/")

    if not body:
        return None

    anchored = body.startswith("/") or "/" in body
    if body.startswith("/"):
        body = body[1:]
    if not body:
        return None

    return PatternSpec(text=text, body=body, negated=negated, dir_only=dir_only, anchored=anchored)


def glob_to_regex(pattern, anchored):
    """Translate a gitignore-style glob into an anchored regular expression.

    The rules being followed, in order of how often they surprise people:

    * ``*`` and ``?`` never cross a ``/``;
    * a pattern with no ``/`` in it (other than a trailing one) matches a name
      at any depth, which is what the ``(?:.*/)?`` prefix buys;
    * ``**/`` means "zero or more directories", so ``**/foo`` is the same as a
      bare ``foo``;
    * ``/**`` at the end means "everything inside", never the directory itself.
    """
    if pattern.startswith("**/"):
        pattern = pattern[3:]
        anchored = False

    out = ["^"] if anchored else ["(?:.*/)?"]
    index = 0
    length = len(pattern)
    while index < length:
        char = pattern[index]
        if char == "*":
            if pattern.startswith("**", index):
                if index + 2 == length:
                    out.append(".*")
                    index += 2
                    continue
                if pattern.startswith("**/", index):
                    out.append("(?:.*/)?")
                    index += 3
                    continue
                out.append("[^/]*")
                index += 2
                continue
            out.append("[^/]*")
            index += 1
            continue
        if char == "?":
            out.append("[^/]")
            index += 1
            continue
        if char == "[":
            close = index + 1
            if close < length and pattern[close] in "!^":
                close += 1
            if close < length and pattern[close] == "]":
                close += 1
            while close < length and pattern[close] != "]":
                close += 1
            if close >= length:
                out.append(r"\[")
                index += 1
                continue
            inner = pattern[index + 1 : close]
            if inner.startswith("!"):
                inner = "^" + inner[1:]
            out.append("[" + inner.replace("\\", "\\\\") + "]")
            index = close + 1
            continue
        if char == "\\":
            if index + 1 < length:
                out.append(re.escape(pattern[index + 1]))
                index += 2
            else:
                out.append(re.escape("\\"))
                index += 1
            continue
        out.append(re.escape(char))
        index += 1
    out.append("$")
    return "".join(out)


# ---------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    """One pattern, together with everything needed to blame it later."""

    text: str
    source: str
    line: int
    negated: bool
    dir_only: bool
    regex: object
    base: str

    def matches(self, path_norm, is_dir):
        if self.dir_only and not is_dir:
            return False
        if self.base and path_norm.startswith(self.base + "/"):
            relative = path_norm[len(self.base) + 1 :]
        else:
            relative = path_norm
        return self.regex.match(relative) is not None

    @property
    def where(self):
        if self.line:
            return "%s:%d" % (self.source, self.line)
        return self.source


def load_rules_file(path, display, base_dir):
    """Read one ignore file.  Unreadable or unparsable lines are skipped."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError:
        return ()

    text = raw.decode("utf-8", "replace")
    if text.startswith("\ufeff"):
        text = text[1:]

    base = norm(base_dir)
    rules = []
    for lineno, line in enumerate(text.splitlines(), 1):
        spec = parse_pattern(line)
        if spec is None:
            continue
        try:
            regex = re.compile(glob_to_regex(spec.body, spec.anchored), REGEX_FLAGS)
        except re.error:
            continue
        rules.append(
            Rule(
                text=spec.text,
                source=display,
                line=lineno,
                negated=spec.negated,
                dir_only=spec.dir_only,
                regex=regex,
                base=base,
            )
        )
    return tuple(rules)


def _looks_absolute(text):
    return text.startswith(("/", "\\")) or re.match(r"^[a-zA-Z]:[\\/]", text) is not None


def load_cli_rules(patterns, base_dir):
    """``--exclude`` patterns, all anchored at the search root.

    Absolute patterns are rewritten relative to the root instead of being
    rejected, so ``-e /home/me/project/vendor/`` does what the user meant.
    """
    base = norm(base_dir)
    rules = []
    for text in patterns:
        raw = text.strip()
        body_text = text
        if _looks_absolute(raw):
            try:
                relative = os.path.relpath(os.path.abspath(raw), os.path.abspath(base_dir))
            except ValueError:
                relative = None
            if relative is None or relative.startswith(".."):
                sys.stderr.write("findwhy: --exclude %s is outside %s and can never match\n" % (text, base_dir))
                continue
            body_text = norm(relative)
            if raw.endswith(("/", "\\")) and not body_text.endswith("/"):
                body_text += "/"

        spec = parse_pattern(body_text)
        if spec is None:
            continue
        try:
            regex = re.compile(glob_to_regex(spec.body, spec.anchored), REGEX_FLAGS)
        except re.error:
            continue
        rules.append(
            Rule(
                text=spec.text,
                source="--exclude",
                line=0,
                negated=spec.negated,
                dir_only=spec.dir_only,
                regex=regex,
                base=base,
            )
        )
    return tuple(rules)


def default_global_ignore():
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "findwhy", "ignore")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "findwhy", "ignore")


# ---------------------------------------------------------------------------
# decisions
# ---------------------------------------------------------------------------


@dataclass
class Verdict:
    """The answer for one path, plus the rule that produced it."""

    excluded: bool
    kind: str = "none"
    rule: Rule = None


class Finder:
    def __init__(self, root, opts):
        self.root = os.path.abspath(root)
        self.root_norm = norm(self.root)
        self.opts = opts
        self._file_cache = {}
        self.skipped = {}
        self.stats = {"visited": 0, "emitted": 0, "filtered": 0, "unreadable": 0}

        global_path = opts.global_ignore if opts.global_ignore else default_global_ignore()
        if opts.global_ignore == "-":
            self.global_rules = ()
            self.global_path = None
        else:
            self.global_rules = load_rules_file(global_path, "(global)", self.root)
            self.global_path = global_path
        self.cli_rules = load_cli_rules(opts.exclude, self.root)
        self.display_prefix = self._display_prefix(root)
        self.parent_rules = self.load_parent_rules() if opts.parent else ()

    def _display_prefix(self, root):
        """What to put in front of a result path.

        An explicit root is echoed back, the way find and fd do it, so the
        output can be piped straight into another command.  A root of "."
        prints bare relative paths instead of "./x".
        """
        if os.path.abspath(root) == os.path.abspath("."):
            return ""
        return root.replace("\\", "/").rstrip("/")

    def load_parent_rules(self):
        """Ignore files above the search root, outermost first.

        Off by default: an explicit root is treated as the whole world, which
        is what makes searching inside an ignored directory work.
        """
        rules = []
        chain = []
        current = os.path.dirname(self.root)
        for _ in range(64):
            if not current or current == os.path.dirname(current):
                break
            chain.append(current)
            current = os.path.dirname(current)
        for directory in reversed(chain):
            for name in IGNORE_FILE_NAMES:
                path = os.path.join(directory, name)
                if os.path.isfile(path):
                    rules.extend(load_rules_file(path, path, directory))
        return tuple(rules)

    # -- rule stacks ------------------------------------------------------

    def load_dir_files(self, dir_abs, names=None):
        """Ignore files in one directory.

        ``names`` is the scandir listing of that directory when the walker
        already has it, which saves three stat calls per directory.
        """
        rules = []
        for name in IGNORE_FILE_NAMES:
            if names is not None and name not in names:
                continue
            path = os.path.join(dir_abs, name)
            cached = self._file_cache.get(path)
            if cached is None:
                if names is None and not os.path.isfile(path):
                    continue
                cached = load_rules_file(path, self.display_source(path), dir_abs)
                self._file_cache[path] = cached
            rules.extend(cached)
        return tuple(rules)

    def display_source(self, path):
        relative = self.relative_real(path)
        if relative is None:
            return path
        return relative if relative else os.path.basename(path)

    def relative_real(self, path):
        """Path relative to the root, original case, forward slashes."""
        candidate = path.replace("\\", "/")
        root = self.root.replace("\\", "/")
        if candidate.lower() == root.lower():
            return ""
        if candidate.lower().startswith(root.lower() + "/"):
            return candidate[len(root) + 1 :]
        return None

    def relative_norm(self, path):
        """Path relative to the search root, posix, or None when outside."""
        candidate = norm(path)
        if candidate == self.root_norm:
            return ""
        if candidate.startswith(self.root_norm + "/"):
            return candidate[len(self.root_norm) + 1 :]
        return None

    def rules_for(self, carried=()):
        """Global rules, then ignore files above and below the root, then --exclude."""
        return tuple(self.global_rules) + tuple(self.parent_rules) + tuple(carried) + tuple(self.cli_rules)

    def root_blocked(self):
        """With --parent, the root itself can sit inside an ignored directory."""
        if not self.opts.parent:
            return None
        verdict = self.rule_verdict(self.root, True, self.rules_for(()))
        return verdict if verdict.excluded else None

    def decide(self, path):
        """The verdict for one path, ancestors included.

        The walker never has to ask this - it never descends into an excluded
        directory - but --explain and any external check do.
        """
        path = os.path.abspath(path)
        relative = self.relative_real(path)
        if relative is None:
            raise ValueError("%s is outside %s" % (path, self.root))

        parts = relative.split("/") if relative else []

        carried = list(self.load_dir_files(self.root))
        current = self.root
        for part in parts[:-1]:
            current = os.path.join(current, part)
            verdict = self.rule_verdict(current, True, self.rules_for(tuple(carried)))
            if verdict.excluded:
                return Verdict(True, "parent", verdict.rule)
            carried.extend(self.load_dir_files(current))

        is_dir = os.path.isdir(path)
        if not is_dir and os.path.islink(path):
            is_dir = False
        verdict = self.rule_verdict(path, is_dir, self.rules_for(tuple(carried)))
        if verdict.excluded:
            return verdict

        # A concrete rule wins over the generic dotfile gate when both apply:
        # the rule is the thing the user can act on.
        for part in parts:
            if is_hidden_name(part) and not self.opts.hidden:
                return Verdict(True, "hidden", None)
        return verdict

    # -- verdicts ---------------------------------------------------------

    def rule_verdict(self, path, is_dir, rules):
        """Last matching rule wins.  Scanning backwards finds it first."""
        target = norm(path)
        for rule in reversed(rules):
            if rule.matches(target, is_dir):
                return Verdict(not rule.negated, "rule", rule)
        return Verdict(False, "none", None)

    def all_hits(self, path, is_dir, rules):
        target = norm(path)
        return [rule for rule in rules if rule.matches(target, is_dir)]

    def hidden_verdict(self, name):
        if not is_hidden_name(name):
            return None
        if self.opts.hidden:
            return None
        return Verdict(True, "hidden", None)

    # -- walking ----------------------------------------------------------

    def record_skip(self, verdict, example):
        key = (verdict.kind, verdict.rule.source if verdict.rule else "", verdict.rule.line if verdict.rule else 0,
               verdict.rule.text if verdict.rule else "")
        entry = self.skipped.get(key)
        if entry is None:
            self.skipped[key] = [1, example, verdict]
        else:
            entry[0] += 1
            if norm(example) < norm(entry[1]):
                entry[1] = example

    def walk(self):
        results = []
        max_depth = self.opts.max_depth if self.opts.max_depth is not None else 1 << 30
        min_depth = self.opts.min_depth or 0
        follow = self.opts.follow
        absolute = self.opts.absolute
        display_prefix = self.display_prefix
        root_length = len(self.root)
        prefix_length = len(self.root_norm) + 1
        fold = CASE_INSENSITIVE

        if self.root_blocked() is not None:
            return []

        stack = [(self.root, 0, (), frozenset())]
        while stack:
            dir_abs, depth, carried, seen = stack.pop()
            try:
                with os.scandir(dir_abs) as scanner:
                    entries = list(scanner)
            except OSError:
                self.stats["unreadable"] += 1
                continue

            # The listing is already in hand, so the ignore files cost a set
            # lookup each instead of a stat call each.
            carried = carried + self.load_dir_files(dir_abs, {item.name for item in entries})
            rules = self.rules_for(carried)

            descend = []
            for entry in entries:
                self.stats["visited"] += 1
                path = entry.path
                try:
                    is_symlink = entry.is_symlink()
                    is_dir = entry.is_dir(follow_symlinks=follow)
                    is_file = entry.is_file(follow_symlinks=follow)
                except OSError:
                    self.stats["unreadable"] += 1
                    continue
                entry_depth = depth + 1

                # One normalisation per entry, reused by every rule.  Doing it
                # inside each rule was the second costliest thing in the profile.
                posix = path.replace("\\", "/")
                target = posix.lower() if fold else posix

                if rules and not self.opts.ignored:
                    excluded = None
                    for rule in reversed(rules):
                        if rule.matches(target, is_dir):
                            if not rule.negated:
                                excluded = rule
                            break
                    if excluded is not None:
                        self.record_skip(Verdict(True, "rule", excluded), path)
                        continue

                hidden = self.hidden_verdict(entry.name)
                if hidden is not None:
                    self.record_skip(hidden, path)
                    continue

                # Filters decide whether a path is *reported*.  They never
                # decide whether a directory is walked into - `-t f` still has
                # to descend through directories to find the files inside.
                keep = (
                    min_depth <= entry_depth <= max_depth
                    and self.type_allowed(is_dir, is_file, is_symlink)
                    and self.pattern_matches(entry.name, target[prefix_length:])
                )
                if keep:
                    self.stats["emitted"] += 1
                    if absolute:
                        results.append(posix)
                    elif display_prefix:
                        results.append(display_prefix + "/" + posix[prefix_length:])
                    else:
                        results.append(posix[prefix_length:])
                else:
                    self.stats["filtered"] += 1

                if is_dir and entry_depth < max_depth:
                    if follow:
                        try:
                            info = entry.stat(follow_symlinks=True)
                        except OSError:
                            continue
                        key = (info.st_dev, info.st_ino)
                        if key in seen:
                            continue
                        descend.append((path, entry_depth, carried, seen | {key}))
                    else:
                        descend.append((path, entry_depth, carried, seen))
            stack.extend(reversed(descend))
        results.sort(key=str.lower)
        return results

    def type_allowed(self, is_dir, is_file, is_symlink):
        wanted = self.opts.types
        if not wanted:
            return True
        if is_symlink and "l" in wanted:
            return True
        if is_dir and "d" in wanted:
            return True
        if is_file and "f" in wanted and (not is_symlink or self.opts.follow):
            return True
        return False

    def pattern_matches(self, name, relative):
        matcher = self.opts.matcher
        if matcher is None:
            return True
        hit = matcher(name, relative)
        return not hit if self.opts.invert else hit

    # -- output helpers ---------------------------------------------------

    def output_path(self, path):
        if self.opts.absolute:
            return path.replace("\\", "/")
        relative = self.relative_real(path)
        if relative is None:
            return path.replace("\\", "/")
        if not self.display_prefix:
            return relative or "."
        return self.display_prefix + "/" + relative if relative else self.display_prefix


# ---------------------------------------------------------------------------
# search pattern
# ---------------------------------------------------------------------------


def build_matcher(pattern, use_glob, ignore_case):
    """Return f(name, path) -> bool, or None when there is no pattern."""
    if not pattern:
        return None
    smart = not ignore_case and not any(char.isupper() for char in pattern)
    flags = re.IGNORECASE if (ignore_case or smart) else 0

    if use_glob:
        anchored = "/" in pattern.strip("/")
        body = pattern[1:] if pattern.startswith("/") else pattern
        compiled = re.compile(glob_to_regex(body, anchored), flags)
        return lambda name, path: compiled.match(path) is not None

    try:
        compiled = re.compile(pattern, flags)
    except re.error as error:
        raise SystemExit("findwhy: %r is not a valid regular expression (%s)" % (pattern, error))
    return lambda name, path: compiled.search(name) is not None


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


def describe_rule(kind, source, line, text):
    if kind == "hidden":
        return "(hidden)", "dotfile"
    if kind == "rule":
        where = "%s:%d" % (source, line) if line else source
        return where, text
    return "(%s)" % kind, text


def print_skipped(finder, out):
    if not finder.skipped:
        out.write("nothing was skipped\n")
        return
    groups = sorted(finder.skipped.items(), key=lambda item: (-item[1][0], item[0]))
    total = sum(value[0] for _, value in groups)
    out.write("%d paths skipped - %d reasons\n\n" % (total, len(groups)))
    for (kind, source, line, text), (count, example, verdict) in groups:
        where, label = describe_rule(kind, source, line, text)
        out.write("  %5d  %-28s %-26s e.g. %s\n" % (count, where, label[:26], finder.output_path(example)))
    out.write("\n  A skipped directory is counted once; its contents are not walked.\n")


# ---------------------------------------------------------------------------
# explain
# ---------------------------------------------------------------------------


class Explainer:
    def __init__(self, finder, path, out, show_all=False):
        self.finder = finder
        self.path = os.path.abspath(path)
        self.out = out
        self.show_all = show_all

    def run(self):
        finder = self.finder
        out = self.out
        relative = finder.relative_norm(self.path)
        if relative is None:
            out.write("%s is outside the search root %s\n" % (self.path, finder.root))
            out.write("pass the root as the second argument, e.g. findwhy --explain %s %s\n"
                      % (self.path, finder.root))
            return 2

        try:
            info = os.lstat(self.path)
        except OSError as error:
            out.write("%s does not exist (%s)\n" % (self.path, error.strerror or error))
            return 2

        real = finder.relative_real(self.path) or ""
        kind = "directory" if stat.S_ISDIR(info.st_mode) else ("symlink" if stat.S_ISLNK(info.st_mode) else "file")
        depth = real.count("/") + 1 if real else 0
        out.write("%s\n" % (show(real) if real else "."))
        out.write("  %s, %s, depth %d\n\n" % (kind, "-" if kind == "directory" else human_size(info.st_size), depth))

        parts = real.split("/") if real else []

        # 1. ancestors: an excluded directory stops everything below it
        carried = list(finder.load_dir_files(finder.root))
        current = finder.root
        blocked = None
        ancestor_lines = []
        for part in parts[:-1]:
            current = os.path.join(current, part)
            verdict = finder.rule_verdict(current, True, finder.rules_for(tuple(carried)))
            label = show(finder.relative_real(current) or "")
            ancestor_lines.append((label, verdict))
            if verdict.excluded:
                blocked = (label, verdict)
                break
            carried.extend(finder.load_dir_files(current))

        out.write("ancestors\n")
        if not ancestor_lines:
            out.write("  the path sits directly in the search root\n")
        for name, verdict in ancestor_lines:
            if verdict.excluded:
                out.write("  %-40s excluded by %s `%s`\n" % (name, verdict.rule.where, verdict.rule.text))
            else:
                out.write("  %-40s included\n" % name)
        out.write("\n")

        # 2. the rules that apply to the path itself
        rules = finder.rules_for(tuple(carried))
        hits = finder.all_hits(self.path, kind == "directory", rules)
        self.print_rules(rules, hits)

        # 3. the dotfile gate, which only matters when no rule already decided
        out.write("dotfile gate (consulted only when no rule excludes the path)\n")
        blocked_hidden = None
        for index, part in enumerate(parts):
            if is_hidden_name(part):
                shown = show("/".join(parts[: index + 1]))
                if finder.opts.hidden:
                    out.write("  %-40s hidden, but --hidden is on\n" % shown)
                else:
                    out.write("  %-40s hidden\n" % shown)
                    blocked_hidden = shown
                    break
        if blocked_hidden is None:
            out.write("  %-40s no component starts with a dot\n" % "-")
        out.write("\n")

        if blocked is not None:
            name, verdict = blocked
            shadowed = [rule for rule in hits if rule.negated]
            # Peek one level into the excluded directory: a negation written
            # inside it can never take effect, and saying so is the whole point.
            shadowed += [rule for rule in finder.load_dir_files(current)
                         if rule.negated and rule.matches(norm(self.path), kind == "directory")]
            out.write("decision: EXCLUDED\n")
            out.write("reason:   %s excludes the parent directory %s\n" % (verdict.rule.where, name))
            if shadowed:
                out.write("\n  %s would re-include this path, but nothing inside an excluded\n"
                          % shadowed[-1].where)
                out.write("  directory can be re-included. Exclude the contents instead of the\n")
                out.write("  directory (`%s` with a trailing `*`), or pass --ignored.\n"
                          % verdict.rule.text.rstrip("/"))
            return 1

        if hits and not hits[-1].negated:
            winner = hits[-1]
            out.write("decision: EXCLUDED\n")
            out.write("reason:   %s `%s` is the last matching rule\n" % (winner.where, winner.text))
            return 1

        if blocked_hidden is not None:
            out.write("decision: EXCLUDED\n")
            out.write("reason:   %s starts with a dot; pass --hidden to include dotfiles\n" % blocked_hidden)
            return 1

        out.write("decision: INCLUDED\n")
        if hits:
            winner = hits[-1]
            out.write("reason:   %s `%s` re-includes it\n" % (winner.where, winner.text))
        else:
            out.write("reason:   no ignore rule matched\n")
        return 0

    def print_rules(self, rules, hits):
        out = self.out
        hit_ids = {id(rule) for rule in hits}
        out.write("ignore rules, lowest priority first (%d pattern%s)\n"
                  % (len(rules), "" if len(rules) == 1 else "s"))
        if not rules:
            out.write("  none found - no .gitignore, .ignore, .findwhyignore or --exclude\n")
            out.write("\n")
            return

        order = []
        for rule in rules:
            key = rule.source
            if not order or order[-1][0] != key:
                order.append((key, []))
            order[-1][1].append(rule)

        for source, group in order:
            matched = [rule for rule in group if id(rule) in hit_ids]
            label = source
            if source == "(global)" and self.finder.global_path:
                label = "(global) %s" % self.finder.global_path
            if matched:
                out.write("  %s\n" % label)
                for rule in group:
                    if id(rule) in hit_ids:
                        tag = "re-include" if rule.negated else "exclude"
                        out.write("    line %-4d %-30s %-12s -> %s\n"
                                  % (rule.line, rule.text[:30], "(dir only)" if rule.dir_only else "", tag))
                    elif self.show_all:
                        out.write("    line %-4d %-30s %-12s    no match\n"
                                  % (rule.line, rule.text[:30], "(dir only)" if rule.dir_only else ""))
                if not self.show_all and len(group) > len(matched):
                    out.write("    ... %d more pattern(s) in this source did not match (--explain-all)\n"
                              % (len(group) - len(matched)))
            elif self.show_all:
                out.write("  %s\n" % label)
                for rule in group:
                    out.write("    line %-4d %-30s %-12s    no match\n"
                              % (rule.line, rule.text[:30], "(dir only)" if rule.dir_only else ""))
            else:
                out.write("  %-58s %d pattern(s), none matched\n" % (label, len(group)))
        out.write("\n")


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="findwhy",
        description="Find files. When one is missing, say which rule removed it.",
        epilog="exit codes: 0 found, 1 nothing found, 2 error",
    )
    parser.add_argument("pattern", nargs="?", help="regular expression matched against the file name")
    parser.add_argument("path", nargs="?", default=".", help="directory to search (default: .)")
    parser.add_argument("-g", "--glob", action="store_true", help="treat the pattern as a glob on the relative path")
    parser.add_argument("-i", "--ignore-case", action="store_true", help="never use smart case")
    parser.add_argument("--not", dest="invert", action="store_true", help="keep paths that do NOT match")
    parser.add_argument("-t", "--type", dest="types", action="append", choices=["f", "d", "l"],
                        help="f file, d directory, l symlink (repeatable)")
    parser.add_argument("-d", "--max-depth", type=int, help="stop descending after this many levels")
    parser.add_argument("--min-depth", type=int, help="skip entries above this level")
    parser.add_argument("-E", "--exclude", action="append", default=[], metavar="PATTERN",
                        help="extra ignore pattern, highest priority (repeatable)")
    parser.add_argument("-H", "--hidden", action="store_true", help="include dotfiles")
    parser.add_argument("-I", "--ignored", action="store_true", help="include ignored files")
    parser.add_argument("-u", "--unrestricted", action="store_true", help="shorthand for --hidden --ignored")
    parser.add_argument("-a", "--absolute", action="store_true", help="print absolute paths")
    parser.add_argument("-L", "--follow", action="store_true", help="follow symlinks, detecting cycles")
    parser.add_argument("--parent", action="store_true", help="also read ignore files above the search root")
    parser.add_argument("--global-ignore", metavar="PATH", help="global ignore file, or - to disable")
    parser.add_argument("--explain", metavar="PATH", help="explain the decision for one path")
    parser.add_argument("--explain-all", action="store_true", help="with --explain, list rules that did not match too")
    parser.add_argument("--skipped", action="store_true", help="report what was skipped and why, instead of results")
    parser.add_argument("-V", "--version", action="version", version="findwhy " + __version__)
    return parser


def main(argv=None):
    parser = build_parser()
    opts = parser.parse_args(argv)

    if opts.unrestricted:
        opts.hidden = True
        opts.ignored = True
    if opts.max_depth is not None and opts.max_depth < 0:
        parser.error("--max-depth cannot be negative")

    # `findwhy --skipped src` and `findwhy --explain x.py src` read better than
    # forcing an empty pattern, so in those two modes a lone directory argument
    # is the search root rather than a pattern.
    if opts.pattern and opts.path == "." and (opts.explain or opts.skipped) and os.path.isdir(opts.pattern):
        opts.path, opts.pattern = opts.pattern, None

    root = opts.path
    if not os.path.isdir(root):
        if os.path.exists(root):
            parser.error("%s is not a directory; pass the directory that contains it" % root)
        parser.error("%s does not exist" % root)

    opts.matcher = build_matcher(opts.pattern, opts.glob, opts.ignore_case)
    finder = Finder(root, opts)

    if opts.explain:
        return Explainer(finder, opts.explain, sys.stdout, opts.explain_all).run()

    results = finder.walk()

    if opts.skipped:
        print_skipped(finder, sys.stdout)
        return 0 if finder.skipped else 1

    out = sys.stdout
    for line in results:
        out.write(line + "\n")

    skipped_total = sum(value[0] for value in finder.skipped.values())
    if sys.stderr.isatty():
        summary = "%d result%s" % (len(results), "" if len(results) == 1 else "s")
        if skipped_total:
            summary += " - %d path%s skipped (--skipped to see why)" % (
                skipped_total, "" if skipped_total == 1 else "s")
        if finder.stats["unreadable"]:
            summary += " - %d unreadable" % finder.stats["unreadable"]
        sys.stderr.write(summary + "\n")

    return 0 if results else 1


if __name__ == "__main__":
    sys.exit(main())
