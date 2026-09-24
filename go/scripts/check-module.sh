#!/bin/sh
# Release-readiness check for the Go module.
#
# 1. kei/runtimelink must belong to this module (a nested go.mod would split it
#    out of the module zip).
# 2. Its tests must pass from a module-cache-style copy of go/ -- a
#    "<module>@<version>" directory without the repository-level testing/
#    fixtures -- which is what `go test all` sees in a consumer.
set -eu
cd "$(dirname "$0")/.."

mod=$(go list -m)
pkg_mod=$(go list -f '{{.Module.Path}}' ./kei/runtimelink)
if [ "$pkg_mod" != "$mod" ]; then
	echo "check-module: kei/runtimelink belongs to $pkg_mod, not $mod" >&2
	exit 1
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
dst="$tmp/$mod@v0.0.0-check"
mkdir -p "$dst"
tar cf - --exclude .git . | (cd "$dst" && tar xf -)
(cd "$dst" && GOWORK=off go test -count=1 ./kei/runtimelink)
echo "check-module: $mod/kei/runtimelink ships in the module and tests pass outside the repository"
