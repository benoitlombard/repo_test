#!/bin/sh

###############################################################################
# Paths
###############################################################################

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

ROOT_DIR="$SCRIPT_DIR/../.."

PROJECT_CHECK="$ROOT_DIR/advanced/gitar-check-no-lfs.sh"

# shellcheck source=common/common.sh
. "$ROOT_DIR/common/common.sh"

# shellcheck source=common/common_lock.sh
. "$ROOT_DIR/common/common_lock.sh"

# shellcheck source=common/common_file.sh
. "$ROOT_DIR/common/common_file.sh"

# shellcheck source=common/common_lock_no_lfs.sh
. "$ROOT_DIR/common/common_lock_no_lfs.sh"

###############################################################################
# Parameter validation
###############################################################################

[ "$#" -gt 0 ] || die "Usage: gitar-lock-no-lfs.sh <file>..."

###############################################################################
# Prerequisites
###############################################################################

"$PROJECT_CHECK" || exit 1

CURRENT_USER="$(get_current_user)"

[ -n "$CURRENT_USER" ] || \
    die "Git user.email is not configured."

###############################################################################
# Lock worktree setup
###############################################################################

ensure_lock_worktree || die "Unable to set up the lock worktree."

sync_lock_worktree || die "Unable to synchronize the lock worktree."

###############################################################################
# Dependency resolution
###############################################################################

FILES_TO_LOCK=

for TARGET_FILE
do
    RESOLVED_FILES="$(resolve_dependencies "$TARGET_FILE")" || \
        die "Unable to resolve dependencies for: $TARGET_FILE"

    if [ -n "$RESOLVED_FILES" ]
    then
        if [ -n "$FILES_TO_LOCK" ]
        then
            FILES_TO_LOCK="$FILES_TO_LOCK
$RESOLVED_FILES"
        else
            FILES_TO_LOCK="$RESOLVED_FILES"
        fi
    fi
done

FILES_TO_LOCK="$(printf '%s\n' "$FILES_TO_LOCK" |
    awk 'length($0) > 0 && !seen[$0]++')"

[ -n "$FILES_TO_LOCK" ] || die "No file to lock."

###############################################################################
# Summary
###############################################################################

printf '\n'
printf 'Files to lock\n'
printf '=============\n'

printf '%s\n' "$FILES_TO_LOCK"

printf '\n'

###############################################################################
# Lock files
###############################################################################

printf '%s\n' "$FILES_TO_LOCK" |
while IFS= read -r file
do

    [ -n "$file" ] || continue

    if file_not_exists "$file"
    then
        log_warn "File not found: $file"
        continue
    fi

    LOCK_OWNER="$(is_locked_by_someone_else "$file")"

    if [ -n "$LOCK_OWNER" ]
    then
        log_warn "Already locked by $LOCK_OWNER: $file"
        continue
    fi

    log_info "Locking: $file"

    if lock_file_no_lfs "$file"
    then
        git add "$file"
    else
        log_warn "Unable to lock: $file"
    fi

done

# Commit and push the lock immediately so other users see it right away.
commit_and_push_lock_worktree "Locked by $CURRENT_USER" || return 1

###############################################################################
# End
###############################################################################

printf '\n'
printf 'Lock operation completed.\n'