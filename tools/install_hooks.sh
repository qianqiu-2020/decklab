#!/bin/sh
# Install the public push gate for every worktree of this repository.
#
# core.hooksPath is set to an absolute path inside the shared git directory, so
# every worktree is gated and a global core.hooksPath cannot silently replace it.
# The installed hook runs the .githooks/pre-push of the checkout being pushed.
# Worktrees on commits older than the scanner use the copy saved here at install
# time; without either the push is refused. Re-run after the scanner changes.
set -eu
top=$(git rev-parse --show-toplevel)
common=$(git rev-parse --path-format=absolute --git-common-dir)
hooks="$common/hooks"
saved="$hooks/public-push"
mkdir -p "$hooks" "$saved" "$common/info"
cp "$top/tools/check_public_push.py" "$top/tools/public_push_allowlist.txt" "$saved/"
cat > "$hooks/pre-push" <<'HOOK'
#!/bin/sh
top=$(git rev-parse --show-toplevel) || exit 1
if [ -x "$top/.githooks/pre-push" ]; then
  exec "$top/.githooks/pre-push" "$@"
fi
saved="$(git rev-parse --path-format=absolute --git-common-dir)/hooks/public-push"
if [ ! -f "$saved/check_public_push.py" ]; then
  echo "pre-push: no public push scanner for $top; refusing to push unscanned commits" >&2
  exit 1
fi
python=python3
command -v python3 >/dev/null 2>&1 || python=python
echo "pre-push: $top predates the scanner; using the installed copy" >&2
exec "$python" "$saved/check_public_push.py" --allowlist "$saved/public_push_allowlist.txt" --pre-push "$@"
HOOK
chmod +x "$hooks/pre-push"
git config core.hooksPath "$hooks"
deny="$common/info/public-push-deny"
if [ ! -f "$deny" ]; then
  cat > "$deny" <<'DENY'
# Local only (inside the git directory, never committed or pushed).
# One case-insensitive Python regex per line for internal domains, hosts and
# other names that must never be published, for example:
#   (?<![\w-])corp-example\.com\b
DENY
  echo "created $deny; add internal domain patterns to it"
fi
echo "public push gate installed: core.hooksPath=$hooks (checkout $top)"
