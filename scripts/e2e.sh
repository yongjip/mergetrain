#!/usr/bin/env bash
#
# End-to-end test of the INSTALLED mergetrain CLI against real git repositories.
#
# Builds a wheel from the repo, installs it into a throwaway venv, then drives
# the real `mergetrain` console script through every major workflow against
# real bare-remote + clone sandboxes: init, enqueue, validate, deploy (atomic
# push), batches, merge conflicts, gate failures, post-push verify failures,
# the merge-train isolation fallback, push rejection, the runner lock and crash
# recovery, the auto daemon, gc, and cancel.
#
# Usage:   bash scripts/e2e.sh
# Env:     PYTHON=python3.12 bash scripts/e2e.sh   # choose the interpreter
#
# Exit code is non-zero if any assertion fails. No state is left behind.

PYTHON=${PYTHON:-python3}
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
PROJECT=$(cd "$SCRIPT_DIR/.." && pwd)

# Portable mktemp -d (GNU works bare; BSD/macOS needs a template).
WORK=$(mktemp -d 2>/dev/null || mktemp -d -t mt-e2e) || { echo "mktemp failed"; exit 1; }
cleanup(){ rm -rf "$WORK"; }
trap cleanup EXIT

VENV=$WORK/venv

echo "### mergetrain e2e ###"
echo "project: $PROJECT"
echo "python : $($PYTHON --version 2>&1)  ($PYTHON)"
echo "workdir: $WORK"

# The git config isolation below uses GIT_CONFIG_GLOBAL/SYSTEM, added in git
# 2.32. On older git these are silently ignored, which would make the suite
# read and mutate the developer's real ~/.gitconfig. Fail loudly instead.
gitver=$(git --version | { read -r _ _ v _; echo "${v%%[^0-9.]*}"; })
if ! printf '%s\n%s\n' "2.32" "$gitver" | sort -V -C 2>/dev/null; then
  echo "git >= 2.32 required for config isolation (have ${gitver:-unknown})" >&2
  exit 1
fi

echo "### build wheel + install into clean venv ###"
"$PYTHON" -m venv "$VENV" || { echo "venv failed"; exit 1; }
"$VENV/bin/python" -m pip install --quiet --upgrade pip build >/dev/null 2>&1 || { echo "pip bootstrap failed"; exit 1; }
"$VENV/bin/python" -m build --wheel --outdir "$WORK/dist" "$PROJECT" >/dev/null 2>&1 || { echo "BUILD FAILED"; exit 1; }
WHEEL=$(ls "$WORK"/dist/*.whl 2>/dev/null | head -1)
[ -n "$WHEEL" ] || { echo "no wheel produced"; exit 1; }
"$VENV/bin/python" -m pip install --quiet "$WHEEL" >/dev/null 2>&1 || { echo "INSTALL FAILED"; exit 1; }

MT="$VENV/bin/mergetrain"
VPY="$VENV/bin/python"
[ -x "$MT" ] || { echo "mergetrain console script not installed"; exit 1; }
echo "installed: $("$MT" --version)"

# Isolate from the developer's global git identity / hooks (git >= 2.32).
export GIT_CONFIG_GLOBAL="$WORK/gitconfig"
export GIT_CONFIG_SYSTEM=/dev/null
git config --file "$GIT_CONFIG_GLOBAL" init.defaultBranch main
git config --file "$GIT_CONFIG_GLOBAL" user.email e2e@example.invalid
git config --file "$GIT_CONFIG_GLOBAL" user.name "E2E Bot"
git config --file "$GIT_CONFIG_GLOBAL" commit.gpgsign false
git config --file "$GIT_CONFIG_GLOBAL" protocol.file.allow always

PASS=0; FAIL=0
declare -a FAILS
ok(){ echo "  PASS  $1"; PASS=$((PASS + 1)); }
no(){ echo "  FAIL  $1"; FAIL=$((FAIL + 1)); FAILS[${#FAILS[@]}]="$1"; }

# Read JSON from stdin (a pipe) and print a dotted/index path. Code is passed
# via -c so stdin stays connected to the pipe (a heredoc would steal stdin).
jget(){ "$VPY" -c '
import sys, json
d = json.load(sys.stdin)
for k in sys.argv[1].split("."):
    d = d[int(k)] if k.lstrip("-").isdigit() else d[k]
print("" if d is None else d)' "$1"; }

# Pull one job field out of a status/results payload by branch name.
job_field(){ "$VPY" -c '
import sys, json
data = json.load(sys.stdin)
branch, field = sys.argv[1], sys.argv[2]
for j in data["jobs"]:
    if j["branch"] == branch:
        print(j[field]); break
else:
    print("")' "$1" "$2"; }

# Execute SQL against a sandbox queue DB (stdlib sqlite3 — no sqlite3 CLI needed).
sqlexec(){ "$VPY" -c '
import sqlite3, sys
con = sqlite3.connect(sys.argv[1]); con.executescript(sys.argv[2]); con.commit(); con.close()' "$1" "$2"; }

gitq(){ git -C "$1" "${@:2}"; }
remote_main(){ git -C "$1/remote.git" rev-parse main 2>/dev/null; }
is_sha(){ echo "$1" | grep -Eq '^[0-9a-f]{40}$'; }

write_config(){ # $1=repo $2=mode
  repo="$1"; mode="$2"; d=$(dirname "$repo")
  marker="$d/gate.marker"; vmarker="$d/verify.marker"
  gate="$VPY -c \"from pathlib import Path; p=Path('$marker'); p.write_text((p.read_text() if p.exists() else '')+'x')\""
  verify="$VPY -c \"from pathlib import Path; Path('$vmarker').write_text('ok')\""
  pushrefs="    - main"
  case "$mode" in
    normal) ;;
    gatefail)     gate="$VPY -c \"import sys; sys.exit(1)\"";;
    sleep)        gate="$VPY -c \"import time; time.sleep(3)\"";;
    verifyfail)   verify="$VPY -c \"import sys; sys.exit(3)\"";;
    combinedfail) gate="$VPY -c \"import os, sys; sys.exit(1 if os.path.exists('b.txt') else 0)\"";;
    multiref)     pushrefs=$'    - main\n    - release';;
  esac
  cat > "$repo/.mergetrain.yaml" <<YAML
project:
  name: e2e
state:
  db: .mergetrain/queue.sqlite
  logs: .mergetrain/logs
  worktree_root: .mergetrain/worktrees
git:
  remote: origin
  integration_branch: main
  push_refs:
$pushrefs
gates:
  - name: marker
    run: $gate
deploy:
  verify:
    - name: verified
      run: $verify
YAML
}

# Fresh bare-remote + clone, config committed, state gitignored. Echoes the clone path.
setup(){ # $1=name [$2=mode]
  name="$1"; mode="${2:-normal}"; d="$WORK/$name"
  rm -rf "$d"; mkdir -p "$d"
  git init -q --bare "$d/remote.git"
  git clone -q "$d/remote.git" "$d/repo" >/dev/null 2>&1
  printf 'line1\nline2\n' > "$d/repo/app.txt"
  gitq "$d/repo" add app.txt
  gitq "$d/repo" commit -q -m base
  write_config "$d/repo" "$mode"
  printf '.mergetrain/\n' > "$d/repo/.gitignore"
  gitq "$d/repo" add .mergetrain.yaml .gitignore
  gitq "$d/repo" commit -q -m "mergetrain config"
  gitq "$d/repo" branch -M main
  gitq "$d/repo" push -q -u origin main >/dev/null 2>&1
  echo "$d/repo"
}

make_branch(){ # $1=repo $2=branch $3=file $4=content
  gitq "$1" switch -q -c "$2" main
  printf '%s\n' "$4" > "$1/$3"
  gitq "$1" add "$3"
  gitq "$1" commit -q -m "$2"
  gitq "$1" switch -q main
}

enq(){ "$MT" --repo "$1" enqueue --worktree "$1" "${@:2}"; }  # enqueue against the sandbox repo
ENQ="--capture-sha --allow-branch-mismatch"
section(){ echo; echo "=== $1 ==="; }

section "S0  Packaging smoke (installed console script)"
[ "$("$MT" --version)" = "mergetrain 0.1.0" ] && ok "version" || no "version=$("$MT" --version)"
"$MT" --help >/dev/null 2>&1 && ok "--help exit 0" || no "--help nonzero"
[ "$("$MT" agent-contract --json | jget boundary.deploy_requires)" = "run-next --deploy or run-batch --deploy" ] && ok "agent-contract well-formed (nested leaf)" || no "agent-contract"
"$VPY" -c "import mergetrain, mergetrain.cli, mergetrain.store, mergetrain.git_runner, mergetrain.daemon, mergetrain.dashboard, mergetrain.snapshot" 2>/dev/null && ok "imports" || no "imports"
"$VPY" -c "from pathlib import Path; import mergetrain; assert Path(mergetrain.__file__).with_name('dashboard_dist').joinpath('index.html').is_file()" 2>/dev/null && ok "dashboard assets packaged" || no "dashboard assets missing"
"$MT" dashboard --help >/dev/null 2>&1 && ok "dashboard help" || no "dashboard help failed"
"$MT" dashboard --host 0.0.0.0 >/dev/null 2>&1 && no "remote dashboard bind should require acknowledgement" || ok "remote dashboard bind refused without --allow-remote"

section "S1  init --write generates config + agent docs"
INITD="$WORK/initrepo"; rm -rf "$INITD"; mkdir -p "$INITD"; git init -q "$INITD"
"$MT" --repo "$INITD" init --project demo --write >/dev/null 2>&1
[ -f "$INITD/.mergetrain.yaml" ] && ok "config written" || no "config missing"
[ -f "$INITD/AGENTS.mergetrain.md" ] && ok "AGENTS.mergetrain.md" || no "AGENTS doc missing"
[ -f "$INITD/CLAUDE.mergetrain.md" ] && ok "CLAUDE.mergetrain.md" || no "CLAUDE doc missing"
"$MT" --repo "$INITD" init --project demo --write >/dev/null 2>&1 && no "re-init w/o --force should fail" || ok "re-init refused w/o --force"

section "S1b  doctor on UNCONFIGURED repo degrades safely"
NOCFG="$WORK/nocfg"; rm -rf "$NOCFG"; mkdir -p "$NOCFG"; git init -q "$NOCFG"
dj=$("$MT" --repo "$NOCFG" doctor --json); drc=$?
[ "$drc" = "0" ] && ok "doctor exit 0 on missing config" || no "doctor hard-failed rc=$drc"
[ "$(echo "$dj" | jget ok)" = "False" ] && ok "doctor ok=False (no config)" || no "ok not False"
[ "$(echo "$dj" | jget config_exists)" = "False" ] && ok "config_exists=False" || no "config_exists wrong"
[ "$(echo "$dj" | jget next_action)" = "enqueue_clean_branch" ] && ok "next_action=enqueue_clean_branch" || no "next_action wrong"

section "S1c  malformed config -> clean error, no traceback"
R=$(setup s1c)
printf 'project:\n  name: e2e\n bad-indent: x\n' > "$R/.mergetrain.yaml"
err=$("$MT" --repo "$R" doctor --json 2>&1); rc=$?
{ [ "$rc" = 1 ] && [ "$(echo "$err" | jget error.code 2>/dev/null)" = "config_error" ] && ! printf '%s' "$err" | grep -q 'Traceback'; } \
  && ok "malformed config: clean error, exit 1, no traceback" || no "malformed config rc=$rc err=$err"

section "S1d  explicit empty push refs fail closed"
R=$(setup s1d)
printf 'git:\n  remote: origin\n  integration_branch: main\n  push_refs: []\n' > "$R/.mergetrain.yaml"
err=$("$MT" --repo "$R" doctor --json 2>&1); rc=$?
{ [ "$rc" = 1 ] && [ "$(echo "$err" | jget error.code 2>/dev/null)" = "config_error" ] && echo "$err" | grep -q 'at least one ref'; } \
  && ok "empty push_refs rejected instead of defaulting to main" || no "empty push_refs accepted: rc=$rc err=$err"

section "S2/S3  enqueue, status, doctor, duplicate guard"
R=$(setup s2); make_branch "$R" feature/a a.txt aaa
ej=$(enq "$R" --task "feat a" --branch feature/a $ENQ --json)
[ "$(echo "$ej" | jget job.status)" = "queued" ] && ok "enqueue -> queued" || no "enqueue: $ej"
hs=$(echo "$ej" | jget job.head_sha); bs=$(echo "$ej" | jget job.base_sha)
is_sha "$hs" && ok "head_sha is a 40-hex SHA" || no "head_sha not a SHA: $hs"
is_sha "$bs" && ok "base_sha is a 40-hex SHA" || no "base_sha not a SHA: $bs"
[ "$hs" != "$bs" ] && ok "base_sha != head_sha (a real diff captured)" || no "base==head"
[ "$("$MT" --repo "$R" status --json | jget jobs.0.branch)" = "feature/a" ] && ok "status lists job" || no "status missing"
[ "$("$MT" --repo "$R" doctor --json | jget next_action)" = "run_batch_validate" ] && ok "doctor next_action=run_batch_validate" || no "doctor next_action wrong"
echo "$(enq "$R" --task dup --branch feature/a $ENQ --json 2>&1)" | grep -qi "already has an active job" && ok "duplicate active branch rejected" || no "dup not rejected"

section "S4  run-batch --validate-only  (NO push; gate runs once; verify skipped)"
R=$(setup s4); make_branch "$R" feature/a a.txt aaa
enq "$R" --task a --branch feature/a $ENQ >/dev/null 2>&1
before=$(remote_main "$(dirname "$R")")
rb=$("$MT" --repo "$R" run-batch --validate-only --json)
[ "$(echo "$rb" | jget jobs.0.status)" = "validated" ] && ok "job validated" || no "validate: $rb"
[ "$before" = "$(remote_main "$(dirname "$R")")" ] && ok "remote UNCHANGED by validate" || no "remote changed on validate!"
[ "$(cat "$(dirname "$R")/gate.marker" 2>/dev/null)" = "x" ] && ok "gate ran exactly once" || no "gate marker wrong"
[ -f "$(dirname "$R")/verify.marker" ] && no "verify ran on validate" || ok "verify skipped on validate"

section "S4b  validate -> approve -> deploy exact train after integration movement"
R=$(setup s4b); D=$(dirname "$R")
make_branch "$R" feature/a a.txt aaa; make_branch "$R" feature/b b.txt bbb
enq "$R" --task a --branch feature/a $ENQ >/dev/null 2>&1
enq "$R" --task b --branch feature/b $ENQ >/dev/null 2>&1
vr=$("$MT" --repo "$R" run-batch --validate-only --json)
tid=$(echo "$vr" | jget jobs.0.train_id)
[ -n "$tid" ] && [ "$tid" = "$(echo "$vr" | jget jobs.1.train_id)" ] && ok "validation records one shared train_id" || no "missing/mismatched train_id"
[ "$("$MT" --repo "$R" doctor --json | jget next_action)" = "deploy_validated_train_when_approved" ] && ok "doctor points to approved-train deploy" || no "doctor next_action wrong after validate"
[ "$("$MT" --repo "$R" gc --json | "$VPY" -c "import sys,json; print(len(json.load(sys.stdin)['branch_candidates']))")" = "0" ] && ok "validated branches excluded from GC" || no "validated branch appeared in GC"
make_branch "$R" feature/later later.txt later
enq "$R" --task later --branch feature/later $ENQ >/dev/null 2>&1
printf 'integration moved\n' > "$R/base-moved.txt"; gitq "$R" add base-moved.txt; gitq "$R" commit -q -m "move integration"; gitq "$R" push -q origin main
rb=$("$MT" --repo "$R" run-batch --deploy --json)
stj=$("$MT" --repo "$R" status --json)
{ [ "$(echo "$stj" | job_field feature/a status)" = deployed ] && [ "$(echo "$stj" | job_field feature/b status)" = deployed ]; } && ok "validated train deployed" || no "validated jobs not deployed"
[ "$(echo "$stj" | job_field feature/later status)" = queued ] && ok "newer queued job excluded from approved train" || no "new queued job was consumed"
[ "$(git -C "$D/remote.git" show main:base-moved.txt 2>/dev/null)" = "integration moved" ] && ok "integration movement preserved" || no "moved integration content missing"
git -C "$D/remote.git" show main:later.txt >/dev/null 2>&1 && no "new queued content leaked" || ok "new queued content not deployed"
[ "$(cat "$D/gate.marker" 2>/dev/null)" = "xx" ] && ok "gates reran before validated deploy" || no "validated deploy did not rerun gates"

section "S4c  changed task HEAD blocks validated deploy"
R=$(setup s4c); D=$(dirname "$R"); make_branch "$R" feature/a a.txt aaa
enq "$R" --task a --branch feature/a $ENQ >/dev/null 2>&1
"$MT" --repo "$R" run-batch --validate-only --json >/dev/null
before=$(remote_main "$D")
gitq "$R" switch -q feature/a; printf 'changed\n' > "$R/changed.txt"; gitq "$R" add changed.txt; gitq "$R" commit -q -m "change after validation"; gitq "$R" switch -q main
rb=$("$MT" --repo "$R" run-batch --deploy --json)
[ "$(echo "$rb" | jget jobs.0.status)" = "blocked" ] && ok "changed validated HEAD blocked" || no "changed HEAD was not blocked: $rb"
[ "$before" = "$(remote_main "$D")" ] && ok "remote unchanged after identity failure" || no "remote changed after identity failure"
[ "$(cat "$D/gate.marker" 2>/dev/null)" = "x" ] && ok "deploy gates skipped after identity failure" || no "gate unexpectedly reran"

section "S5  run-batch --deploy  (atomic push updates remote; verify runs)"
R=$(setup s5); make_branch "$R" feature/a a.txt aaa
enq "$R" --task a --branch feature/a $ENQ >/dev/null 2>&1
before=$(remote_main "$(dirname "$R")")
rb=$("$MT" --repo "$R" run-batch --deploy --json)
dsha=$(echo "$rb" | jget jobs.0.deploy_sha)
[ "$(echo "$rb" | jget jobs.0.status)" = "deployed" ] && ok "job deployed" || no "deploy: $rb"
after=$(remote_main "$(dirname "$R")")
[ "$before" != "$after" ] && ok "remote UPDATED by deploy" || no "remote not updated"
[ "$after" = "$dsha" ] && ok "remote main == deploy_sha (atomic push landed)" || no "remote!=deploy_sha"
[ "$(git -C "$(dirname "$R")/remote.git" show main:a.txt 2>/dev/null)" = "aaa" ] && ok "feature content on remote" || no "feature content missing"
[ "$(cat "$(dirname "$R")/verify.marker" 2>/dev/null)" = "ok" ] && ok "verify hook ran on deploy" || no "verify hook missing"

section "S5b  post-push verify FAILURE -> deployed + warning note (not failed)"
R=$(setup s5b verifyfail); make_branch "$R" feature/a a.txt aaa
enq "$R" --task a --branch feature/a $ENQ >/dev/null 2>&1
before=$(remote_main "$(dirname "$R")")
rb=$("$MT" --repo "$R" run-batch --deploy --json)
dsha=$(echo "$rb" | jget jobs.0.deploy_sha)
[ "$(echo "$rb" | jget jobs.0.status)" = "deployed" ] && ok "still DEPLOYED despite verify failure" || no "status not deployed: $rb"
after=$(remote_main "$(dirname "$R")")
{ [ "$before" != "$after" ] && [ "$after" = "$dsha" ]; } && ok "atomic push still landed before verify ran" || no "remote not at deploy_sha"
echo "$(echo "$rb" | jget jobs.0.note)" | grep -qi "post-push verify warning" && ok "failure recorded as non-blocking warning note" || no "no verify-warning note"
[ "$("$MT" --repo "$R" doctor --json | jget next_action)" = "enqueue_clean_branch" ] && ok "warned deploy is terminal-clean (not fix_blocked_job)" || no "doctor next_action wrong"

section "S5c  multiple push_refs (atomic fan-out to >1 ref)"
R=$(setup s5c multiref); D=$(dirname "$R")
gitq "$R" push -q origin main:release >/dev/null 2>&1   # create the second remote ref
make_branch "$R" feature/a a.txt aaa
enq "$R" --task a --branch feature/a $ENQ >/dev/null 2>&1
rb=$("$MT" --repo "$R" run-batch --deploy --json)
dsha=$(echo "$rb" | jget jobs.0.deploy_sha)
[ "$(echo "$rb" | jget jobs.0.status)" = "deployed" ] && ok "deployed with 2 push_refs" || no "multiref deploy: $rb"
[ "$(git -C "$D/remote.git" rev-parse main 2>/dev/null)" = "$dsha" ] && ok "main advanced to deploy_sha" || no "main not at deploy_sha"
[ "$(git -C "$D/remote.git" rev-parse release 2>/dev/null)" = "$dsha" ] && ok "release advanced to deploy_sha (fan-out)" || no "release not at deploy_sha"

section "S6  run-next single-job deploy"
R=$(setup s6); make_branch "$R" feature/solo a.txt solo
enq "$R" --task solo --branch feature/solo $ENQ >/dev/null 2>&1
rb=$("$MT" --repo "$R" run-next --deploy --json)
[ "$(echo "$rb" | jget jobs.0.status)" = "deployed" ] && ok "run-next deployed one job" || no "run-next: $rb"
[ "$(git -C "$(dirname "$R")/remote.git" show main:a.txt 2>/dev/null)" = "solo" ] && ok "run-next pushed to remote" || no "run-next remote content"

section "S7  batch of 2 non-conflicting branches (gate runs once)"
R=$(setup s7); make_branch "$R" feature/a a.txt aaa; make_branch "$R" feature/b b.txt bbb
enq "$R" --task a --branch feature/a $ENQ >/dev/null 2>&1
enq "$R" --task b --branch feature/b $ENQ >/dev/null 2>&1
rb=$("$MT" --repo "$R" run-batch --deploy --json)
s0=$(echo "$rb" | jget jobs.0.status); s1=$(echo "$rb" | jget jobs.1.status)
{ [ "$s0" = deployed ] && [ "$s1" = deployed ]; } && ok "both deployed" || no "batch: $s0,$s1"
[ "$(cat "$(dirname "$R")/gate.marker" 2>/dev/null)" = "x" ] && ok "gate ran ONCE for the train" || no "gate ran multiple times"
af=$(remote_main "$(dirname "$R")")
{ [ -n "$(git -C "$(dirname "$R")/remote.git" show "$af:a.txt" 2>/dev/null)" ] && [ -n "$(git -C "$(dirname "$R")/remote.git" show "$af:b.txt" 2>/dev/null)" ]; } && ok "both files on remote" || no "files missing on remote"

section "S8  merge conflict -> one blocked, other proceeds"
R=$(setup s8)
gitq "$R" switch -q -c feature/x main; printf 'X\nline2\n' > "$R/app.txt"; gitq "$R" commit -qam x; gitq "$R" switch -q main
gitq "$R" switch -q -c feature/y main; printf 'Y\nline2\n' > "$R/app.txt"; gitq "$R" commit -qam y; gitq "$R" switch -q main
enq "$R" --task x --branch feature/x $ENQ >/dev/null 2>&1
enq "$R" --task y --branch feature/y $ENQ >/dev/null 2>&1
rb=$("$MT" --repo "$R" run-batch --deploy --json)
allst=$(echo "$rb" | "$VPY" -c "import sys,json;print(','.join(sorted(j['status'] for j in json.load(sys.stdin)['jobs'])))")
echo "    statuses: [$allst]"
[ "$(echo "$rb" | jget result)" = "partial" ] && ok "partial batch reports result=partial" || no "partial result not reported"
echo "$allst" | grep -q blocked && ok "conflicting job blocked" || no "no blocked: $allst"
echo "$allst" | grep -q deployed && ok "other job deployed" || no "no deployed: $allst"

section "S8b  batch gate-failure ISOLATION (poison branch failed, sibling deployed)"
R=$(setup s8b combinedfail); D=$(dirname "$R")
make_branch "$R" feature/a a.txt aaa; make_branch "$R" feature/b b.txt bbb
enq "$R" --task a --branch feature/a $ENQ >/dev/null 2>&1
enq "$R" --task b --branch feature/b $ENQ >/dev/null 2>&1
before=$(remote_main "$D")
"$MT" --repo "$R" run-batch --deploy --json >/dev/null 2>&1
stj=$("$MT" --repo "$R" status --json)
[ "$(echo "$stj" | job_field feature/a status)" = "deployed" ] && ok "innocent branch deployed" || no "innocent not deployed"
[ "$(echo "$stj" | job_field feature/b status)" = "failed" ] && ok "poison branch isolated as failed" || no "poison not failed"
[ "$(git -C "$D/remote.git" show main:a.txt 2>/dev/null)" = "aaa" ] && ok "innocent content on remote" || no "innocent content missing"
git -C "$D/remote.git" show main:b.txt >/dev/null 2>&1 && no "poison content leaked to remote" || ok "poison content NOT on remote"
[ "$(remote_main "$D")" = "$(echo "$stj" | job_field feature/a deploy_sha)" ] && ok "remote == innocent deploy_sha" || no "remote != innocent deploy_sha"

section "S9  gate failure -> failed, remote NOT updated"
R=$(setup s9 gatefail); make_branch "$R" feature/a a.txt aaa
enq "$R" --task a --branch feature/a $ENQ >/dev/null 2>&1
before=$(remote_main "$(dirname "$R")")
rb=$("$MT" --repo "$R" run-batch --deploy --json); rc=$?
{ [ "$rc" = 1 ] && [ "$(echo "$rb" | jget ok)" = "False" ] && [ "$(echo "$rb" | jget jobs.0.status)" = "failed" ]; } \
  && ok "job failure returns ok=false and exit 1" || no "failure outcome incorrect: rc=$rc payload=$rb"
[ "$before" = "$(remote_main "$(dirname "$R")")" ] && ok "remote UNCHANGED (no push on gate fail)" || no "remote changed despite gate fail!"
[ "$("$MT" --repo "$R" doctor --json | jget next_action)" = "fix_blocked_job" ] && ok "doctor=fix_blocked_job" || no "doctor next_action wrong"

section "S9b  push REJECTED by remote -> failed, remote unchanged"
R=$(setup s9b); D=$(dirname "$R"); make_branch "$R" feature/a a.txt aaa
enq "$R" --task a --branch feature/a $ENQ >/dev/null 2>&1
hk="$D/remote.git/hooks/pre-receive"; printf '#!/bin/sh\necho "remote: ref locked" >&2\nexit 1\n' > "$hk"; chmod +x "$hk"
before=$(remote_main "$D")
rb=$("$MT" --repo "$R" run-batch --deploy --json)
[ "$(echo "$rb" | jget jobs.0.status)" = "failed" ] && ok "job failed on push rejection" || no "status not failed: $rb"
echo "$(echo "$rb" | jget jobs.0.note)" | grep -Eqi 'command failed|push|reject|locked' && ok "note reflects the push failure" || no "note not push-related"
[ "$before" = "$(remote_main "$D")" ] && ok "remote UNCHANGED after rejected push" || no "remote changed despite rejection!"
[ "$("$MT" --repo "$R" doctor --json | jget next_action)" = "fix_blocked_job" ] && ok "doctor=fix_blocked_job" || no "doctor next_action wrong"

section "S10  lock: concurrent run rejected while a runner holds it"
R=$(setup s10 sleep); make_branch "$R" feature/a a.txt aaa
jid=$(enq "$R" --task a --branch feature/a $ENQ --json | jget job.id)
"$MT" --repo "$R" run-batch --validate-only --json >/dev/null 2>&1 &
BG=$!
held=no
for i in $(seq 1 50); do
  [ "$("$MT" --repo "$R" status --json | jget lock.liveness 2>/dev/null)" = "alive" ] && { held=yes; break; }
  sleep 0.1
done
if [ "$held" = yes ]; then
  ok "background runner is provably holding the lock"
  second=$("$MT" --repo "$R" run-batch --validate-only --json 2>&1); rc=$?
  { [ $rc -ne 0 ] && echo "$second" | grep -qi "lock is held"; } && ok "concurrent run rejected with lock error" || no "concurrent run NOT rejected (rc=$rc): $second"
else
  no "lock never became alive within timeout"
fi

phase=""
gate_name=""
for i in $(seq 1 50); do
  inspection=$("$MT" --repo "$R" inspect "$jid" --json 2>/dev/null) || true
  phase=$(echo "$inspection" | jget progress.phase 2>/dev/null || true)
  gate_name=$(echo "$inspection" | jget progress.gate.name 2>/dev/null || true)
  { [ "$phase" = "gating" ] && [ "$gate_name" = "marker" ]; } && break
  sleep 0.05
done
{ [ "$phase" = "gating" ] && [ -n "$(echo "$inspection" | jget progress.heartbeat_at 2>/dev/null)" ] && [ "$gate_name" = "marker" ]; } \
  && ok "inspect exposes active gate and latest heartbeat" || no "inspect did not expose long gate: $inspection"

live_log="$WORK/s10-live.log"
event_stream="$WORK/s10-events.jsonl"
"$MT" --repo "$R" logs "$jid" --follow --tail 200 --poll-interval 0.05 >"$live_log" 2>&1 &
LOG_FOLLOWER=$!
"$MT" --repo "$R" events --job "$jid" --after 0 --follow --jsonl --poll-interval 0.05 >"$event_stream"
event_rc=$?
wait $BG 2>/dev/null
wait $LOG_FOLLOWER 2>/dev/null; log_rc=$?
"$VPY" -c '
import json, sys
rows=[json.loads(line) for line in open(sys.argv[1], encoding="utf-8") if line.strip()]
assert any(row.get("type") == "event" and row.get("phase") == "gating" and (row.get("gate") or {}).get("name") == "marker" for row in rows)
assert rows[-1]["type"] == "stream_end" and rows[-1]["reason"] == "success"
' "$event_stream" 2>/dev/null && [ "$event_rc" = 0 ] \
  && ok "events --follow JSONL frames long gate through clean success" || no "event follower failed rc=$event_rc: $(cat "$event_stream")"
{ [ "$log_rc" = 0 ] && grep -q 'gate: marker' "$live_log"; } \
  && ok "logs --follow streams the active runner log" || no "log follower failed rc=$log_rc: $(cat "$live_log")"

section "S10a  interrupted event follower exits 130 with a final frame"
R=$(setup s10a); make_branch "$R" feature/a a.txt aaa
jid=$(enq "$R" --task a --branch feature/a $ENQ --json | jget job.id)
interrupt_stream="$WORK/s10a-events.jsonl"
"$VPY" -c '
import json, signal, subprocess, sys, time
with open(sys.argv[4], "w", encoding="utf-8") as output:
    process = subprocess.Popen(
        [sys.argv[1], "--repo", sys.argv[2], "events", "--job", sys.argv[3], "--follow", "--jsonl", "--poll-interval", "0.05"],
        stdout=output,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.25)
    process.send_signal(signal.SIGINT)
    _, stderr = process.communicate(timeout=5)
rows=[json.loads(line) for line in open(sys.argv[4], encoding="utf-8") if line.strip()]
assert process.returncode == 130, (process.returncode, stderr)
assert rows[-1]["type"] == "stream_end" and rows[-1]["reason"] == "interrupted"
' "$MT" "$R" "$jid" "$interrupt_stream" 2>/dev/null \
  && ok "interrupted follower terminates cleanly" || no "interrupted follower contract failed: $(cat "$interrupt_stream")"

section "S10b  crash recovery: orphan in_progress reclaimed; live+expired held back"
# (A) No lock + orphan in_progress -> next runner requeues and deploys it.
R=$(setup s10ba); D=$(dirname "$R"); make_branch "$R" feature/a a.txt aaa
enq "$R" --task a --branch feature/a $ENQ >/dev/null 2>&1
sqlexec "$R/.mergetrain/queue.sqlite" "UPDATE deploy_queue SET status='in_progress', started_at='2000-01-01T00:00:00Z';"
before=$(remote_main "$D")
rb=$("$MT" --repo "$R" run-batch --deploy --json)
[ "$(echo "$rb" | jget jobs.0.status)" = "deployed" ] && ok "orphan in_progress requeued and deployed" || no "recovery: $rb"
[ "$before" != "$(remote_main "$D")" ] && ok "remote advanced after recovery" || no "remote not advanced"
# (C) Live owner + expired lease + in_progress -> refuse (operator must intervene).
R=$(setup s10bc); D=$(dirname "$R"); make_branch "$R" feature/a a.txt aaa
enq "$R" --task a --branch feature/a $ENQ >/dev/null 2>&1
sqlexec "$R/.mergetrain/queue.sqlite" "UPDATE deploy_queue SET status='in_progress'; INSERT INTO locks(name,owner,worktree_path,head_sha,acquired_at,expires_at) VALUES('runner','ghost:$$','','','2000-01-01T00:00:00Z','2000-01-01T00:00:00Z');"
before=$(remote_main "$D")
out=$("$MT" --repo "$R" run-batch --deploy --json 2>&1); rc=$?
{ [ $rc -ne 0 ] && echo "$out" | grep -qi "in-progress"; } && ok "live+expired lock with in_progress refused" || no "not refused: rc=$rc $out"
[ "$before" = "$(remote_main "$D")" ] && ok "remote unchanged while held back" || no "remote changed on refusal"

section "S11  daemon --once auto-only (deploys --auto, skips manual)"
R=$(setup s11); make_branch "$R" feature/auto a.txt aaa; make_branch "$R" feature/manual b.txt bbb
enq "$R" --task auto   --branch feature/auto   $ENQ --auto >/dev/null 2>&1
enq "$R" --task manual --branch feature/manual $ENQ        >/dev/null 2>&1
before=$(remote_main "$(dirname "$R")")
"$MT" --repo "$R" daemon --once >/dev/null 2>&1
stj=$("$MT" --repo "$R" status --json)
[ "$(echo "$stj" | job_field feature/auto status)" = deployed ] && ok "auto job deployed by daemon" || no "auto not deployed"
[ "$(echo "$stj" | job_field feature/manual status)" = queued ] && ok "manual job skipped (still queued)" || no "manual not queued"
[ "$before" != "$(remote_main "$(dirname "$R")")" ] && ok "remote updated by daemon" || no "remote not updated by daemon"

section "S12  gc cleans kept temporary worktrees"
R=$(setup s12); make_branch "$R" feature/a a.txt aaa
enq "$R" --task a --branch feature/a $ENQ >/dev/null 2>&1
"$MT" --repo "$R" run-batch --validate-only --keep-worktree --json >/dev/null 2>&1
cand=$("$MT" --repo "$R" gc --json | "$VPY" -c "import sys,json;print(len(json.load(sys.stdin).get('worktree_candidates',[])))")
[ "${cand:-0}" -ge 1 ] && ok "gc dry-run finds kept worktree (n=$cand)" || no "gc found no candidates"
"$MT" --repo "$R" gc --apply --json >/dev/null 2>&1
left=$(ls "$R/.mergetrain/worktrees" 2>/dev/null | wc -l | tr -d ' ')
[ "$left" = "0" ] && ok "gc --apply removed worktrees" || no "$left worktrees remain"

section "S13  cancel queued; re-enqueue after terminal; terminal cannot cancel"
R=$(setup s13); make_branch "$R" feature/a a.txt aaa
jid=$(enq "$R" --task a --branch feature/a $ENQ --json | jget job.id)
[ "$("$MT" --repo "$R" cancel "$jid" --json | jget job.status)" = "canceled" ] && ok "queued job canceled" || no "cancel failed"
"$MT" --repo "$R" cancel "$jid" --json >/dev/null 2>&1 && no "terminal cancel should fail" || ok "terminal job cannot be canceled"
[ "$(enq "$R" --task a2 --branch feature/a $ENQ --json | jget job.status)" = "queued" ] && ok "re-enqueue after terminal works" || no "re-enqueue failed"

echo
echo "=== RESULTS:  PASS=$PASS  FAIL=$FAIL ==="
if [ "$FAIL" -gt 0 ]; then
  printf '  - %s\n' "${FAILS[@]}"
  exit 1
fi
echo "ALL GREEN"
