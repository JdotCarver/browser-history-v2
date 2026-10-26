#!/usr/bin/env bash
# tools/test-turn-start.sh
#
# Self-test for tools/arena-turn-start.sh.
#
# Run after ANY edit to the turn-start script:   bash tools/test-turn-start.sh
#
# It builds a throwaway clone in a temp directory and feeds the script every state
# it claims to handle. Safety properties, on purpose:
#   * the real repository is only ever READ (used as the source for a bare clone);
#   * the real 'origin' remote is never contacted, so no test can push or reset it;
#   * the temp copy is deleted on exit.
#
# Why this exists: the predecessor of the turn-start script would have hard-reset a
# branch with unpushed commits and eaten mid-turn work. Scenario 'ahead' and
# 'ahead+dirty' exist so that mistake can never come back silently. Dogfooding the
# script in a scratch clone is the cheapest way to keep that promise honest.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BRANCH="arena/b80febd6-browser-history-v2"
BASE_COMMIT="7d28c21a45101f3d75991c1de97686eaad2c85b9"

PASS=0
FAIL=0
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# --- fixture -----------------------------------------------------------------
# A bare clone of the real repo acts as the fake origin; a normal clone of that is
# the patient. Nothing here can reach the real remote.
setup_fixture() {
    # The re-clone and diverged-history scenarios reset the fixture to the base
    # commit, which only exists in a FULL clone. Platform re-clones arrive shallow,
    # so the base object can be missing (fatal for the resets, silent FAIL for the
    # scenarios). Unshallowing the real repo is the one exception to "the real
    # origin is never contacted": a fetch can only ADD objects, never lose work,
    # and without it the re-clone scenarios cannot run at all.
    if [ "$(git -C "$REPO_ROOT" rev-parse --is-shallow-repository)" = "true" ]; then
        git -C "$REPO_ROOT" fetch -q --unshallow origin 2>/dev/null ||
            echo "  (warning: real repo is shallow and could not be unshallowed — base-commit scenarios will fail)"
    fi
    rm -rf "$WORK/origin.git" "$WORK/repo"
    git clone --quiet --bare "$REPO_ROOT" "$WORK/origin.git"
    git clone --quiet --branch "$BRANCH" "$WORK/origin.git" "$WORK/repo"
    cd "$WORK/repo" || exit 1
    git config user.email "harness-selftest@localhost"
    git config user.name "harness selftest"
}

# Return the fixture to "clean and in sync" between scenarios.
clean_fixture() {
    cd "$WORK/repo" || exit 1
    git checkout -q "$BRANCH" 2>/dev/null
    git reset -q --hard "origin/$BRANCH"
    git clean -qfd
}

# Run the script under test and assert its output contains $1. $2 = scenario label.
expect() {
    local pattern="$1" label="$2" output
    output="$(bash tools/arena-turn-start.sh 2>&1)"
    if grep -qF -- "$pattern" <<<"$output"; then
        printf 'PASS  %s\n' "$label"
        PASS=$((PASS + 1))
    else
        printf 'FAIL  %s\n        expected text: %s\n' "$label" "$pattern"
        printf '        actual output:\n%s\n' "$output" | sed 's/^/          /'
        FAIL=$((FAIL + 1))
    fi
}

# The script under test may have been edited but not yet committed (that is the point
# of running this suite). Install the working copy into the fixture AND push it to the
# fixture's fake origin, so the fixture continues to see a clean repository that is in
# sync with its remote. Anything less (e.g. copying the file without committing it)
# leaves the fixture dirty and every later expectation about a clean tree is wrong.
assert_script_present() {
    if cmp -s "$REPO_ROOT/tools/arena-turn-start.sh" tools/arena-turn-start.sh; then
        return
    fi
    echo "note: repository copy of the script differs (uncommitted edit) — installing it in the fixture"
    cp "$REPO_ROOT/tools/arena-turn-start.sh" tools/arena-turn-start.sh
    if [ "$(git rev-parse --abbrev-ref HEAD)" = "$BRANCH" ]; then
        git add tools/arena-turn-start.sh
        git commit -q -m "fixture: install script under test"
        # Push to the FIXTURE's origin (a temp bare clone) so the fixture still looks
        # in sync — but only when that is a fast-forward. A scenario that deliberately
        # rewound the fixture branch (scenario 9) must stay rewound; a rejected push
        # there would be expected noise, so it is skipped instead.
        if git merge-base --is-ancestor "origin/$BRANCH" HEAD 2>/dev/null; then
            git push -q origin "HEAD:$BRANCH"
        else
            echo "     (fixture branch is deliberately rewound — skipping the sync push)"
        fi
    fi
}

echo "== self-test: tools/arena-turn-start.sh"
echo "   fixture: $WORK"
setup_fixture

# 1. The boring case.
clean_fixture; assert_script_present
expect "clean and in sync with origin/$BRANCH" "clean tree, in sync"

# 2. Uncommitted work at the remote tip must survive.
clean_fixture; assert_script_present
echo "wip" > wip.txt
expect "tree dirty — leaving uncommitted work ALONE" "dirty tree at remote tip is left alone"
rm -f wip.txt

# 3. Unpushed commits must survive (the original sin this script exists to avoid).
clean_fixture; assert_script_present
echo "work" > committed.txt
git add -A && git commit -q -m "local ahead"
expect "AHEAD of origin/$BRANCH" "unpushed commit is left alone"

# 4. ... and still survive when the tree is dirty on top of that commit.
echo "more" > wip.txt
expect "AHEAD of origin/$BRANCH" "unpushed commit + dirty tree is left alone"
rm -f wip.txt

# 5. Behind the remote (user pushed between turns) -> fast-forward.
#    A --mixed reset reproduces the real re-clone: HEAD at the base commit while the
#    working tree still holds the restored files. (A --hard reset here would restore
#    the base version of the script itself and test the wrong code.)
clean_fixture
git reset -q --mixed "$BASE_COMMIT"
assert_script_present
expect "RE-CLONE DETECTED" "HEAD at base commit is detected as a re-clone"
if [ "$(git rev-parse HEAD)" = "$(git rev-parse "origin/$BRANCH")" ]; then
    printf 'PASS  re-clone repair lands on the remote tip\n'; PASS=$((PASS + 1))
else
    printf 'FAIL  re-clone repair did not land on the remote tip (HEAD=%s)\n' "$(git rev-parse --short HEAD)"
    FAIL=$((FAIL + 1))
fi

# 6. No remote branch yet (first turn) -> info, not an abort.
clean_fixture; assert_script_present
git checkout -q -b arena/phantom
sed -i "s|^BRANCH=.*|BRANCH=\"arena/phantom\"|" tools/arena-turn-start.sh
expect "no remote branch" "missing remote branch is reported, not fatal"
git checkout -q -- tools/arena-turn-start.sh

# 7. Wrong branch checked out, clean tree -> switch back automatically.
clean_fixture
git checkout -q -B stray "$BRANCH"        # stray starts at the same commit
assert_script_present
expect "switching back to the session branch" "clean tree on a stray branch is switched back"

# 8. Wrong branch checked out, dirty tree -> refuse loudly with recovery steps.
#    Needs its own setup: scenario 7's run already switched back to the session branch.
clean_fixture
git checkout -q -B stray "$BRANCH"
echo "unsaved" > wip.txt
assert_script_present
expect "NOT switching automatically" "dirty tree on a stray branch is refused"
rm -f wip.txt

# 9. Diverged histories (the mid-turn re-clone signature): both sides have unique
#    commits. The script must refuse, and must explain the re-parenting repair.
#    Reproduced faithfully: local branch = base commit + one local commit, while the
#    remote branch holds this session's earlier commits.
clean_fixture
git checkout -q -B "$BRANCH" "$BASE_COMMIT"   # the rewind also restores base files
assert_script_present                          # put the current script back first
echo "local snapshot" > snapshot.txt
git add -A && git commit -q -m "rebuilt on base after a re-clone"
expect "DIVERGED" "diverged histories are refused"
expect "git reset --soft origin/$BRANCH" "diverged histories get the re-parenting recipe"

echo "== result: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
