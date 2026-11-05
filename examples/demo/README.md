# demo tree

A tiny project with a realistic ignore setup, used by the examples in the top
level README. Everything here is a fixture:

```
.gitignore            build output, dependencies, logs, one negation
src/.gitignore        a negation that can never work (the parent is excluded)
src/build/            excluded by the parent rule, negation shadowed
node_modules/         excluded by name
logs/                 one log kept by a negation, one dropped
.cache/               a dotfile directory
```

The ignored files are committed with `git add -f` on purpose: the demo only
works if they exist on disk and are excluded by the rules above them. If git
were left to apply those rules to itself, they would never reach the repository.

Try it from the repository root:

```bash
python findwhy.py log examples/demo
python findwhy.py --skipped examples/demo
python findwhy.py --explain examples/demo/src/build/out.log
```
