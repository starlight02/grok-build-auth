#!/usr/bin/env bash
# Conventional Commits validator for commit-msg hook
# Enforces single-line English commit messages with type(scope): subject format
set -euo pipefail

commit_msg_file="$1"
# Read first line only (Git commit message format)
commit_msg=$(head -n 1 "$commit_msg_file")

# Skip merge commits
if echo "$commit_msg" | grep -qE '^Merge (branch|pull request|remote-tracking branch)'; then
    exit 0
fi

# Check Conventional Commits format
if ! echo "$commit_msg" | grep -qE '^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\(.+\))?!?: .+$'; then
    echo "ERROR: Commit message does not follow Conventional Commits format" >&2
    echo "" >&2
    echo "Format: type(scope): subject" >&2
    echo "" >&2
    echo "Types:" >&2
    echo "  feat:     new feature" >&2
    echo "  fix:      bug fix" >&2
    echo "  docs:     documentation only" >&2
    echo "  style:    formatting (no code change)" >&2
    echo "  refactor: code refactoring" >&2
    echo "  perf:     performance improvement" >&2
    echo "  test:     add/update tests" >&2
    echo "  build:    build system / dependencies" >&2
    echo "  ci:       CI/CD configuration" >&2
    echo "  chore:    maintenance / tooling" >&2
    echo "  revert:   revert previous commit" >&2
    echo "" >&2
    echo "Your message:" >&2
    echo "  $commit_msg" >&2
    exit 1
fi

# Check subject length (recommended < 72 chars)
subject=$(echo "$commit_msg" | sed -E 's/^[^:]+: //')
subject_len=${#subject}
if [ "$subject_len" -gt 72 ]; then
    echo "WARNING: Commit subject is $subject_len chars (recommended < 72)" >&2
fi

# Check for English only using POSIX character classes (macOS grep compatible)
if printf '%s' "$commit_msg" | LC_ALL=C grep -qE '[^[:print:][:space:]]'; then
    echo "ERROR: Commit message must be in English (non-ASCII characters detected)" >&2
    exit 1
fi

echo "Commit message format OK"
