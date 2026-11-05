# findwhy

**Find files. When one you expected is missing, name the rule that removed it — with the file and the line it came from. One file, standard library only, no config file.**

```bash
python findwhy.py log examples/demo
```

```
examples/demo/logs
examples/demo/logs/keep.log
```

Two results. There is a third file that says `log` in its name and did not show
up. Ask what happened to it:

```bash
python findwhy.py --skipped examples/demo
```

```
7 paths skipped - 4 reasons

      3  (hidden)                     dotfile                    e.g. examples/demo/.cache
      2  .gitignore:4                 *.log                      e.g. examples/demo/logs/app.log
      1  .gitignore:2                 build/                     e.g. examples/demo/src/build
      1  .gitignore:3                 node_modules/              e.g. examples/demo/node_modules

  A skipped directory is counted once; its contents are not walked.
```

Now ask about one specific path, and get the whole chain that decided it:

```bash
python findwhy.py --explain examples/demo/src/build/out.log
```

```
examples/demo/src/build/out.log
  file, 12 B, depth 5

ancestors
  examples                                 included
  examples/demo                            included
  examples/demo/src                        included
  examples/demo/src/build                  excluded by examples/demo/.gitignore:2 `build/`

ignore rules, lowest priority first (5 patterns)
  examples/demo/.gitignore
    line 4    *.log                                       -> exclude
    ... 3 more pattern(s) in this source did not match (--explain-all)
  examples/demo/src/.gitignore
    line 1    !out.log                                    -> re-include

dotfile gate (consulted only when no rule excludes the path)
  -                                        no component starts with a dot

decision: EXCLUDED
reason:   examples/demo/.gitignore:2 excludes the parent directory examples/demo/src/build

  examples/demo/src/.gitignore:1 would re-include this path, but nothing inside an excluded
  directory can be re-included. Exclude the contents instead of the
  directory (`build` with a trailing `*`), or pass --ignored.
```

That last paragraph is the point of the whole tool. The user wrote `!out.log`,
it is in a file that is being read, it matches the path, and it does nothing —
because `src/build` is excluded and nothing inside an excluded directory can be
brought back. Every other finder just returns nothing and lets you guess. Here
the shadowed rule is named, and the fix is spelled out: exclude `build/*`
instead of `build/`, and the negation starts working.

## Why this exists

A file finder that respects ignore rules is easy to build and hard to trust. The
hard part is not deciding to skip a path; it is telling the user why the path
they expected never appeared.

The default behavior of most finders is silence: no result, no explanation, no
hint about which rule fired. The user then guesses — wrong directory? wrong
flag? wrong pattern? — and the answer is usually buried in a `.gitignore` three
levels up.

findwhy treats the explanation as the product. Every skipped path has a reason,
every reason has a source file and line, and the rule that would have re-included
a path inside an excluded directory is reported as shadowed rather than ignored.
Once the decision is visible, the fix is usually one line.

## What it does

| | |
|---|---|
| reads | one directory tree, walked with `os.scandir` |
| matches | a regex on the file name (default), or a glob on the relative path |
| decides | dotfile gate, then ignore rules with full provenance |
| reports | matching paths, or what was skipped and why, or one path's decision |
| depends on | nothing outside the standard library |

## How a path gets decided

The order is fixed and printed by `--explain`, so there is never a second
question:

1. **Ancestors.** Walk from the search root down. If any parent directory ends
   up excluded, the path is excluded and the parent's rule is the reason. This
   is git's own rule — nothing inside an excluded directory can be re-included —
   and it is the one people trip over most.
2. **Rules.** The last matching rule wins. Lowest priority first:
   global ignore file → `.gitignore` → `.ignore` → `.findwhyignore` → `--exclude`.
   Deeper directories beat shallower ones, and a later line beats an earlier one
   in the same file.
3. **The dotfile gate.** Only consulted if no rule already excluded the path. A
   concrete rule is more useful to the user than "it starts with a dot".

Two deliberate choices fall out of this:

- **The search root is the boundary.** Ignore files above it are not read, so
  `findwhy rmeta target/` finds the files in `target/` even when `target/` is in
  `.gitignore`. `--parent` widens the boundary when you want the outer rules.
- **`.gitignore` is read whether or not the directory is a git repository.**
  Otherwise deleting `.git` silently starts listing `node_modules`. Here the
  rule is applied and, if you ask, named.

## Install

Nothing to install. Python 3.8+ and the file itself.

```bash
git clone https://github.com/z15346183030/findwhy
cd findwhy
python findwhy.py --help
```

Put it on the PATH if you like:

```bash
cp findwhy.py ~/.local/bin/findwhy && chmod +x ~/.local/bin/findwhy
```

## Use

```bash
# every path whose name contains "config"
python findwhy.py config

# only files, at most three levels deep
python findwhy.py -t f -d 3 '\.py$' .

# glob on the whole relative path instead of a regex on the name
python findwhy.py -g 'src/**/*.ts' .

# what was dropped, and by which rule
python findwhy.py --skipped .

# why is this one path missing
python findwhy.py --explain src/generated/api.ts

# the same question, with the search root given explicitly
python findwhy.py --explain src/generated/api.ts /path/to/project
```

`--explain` exits 0 when the path would be included and 1 when it would not, so
it works as a shell test:

```bash
if python findwhy.py --explain build/out.log >/dev/null; then echo tracked; else echo hidden; fi
```

## Options

| flag | meaning |
|---|---|
| `-g, --glob` | pattern is a glob on the relative path, not a regex on the name |
| `-i, --ignore-case` | never use smart case |
| `--not` | keep the paths that do **not** match |
| `-t, --type f\|d\|l` | files, directories or symlinks (repeatable) |
| `-d, --max-depth N` | stop descending after N levels |
| `--min-depth N` | do not report anything above N levels |
| `-E, --exclude PATTERN` | extra rule, highest priority, absolute paths allowed, `!` re-includes |
| `-H, --hidden` | include dotfiles |
| `-I, --ignored` | include ignored paths |
| `-u, --unrestricted` | both of the above |
| `-a, --absolute` | print absolute paths |
| `-L, --follow` | follow symlinks, with cycle detection |
| `--parent` | also read ignore files above the search root |
| `--global-ignore PATH` | use this global ignore file, or `-` for none |
| `--explain PATH` | print the decision for one path |
| `--explain-all` | with `--explain`, list the rules that did not match too |
| `--skipped` | report what was dropped and why, instead of results |

This has 19 long options, including `--help`. Each one either changes what is
searched or changes what is explained; there are no formatting knobs.

## Design decisions

| decision | reason |
|---|---|
| one Python file | the tool must run anywhere a shell does, with no install step |
| 19 options | every option must pay for itself in the help text and the tests |
| a skipped-count footer when nothing matches | silence is the least useful answer a finder can give |
| `--skipped` lists every rule that fired | a count alone still leaves the user guessing |
| `--explain` prints the whole ancestor chain | the rule that matters may be several levels above the file |
| the search root is the boundary | searching inside an ignored directory should search it |
| `.gitignore` is read outside a git repo | the file states an intent even without a `.git` directory |
| global ignore is the lowest precedence | a project must be able to override a machine-wide default |
| 42,000 files, warm cache in 0.91 s | plain Python, one process, no index |

The trade is explicit: a compiled finder will beat a Python script on raw
throughput. findwhy spends that budget on making every decision printable.

## Numbers

- **96 tests**, `python -m unittest discover -s tests`, all passing.
- **Ignore semantics** checked against `git check-ignore -v` on a 73-path tree
  built from real-world patterns: **73/73 agreement** on the decision, and
  **45/45 agreement on the rule** that produced it (source file and line). With
  the dotfile gate on, the three differences are the `.gitignore` files
  themselves, which git tracks and findwhy hides like any other dotfile.
- **Output** checked on a tree of 16 repositories, 192 paths each: results are
  stable across runs and match the ignore decisions reported by `--explain`.

## Tests

```bash
python -m unittest discover -s tests
```

```
Ran 96 tests in 0.46s

OK
```

## License

MIT. See [LICENSE](LICENSE).
