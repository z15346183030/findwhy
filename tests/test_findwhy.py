"""Tests for findwhy.

Run with:  python -m unittest discover -s tests
"""

import contextlib
import io
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import findwhy  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def compile_rule(pattern, base=""):
    spec = findwhy.parse_pattern(pattern)
    assert spec is not None, "pattern %r should parse" % pattern
    regex = re.compile(findwhy.glob_to_regex(spec.body, spec.anchored), findwhy.REGEX_FLAGS)
    return findwhy.Rule(spec.text, "test", 1, spec.negated, spec.dir_only, regex, base)


def matches(pattern, path, is_dir=False, base=""):
    return compile_rule(pattern, base).matches(path, is_dir)


class TreeCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, relative, content=""):
        path = os.path.join(self.root, relative.replace("/", os.sep))
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        return path

    def build(self, spec):
        for relative, content in spec.items():
            self.write(relative, content)

    def run_cli(self, argv, cwd=None):
        out = io.StringIO()
        err = io.StringIO()
        previous = os.getcwd()
        os.chdir(cwd or self.root)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = findwhy.main(argv)
        finally:
            os.chdir(previous)
        return code, out.getvalue(), err.getvalue()

    def lines(self, argv, cwd=None):
        code, out, _err = self.run_cli(argv, cwd)
        return code, [line for line in out.splitlines() if line.strip()]

    def finder(self, argv, cwd=None):
        previous = os.getcwd()
        os.chdir(cwd or self.root)
        try:
            opts = findwhy.build_parser().parse_args(argv)
            if opts.unrestricted:
                opts.hidden = True
                opts.ignored = True
            opts.matcher = findwhy.build_matcher(opts.pattern, opts.glob, opts.ignore_case)
            return findwhy.Finder(opts.path, opts)
        finally:
            os.chdir(previous)


# ---------------------------------------------------------------------------
# parsing a pattern line
# ---------------------------------------------------------------------------


class ParsePatternTests(unittest.TestCase):
    def test_comment_is_skipped(self):
        self.assertIsNone(findwhy.parse_pattern("# build output"))

    def test_blank_line_is_skipped(self):
        self.assertIsNone(findwhy.parse_pattern("   "))

    def test_trailing_spaces_are_dropped(self):
        self.assertEqual(findwhy.parse_pattern("build/   ").body, "build")

    def test_escaped_trailing_space_is_kept(self):
        self.assertEqual(findwhy.parse_pattern("foo\\ ").body, "foo\\ ")
        self.assertTrue(matches("foo\\ ", "foo "))
        self.assertFalse(matches("foo\\ ", "foo"))

    def test_negation(self):
        spec = findwhy.parse_pattern("!keep.log")
        self.assertTrue(spec.negated)
        self.assertEqual(spec.body, "keep.log")

    def test_escaped_bang_is_literal(self):
        spec = findwhy.parse_pattern("\\!important")
        self.assertFalse(spec.negated)
        self.assertEqual(spec.body, "!important")

    def test_escaped_hash_is_literal(self):
        self.assertEqual(findwhy.parse_pattern("\\#notes").body, "#notes")

    def test_directory_only(self):
        self.assertTrue(findwhy.parse_pattern("build/").dir_only)

    def test_leading_slash_anchors(self):
        spec = findwhy.parse_pattern("/build")
        self.assertTrue(spec.anchored)
        self.assertEqual(spec.body, "build")

    def test_inner_slash_anchors(self):
        self.assertTrue(findwhy.parse_pattern("src/generated").anchored)

    def test_plain_name_is_not_anchored(self):
        self.assertFalse(findwhy.parse_pattern("build").anchored)

    def test_lone_slash_is_ignored(self):
        self.assertIsNone(findwhy.parse_pattern("/"))

    def test_lone_bang_is_ignored(self):
        self.assertIsNone(findwhy.parse_pattern("!"))


# ---------------------------------------------------------------------------
# glob translation
# ---------------------------------------------------------------------------


class GlobTests(unittest.TestCase):
    def test_star_matches_any_depth(self):
        self.assertTrue(matches("*.log", "a.log"))
        self.assertTrue(matches("*.log", "x/y/a.log"))

    def test_star_does_not_cross_slash(self):
        self.assertFalse(matches("*.py", "pkg/x.py/b"))
        self.assertFalse(matches("src/*.py", "src/deep/x.py"))

    def test_star_suffix_is_respected(self):
        self.assertFalse(matches("*.log", "a.logx"))

    def test_directory_pattern_ignores_files_with_that_name(self):
        self.assertTrue(matches("build/", "build", is_dir=True))
        self.assertFalse(matches("build/", "build", is_dir=False))

    def test_directory_pattern_matches_at_depth(self):
        self.assertTrue(matches("build/", "x/y/build", is_dir=True))

    def test_leading_slash_is_root_only(self):
        self.assertTrue(matches("/build", "build", is_dir=True))
        self.assertFalse(matches("/build", "x/build", is_dir=True))

    def test_inner_slash_is_anchored(self):
        self.assertTrue(matches("a/b", "a/b"))
        self.assertFalse(matches("a/b", "x/a/b"))

    def test_globstar_prefix(self):
        self.assertTrue(matches("**/foo", "foo"))
        self.assertTrue(matches("**/foo", "x/y/foo"))

    def test_globstar_suffix_excludes_the_directory_itself(self):
        self.assertTrue(matches("foo/**", "foo/x"))
        self.assertTrue(matches("foo/**", "foo/x/y"))
        self.assertFalse(matches("foo/**", "foo", is_dir=True))

    def test_globstar_in_the_middle(self):
        self.assertTrue(matches("a/**/b", "a/b"))
        self.assertTrue(matches("a/**/b", "a/x/b"))
        self.assertTrue(matches("a/**/b", "a/x/y/b"))
        self.assertFalse(matches("a/**/b", "a/x/y/c"))

    def test_question_mark_is_one_character(self):
        self.assertTrue(matches("a?.py", "ab.py"))
        self.assertFalse(matches("a?.py", "abc.py"))
        self.assertFalse(matches("a?b", "a/b"))

    def test_character_class(self):
        self.assertTrue(matches("[abc].txt", "b.txt"))
        self.assertFalse(matches("[abc].txt", "d.txt"))

    def test_negated_character_class(self):
        self.assertTrue(matches("[!abc].txt", "d.txt"))
        self.assertFalse(matches("[!abc].txt", "a.txt"))

    def test_escaped_star_is_literal(self):
        self.assertTrue(matches("a\\*b", "a*b"))
        self.assertFalse(matches("a\\*b", "axb"))

    def test_bracket_without_close_is_literal(self):
        self.assertTrue(matches("a[bc", "a[bc"))

    def test_pattern_is_relative_to_its_base(self):
        self.assertTrue(matches("*.log", "sub/a.log", base="sub"))
        self.assertTrue(matches("/a.log", "sub/a.log", base="sub"))

    def test_case_insensitive_on_windows(self):
        if os.name == "nt":
            self.assertTrue(matches("*.LOG", "a.log"))


# ---------------------------------------------------------------------------
# precedence
# ---------------------------------------------------------------------------


class PrecedenceTests(TreeCase):
    def test_gitignore_excludes(self):
        self.build({"a.py": "", "b.log": "", ".gitignore": "*.log\n"})
        _code, found = self.lines(["--global-ignore", "-", "", "."])
        self.assertIn("a.py", found)
        self.assertNotIn("b.log", found)

    def test_negation_reincludes_in_same_file(self):
        self.build({"keep.log": "", "drop.log": "", ".gitignore": "*.log\n!keep.log\n"})
        _code, found = self.lines(["--global-ignore", "-", "", "."])
        self.assertIn("keep.log", found)
        self.assertNotIn("drop.log", found)

    def test_last_rule_wins(self):
        self.build({"keep.log": "", ".gitignore": "*.log\n!keep.log\n*.log\n"})
        _code, found = self.lines(["--global-ignore", "-", "", "."])
        self.assertNotIn("keep.log", found)

    def test_deeper_gitignore_overrides_shallower(self):
        self.build({"src/a.log": "", ".gitignore": "*.log\n", "src/.gitignore": "!a.log\n"})
        _code, found = self.lines(["--global-ignore", "-", "", "."])
        self.assertIn("src/a.log", found)

    def test_ignore_file_beats_gitignore(self):
        self.build({"a.log": "", ".gitignore": "*.log\n", ".ignore": "!a.log\n"})
        _code, found = self.lines(["--global-ignore", "-", "", "."])
        self.assertIn("a.log", found)

    def test_findwhyignore_beats_ignore(self):
        self.build({"a.log": "", ".gitignore": "*.log\n", ".ignore": "!a.log\n", ".findwhyignore": "a.log\n"})
        _code, found = self.lines(["--global-ignore", "-", "", "."])
        self.assertNotIn("a.log", found)

    def test_exclude_beats_everything(self):
        self.build({"a.log": "", ".gitignore": "!a.log\n"})
        _code, found = self.lines(["--global-ignore", "-", "-E", "*.log", "", "."])
        self.assertNotIn("a.log", found)

    def test_exclude_can_reinclude(self):
        self.build({"keep.log": "", ".gitignore": "*.log\n"})
        _code, found = self.lines(["--global-ignore", "-", "-E", "*.log", "-E", "!keep.log", "", "."])
        self.assertIn("keep.log", found)

    def test_global_ignore_is_lowest_priority(self):
        global_file = os.path.join(self.root, "global-ignore")
        with open(global_file, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("*.bak\n")
        self.build({"a.bak": "", ".gitignore": "!a.bak\n"})
        _code, found = self.lines(["--global-ignore", global_file, "", "."])
        self.assertIn("a.bak", found)

    def test_global_ignore_applies_without_a_project_override(self):
        global_file = os.path.join(self.root, "global-ignore")
        with open(global_file, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("*.bak\n")
        self.build({"a.bak": "", "b.txt": ""})
        _code, found = self.lines(["--global-ignore", global_file, "", "."])
        self.assertNotIn("a.bak", found)
        self.assertIn("b.txt", found)


# ---------------------------------------------------------------------------
# the git rule that surprises people
# ---------------------------------------------------------------------------


class AncestorTests(TreeCase):
    def test_cannot_reinclude_inside_excluded_directory(self):
        self.build({"build/out.log": "", ".gitignore": "build/\n!out.log\n"})
        _code, found = self.lines(["--global-ignore", "-", "", "."])
        self.assertEqual(found, [])

    def test_explain_reports_the_shadowed_negation(self):
        self.build({"build/out.log": "", ".gitignore": "build/\n", "build/.gitignore": "!out.log\n"})
        _code, out, _err = self.run_cli(["--global-ignore", "-", "--explain", "build/out.log"])
        self.assertIn("EXCLUDED", out)
        self.assertIn("parent directory", out)
        self.assertIn("would re-include", out)

    def test_negation_of_the_directory_itself_works(self):
        self.build({"build/out.log": "", ".gitignore": "build/\n!build/\n"})
        _code, found = self.lines(["--global-ignore", "-", "", "."])
        self.assertIn("build/out.log", found)

    def test_search_root_inside_an_ignored_directory(self):
        self.build({".gitignore": "ignored/\n", "ignored/a.txt": "", "ignored/b.txt": ""})
        _code, found = self.lines(["--global-ignore", "-", "", "ignored"])
        self.assertEqual(found, ["ignored/a.txt", "ignored/b.txt"])

    def test_parent_flag_restores_the_outer_rules(self):
        self.build({".gitignore": "ignored/\n", "ignored/a.txt": ""})
        _code, found = self.lines(["--global-ignore", "-", "--parent", "", "ignored"])
        self.assertEqual(found, [])


# ---------------------------------------------------------------------------
# gates and filters
# ---------------------------------------------------------------------------


class FilterTests(TreeCase):
    def test_hidden_is_skipped_by_default(self):
        self.build({"a.txt": "", ".hidden": ""})
        _code, found = self.lines(["--global-ignore", "-", "", "."])
        self.assertEqual(found, ["a.txt"])

    def test_hidden_flag_includes_dotfiles(self):
        self.build({"a.txt": "", ".hidden": ""})
        _code, found = self.lines(["--global-ignore", "-", "-H", "", "."])
        self.assertIn(".hidden", found)

    def test_ignored_flag_includes_ignored_files(self):
        self.build({"a.log": "", ".gitignore": "*.log\n"})
        _code, found = self.lines(["--global-ignore", "-", "-I", "", "."])
        self.assertIn("a.log", found)

    def test_unrestricted_is_both(self):
        self.build({"a.log": "", ".hidden": "", ".gitignore": "*.log\n"})
        _code, found = self.lines(["--global-ignore", "-", "-u", "", "."])
        self.assertIn("a.log", found)
        self.assertIn(".hidden", found)

    def test_type_directory(self):
        self.build({"src/a.txt": "", "b.txt": ""})
        _code, found = self.lines(["--global-ignore", "-", "-t", "d", "", "."])
        self.assertEqual(found, ["src"])

    def test_type_file(self):
        self.build({"src/a.txt": ""})
        _code, found = self.lines(["--global-ignore", "-", "-t", "f", "", "."])
        self.assertEqual(found, ["src/a.txt"])

    def test_max_depth(self):
        self.build({"a.txt": "", "x/b.txt": "", "x/y/c.txt": ""})
        _code, found = self.lines(["--global-ignore", "-", "-d", "1", "", "."])
        self.assertEqual(found, ["a.txt", "x"])

    def test_min_depth(self):
        self.build({"a.txt": "", "x/b.txt": ""})
        _code, found = self.lines(["--global-ignore", "-", "--min-depth", "2", "", "."])
        self.assertEqual(found, ["x/b.txt"])

    def test_not_inverts_the_pattern(self):
        self.build({"a.py": "", "b.txt": ""})
        _code, found = self.lines(["--global-ignore", "-", "--not", "txt", "."])
        self.assertEqual(found, ["a.py"])

    def test_glob_mode(self):
        self.build({"a.py": "", "pkg/b.py": "", "c.txt": ""})
        _code, found = self.lines(["--global-ignore", "-", "-g", "pkg/*.py", "."])
        self.assertEqual(found, ["pkg/b.py"])

    def test_smart_case_is_case_sensitive_with_uppercase(self):
        self.build({"README.md": "", "readme.txt": ""})
        _code, found = self.lines(["--global-ignore", "-", "README", "."])
        self.assertEqual(found, ["README.md"])

    def test_smart_case_is_case_insensitive_without_uppercase(self):
        self.build({"README.md": ""})
        _code, found = self.lines(["--global-ignore", "-", "readme", "."])
        self.assertEqual(found, ["README.md"])

    def test_exit_code_zero_when_found(self):
        self.build({"a.txt": ""})
        code, _out, _err = self.run_cli(["--global-ignore", "-", "a", "."])
        self.assertEqual(code, 0)

    def test_exit_code_one_when_nothing_found(self):
        self.build({"a.txt": ""})
        code, _out, _err = self.run_cli(["--global-ignore", "-", "zzz", "."])
        self.assertEqual(code, 1)


# ---------------------------------------------------------------------------
# --exclude and absolute paths
# ---------------------------------------------------------------------------


class ExcludeTests(TreeCase):
    def test_exclude_directory(self):
        self.build({"vendor/a.js": "", "src/b.js": ""})
        _code, found = self.lines(["--global-ignore", "-", "-E", "vendor/", "", "."])
        self.assertEqual(found, ["src", "src/b.js"])

    def test_exclude_absolute_path(self):
        self.build({"vendor/a.js": "", "src/b.js": ""})
        target = os.path.join(self.root, "vendor") + os.sep
        _code, found = self.lines(["--global-ignore", "-", "-E", target, "", "."])
        self.assertEqual(found, ["src", "src/b.js"])

    def test_exclude_outside_the_root_warns(self):
        self.build({"a.js": ""})
        outside = os.path.join(tempfile.gettempdir(), "somewhere-else") + os.sep
        code, _out, err = self.run_cli(["--global-ignore", "-", "-E", outside, "", "."])
        self.assertEqual(code, 0)
        self.assertIn("can never match", err)


# ---------------------------------------------------------------------------
# accounting
# ---------------------------------------------------------------------------


class AccountingTests(TreeCase):
    def test_every_visited_path_is_accounted_for(self):
        self.build({
            "a.txt": "", ".hidden": "", "b.log": "", ".gitignore": "*.log\n",
            "src/c.py": "", "src/.gitignore": "tmp/\n", "src/tmp/d.txt": "",
        })
        finder = self.finder(["--global-ignore", "-", "", "."])
        finder.walk()
        skipped = sum(value[0] for value in finder.skipped.values())
        self.assertEqual(finder.stats["visited"], finder.stats["emitted"] + finder.stats["filtered"] + skipped)

    def test_skipped_groups_by_rule(self):
        self.build({"a.log": "", "b.log": "", "c.txt": "", ".gitignore": "*.log\n"})
        _code, out, _err = self.run_cli(["--global-ignore", "-", "--skipped", "."])
        self.assertIn("*.log", out)
        self.assertIn("(hidden)", out)

    def test_skipped_counts_hidden_separately(self):
        self.build({"a.txt": "", ".hidden": ""})
        _code, out, _err = self.run_cli(["--global-ignore", "-", "--skipped", "."])
        self.assertIn("(hidden)", out)
        self.assertIn("1 paths skipped", out)

    def test_skipped_mode_accepts_a_lone_directory(self):
        self.build({"a.txt": "", ".hidden": ""})
        code, out, _err = self.run_cli(["--global-ignore", "-", "--skipped", "."])
        self.assertEqual(code, 0)
        self.assertIn("skipped", out)


# ---------------------------------------------------------------------------
# explain
# ---------------------------------------------------------------------------


class ExplainTests(TreeCase):
    def test_included_file(self):
        self.build({"a.txt": ""})
        code, out, _err = self.run_cli(["--global-ignore", "-", "--explain", "a.txt"])
        self.assertEqual(code, 0)
        self.assertIn("decision: INCLUDED", out)
        self.assertIn("no ignore rule matched", out)

    def test_excluded_file_names_the_line(self):
        self.build({"a.log": "", ".gitignore": "*.log\n"})
        code, out, _err = self.run_cli(["--global-ignore", "-", "--explain", "a.log"])
        self.assertEqual(code, 1)
        self.assertIn("EXCLUDED", out)
        self.assertIn(".gitignore:1", out)
        self.assertIn("*.log", out)

    def test_reincluded_file(self):
        self.build({"keep.log": "", ".gitignore": "*.log\n!keep.log\n"})
        code, out, _err = self.run_cli(["--global-ignore", "-", "--explain", "keep.log"])
        self.assertEqual(code, 0)
        self.assertIn("re-includes", out)

    def test_hidden_file(self):
        self.build({".env": ""})
        code, out, _err = self.run_cli(["--global-ignore", "-", "--explain", ".env"])
        self.assertEqual(code, 1)
        self.assertIn("dotfile", out)
        self.assertIn("--hidden", out)

    def test_hidden_file_with_the_flag(self):
        self.build({".env": ""})
        code, out, _err = self.run_cli(["--global-ignore", "-", "-H", "--explain", ".env"])
        self.assertEqual(code, 0)

    def test_missing_file(self):
        self.build({"a.txt": ""})
        code, out, _err = self.run_cli(["--global-ignore", "-", "--explain", "nope.txt"])
        self.assertEqual(code, 2)
        self.assertIn("does not exist", out)

    def test_path_outside_the_root(self):
        self.build({"a.txt": ""})
        outside = os.path.join(tempfile.gettempdir(), "elsewhere.txt")
        code, out, _err = self.run_cli(["--global-ignore", "-", "--explain", outside])
        self.assertEqual(code, 2)
        self.assertIn("outside the search root", out)

    def test_explain_all_lists_non_matching_rules(self):
        self.build({"a.txt": "", ".gitignore": "*.log\n*.tmp\n"})
        _code, out, _err = self.run_cli(["--global-ignore", "-", "--explain-all", "--explain", "a.txt"])
        self.assertIn("no match", out)

    def test_explain_shows_the_rule_order(self):
        self.build({"a.txt": "", ".gitignore": "x\n", ".ignore": "y\n"})
        _code, out, _err = self.run_cli(["--global-ignore", "-", "--explain", "a.txt"])
        self.assertLess(out.index(".gitignore"), out.index(".ignore"))


# ---------------------------------------------------------------------------
# file format details
# ---------------------------------------------------------------------------


class FormatTests(TreeCase):
    def test_crlf_ignore_file(self):
        path = os.path.join(self.root, ".gitignore")
        with open(path, "wb") as handle:
            handle.write(b"*.log\r\n!keep.log\r\n")
        self.build({"a.log": "", "keep.log": ""})
        _code, found = self.lines(["--global-ignore", "-", "", "."])
        self.assertEqual(found, ["keep.log"])

    def test_bom_in_ignore_file(self):
        path = os.path.join(self.root, ".gitignore")
        with open(path, "wb") as handle:
            handle.write(b"\xef\xbb\xbf*.log\n")
        self.build({"a.log": ""})
        _code, found = self.lines(["--global-ignore", "-", "", "."])
        self.assertEqual(found, [])

    def test_empty_ignore_file(self):
        self.build({"a.txt": "", ".gitignore": ""})
        _code, found = self.lines(["--global-ignore", "-", "", "."])
        self.assertEqual(found, ["a.txt"])

    def test_output_order_is_deterministic(self):
        self.build({"b.txt": "", "a.txt": "", "m/c.txt": ""})
        _first = self.lines(["--global-ignore", "-", "", "."])[1]
        _second = self.lines(["--global-ignore", "-", "", "."])[1]
        self.assertEqual(_first, _second)
        self.assertEqual(_first, ["a.txt", "b.txt", "m", "m/c.txt"])

    def test_output_keeps_the_original_case(self):
        self.build({"MixedCase.txt": ""})
        _code, found = self.lines(["--global-ignore", "-", "", "."])
        self.assertEqual(found, ["MixedCase.txt"])

    def test_absolute_output(self):
        self.build({"a.txt": ""})
        _code, found = self.lines(["--global-ignore", "-", "-a", "", "."])
        self.assertEqual(len(found), 1)
        self.assertTrue(os.path.isabs(found[0]))

    def test_root_is_echoed_in_output(self):
        self.build({"sub/a.txt": ""})
        _code, found = self.lines(["--global-ignore", "-", "", "sub"])
        self.assertEqual(found, ["sub/a.txt"])


# ---------------------------------------------------------------------------
# the walker and the single-path decision have to agree
# ---------------------------------------------------------------------------


class ConsistencyTests(TreeCase):
    def tree(self):
        self.build({
            ".gitignore": "*.log\nbuild/\n!keep.log\nnode_modules/\n",
            "a.log": "", "keep.log": "",
            "src/.gitignore": "generated/\n!allow.py\n",
            "src/app.py": "", "src/allow.py": "",
            "src/generated/api.py": "",
            "build/out.js": "", "build/.gitignore": "!out.js\n",
            "node_modules/x/index.js": "",
            ".hidden/secret.txt": "",
        })

    def test_walker_and_decide_agree(self):
        self.tree()
        finder = self.finder(["--global-ignore", "-", "", "."])
        found = set(finder.walk())
        for base, dirnames, filenames in os.walk(self.root):
            for name in dirnames + filenames:
                path = os.path.join(base, name)
                relative = os.path.relpath(path, self.root).replace("\\", "/")
                if relative == ".gitignore" or relative.endswith("/.gitignore"):
                    continue
                decided = finder.decide(path).excluded
                self.assertEqual(decided, relative not in found, "disagreement on %s" % relative)

    def test_decide_reports_the_shadowed_rule_inside_the_excluded_dir(self):
        self.tree()
        code, out, _err = self.run_cli(["--global-ignore", "-", "--explain", "build/out.js"])
        self.assertEqual(code, 1)
        self.assertIn("build/.gitignore:1", out)
        self.assertIn("nothing inside an excluded", out)

    def test_negation_works_when_the_parent_is_not_excluded(self):
        self.build({"a.log": "", "keep.log": "", ".gitignore": "*.log\n!keep.log\n"})
        _code, found = self.lines(["--global-ignore", "-", "", "."])
        self.assertEqual(found, ["keep.log"])

    def test_excluding_the_contents_lets_the_negation_through(self):
        self.build({"build/out.js": "", "build/other.js": "", ".gitignore": "build/*\n!build/out.js\n"})
        _code, found = self.lines(["--global-ignore", "-", "", "."])
        self.assertEqual(found, ["build", "build/out.js"])


# ---------------------------------------------------------------------------
# edge cases
# ---------------------------------------------------------------------------


class EdgeTests(TreeCase):
    def test_empty_directory(self):
        code, found = self.lines(["--global-ignore", "-", "", "."])
        self.assertEqual(code, 1)
        self.assertEqual(found, [])

    def test_skipped_when_nothing_was_skipped(self):
        self.build({"a.txt": ""})
        code, out, _err = self.run_cli(["--global-ignore", "-", "--skipped", "."])
        self.assertEqual(code, 1)
        self.assertIn("nothing was skipped", out)

    def test_max_depth_zero_reports_nothing(self):
        self.build({"a.txt": ""})
        _code, found = self.lines(["--global-ignore", "-", "-d", "0", "", "."])
        self.assertEqual(found, [])

    def test_glob_with_a_dot_in_it_is_literal(self):
        self.build({"a.py": "", "axpy": ""})
        _code, found = self.lines(["--global-ignore", "-", "-g", "*.py", "."])
        self.assertEqual(found, ["a.py"])

    def test_not_with_glob(self):
        self.build({"a.py": "", "b.txt": ""})
        _code, found = self.lines(["--global-ignore", "-", "-g", "*.py", "--not", "."])
        self.assertEqual(found, ["b.txt"])

    def test_hidden_directory_hides_its_contents(self):
        self.build({".config/app.json": "", "visible.txt": ""})
        _code, found = self.lines(["--global-ignore", "-", "", "."])
        self.assertEqual(found, ["visible.txt"])

    def test_hidden_flag_reveals_a_whole_hidden_subtree(self):
        self.build({".config/app.json": "", "visible.txt": ""})
        _code, found = self.lines(["--global-ignore", "-", "-H", "", "."])
        self.assertEqual(found, [".config", ".config/app.json", "visible.txt"])

    def test_explain_of_a_file_under_a_hidden_directory(self):
        self.build({".config/app.json": ""})
        code, out, _err = self.run_cli(["--global-ignore", "-", "--explain", ".config/app.json"])
        self.assertEqual(code, 1)
        self.assertIn(".config", out)
        self.assertIn("starts with a dot", out)

    def test_two_exclude_patterns_are_both_applied(self):
        self.build({"a.log": "", "b.tmp": "", "c.txt": ""})
        _code, found = self.lines(["--global-ignore", "-", "-E", "*.log", "-E", "*.tmp", "", "."])
        self.assertEqual(found, ["c.txt"])

    def test_directory_type_filter_still_descends(self):
        self.build({"a/b/c.txt": ""})
        _code, found = self.lines(["--global-ignore", "-", "-t", "d", "", "."])
        self.assertEqual(found, ["a", "a/b"])


if __name__ == "__main__":
    unittest.main()
