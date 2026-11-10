# Agent code execution through casemgr: open risks

Status (2026-11-09): **open, not fixed.** Found while hardening hpc-connect
(`refactor/plugin` branch, plan §4).

## Why this matters now

hpc-connect lists this repo's Discover checkout in the site's `script_roots`.
A `.py` under a script root is *vetted*: at profile `operate` and above, an
agent runs it with no confirmation token, and the payload guard does not look
inside it. That is deliberate. The guard reads hand-written shell; it cannot
read this repo's Python either way.

The guarantee rests on one assumption: **vetted code only runs code the user
reviewed.** hpc-connect now enforces its half: an agent can no longer push,
copy or redirect a code file (`.py`, `.sh`, and so on) into a script root.
Code reaches this checkout only through `git pull`. The risks below are this
repo's half: places where vetted casemgr code runs code from somewhere an agent
*can* write.

## 1. `build.py make` runs scripts from any directory

`generate` writes `*_build.sh` into `scripts_dir`; `make` runs every
`*_build.sh` it finds there. `scripts_dir` comes from:

- `--scripts-dir DIR` (build.py ~line 3046): any directory on the command line;
- the default `'build_scripts'`, which is **relative to the working
  directory**, not to the repo. `python $HPC_REPO/build.py make` run from
  `~` reads `~/build_scripts/`.

Either way an agent can put an edited `.sh` in a directory it may write
(scratch, home), then run `build.py make`, a vetted script, with no token.
The hand-edited script runs unreviewed.

**Fix:** remove `--scripts-dir` and anchor the folder to the repo:

```python
SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "build_scripts")
```

`make` then runs only scripts inside the script root, where hpc-connect no
longer lets an agent place one. Losing the per-experiment folders costs
little: in practice only the matrices are read, never the scripts. If
grouping comes back, use a subfolder name checked to stay under
`build_scripts/`, not a free-form path.

## 2. Matrix values reach the generated shell unquoted

`generate` builds the script with f-strings: `./xmlchange RUN_TYPE={run_type}`,
`-val {spec['ntasks']}`, `mkdir -p SourceMods/...`, `sed` lines. `shlex` is
imported, but `shlex.quote` is used once (~line 1237). A matrix is data, and
hpc-connect lets an agent push data into a script root. So a string value
such as `run_type: "hybrid; curl ... | bash"` becomes code in the generated
script, which `make` then runs. `generate --verify` checks types, which
protects numeric fields but not strings.

**Fix:** quote every interpolated value with `shlex.quote`, or validate it
against a strict pattern before use (case names `^[A-Za-z0-9_.-]+$`, enums
against their allowed set). Do both for case names, which also become paths.

## 3. The documented workflow pushes generated scripts

`CLAUDE.md` (line 18) and the `exocam-model-rules` skill say: *build scripts
are generated locally, reviewed, then run on the cluster*, which means pushing
`build_scripts/*.sh` into the Discover checkout. hpc-connect now refuses that
push (a code file into a script root). The audit has one instance:
`build_scripts/exocam_ML_grp4_pt05_top1pa_build.sh`.

**New workflow:** push the matrix (data; allowed), run `build.py generate` on
Discover, review the generated script there (`hpc-read`, or `hpc-pull` it to
read locally), then `build.py make`. The script is produced by vetted code
from a matrix, not hand-placed. Update `CLAUDE.md` and the skill once fix 1
lands.

## The general rule for vetted scripts in this repo

A script in a script root must not run code from a location or a value an
agent can write. Check each verb for:

- a flag or default that names a directory whose contents get executed (1);
- data values interpolated into shell, `subprocess(..., shell=True)`, or
  `eval`/`exec` without quoting or validation (2);
- imports or `sys.path` changes driven by the working directory or arguments.

Until these are fixed, a `script_confirm` pattern in the hpc-connect site file
puts the risky form back behind a prompt, for example
`"*build.py *make*"`.
