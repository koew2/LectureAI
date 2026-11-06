#!/usr/bin/env bash
# Run through deploy/systemd/eduflow-backup.service on a Linux host.
set -Eeuo pipefail
umask 077

: "${EDUFLOW_PROJECT_DIR:?Set EDUFLOW_PROJECT_DIR}"
: "${EDUFLOW_BACKUP_STAGING:?Set EDUFLOW_BACKUP_STAGING}"
: "${RESTIC_REPOSITORY:?Set RESTIC_REPOSITORY}"
: "${RESTIC_PASSWORD:?Set RESTIC_PASSWORD}"

for tool in docker python3 restic flock sha256sum realpath; do
    command -v "$tool" >/dev/null || { printf 'Missing tool: %s\n' "$tool" >&2; exit 1; }
done

project_dir="$(realpath -e -- "$EDUFLOW_PROJECT_DIR")"
if [[ "$project_dir" == / ]]; then
    printf 'The filesystem root cannot be the application checkout.\n' >&2
    exit 1
fi
case "$EDUFLOW_BACKUP_STAGING" in
    /*) ;;
    *) printf 'Backup staging must be an absolute path.\n' >&2; exit 1 ;;
esac
if [[ "$EDUFLOW_BACKUP_STAGING" == / ]]; then
    printf 'The filesystem root cannot be the backup staging directory.\n' >&2
    exit 1
fi
mkdir -p -- "$EDUFLOW_BACKUP_STAGING"
staging_dir="$(realpath -e -- "$EDUFLOW_BACKUP_STAGING")"
if [[ "$staging_dir" == / ]]; then
    printf 'The filesystem root cannot be the backup staging directory.\n' >&2
    exit 1
fi
if [[ "$staging_dir" == "$project_dir" || "$staging_dir" == "$project_dir/"* ]]; then
    printf 'Keep backup staging outside the application checkout.\n' >&2
    exit 1
fi
chmod 700 -- "$staging_dir"
exec 9>"$staging_dir/backup.lock"
flock -n 9 || { printf 'A backup is already running.\n' >&2; exit 1; }

compose=(docker compose --env-file "$project_dir/.env.production" -f "$project_dir/compose.production.yaml")
# Derive the real audio path from Compose so a second DATA_DIR setting cannot drift.
# The expanded config contains secrets; only the selected path is emitted.
audio_dir="$("${compose[@]}" config --format json | python3 -c '
import json, sys
config = json.load(sys.stdin)
mount = next(v for v in config["services"]["api"]["volumes"] if v["target"] == "/srv/eduflow/uploads")
if mount["type"] != "bind":
    raise SystemExit("Expected a bind mount for production audio")
print(mount["source"])
')"
audio_dir="$(realpath -e -- "$audio_dir")"
if [[ "$audio_dir" == / || "$audio_dir" == "$project_dir" || "$audio_dir" == "$project_dir/"* ]]; then
    printf 'Keep production audio in a separate data directory.\n' >&2
    exit 1
fi

work_dir="$staging_dir/current"
mkdir -p -- "$work_dir"
chmod 700 -- "$work_dir"
cleanup() {
    # Delete only the three known staging files, never user data or directories.
    rm -f -- "$work_dir/database.dump.partial" "$work_dir/database.dump" "$work_dir/database.dump.sha256"
}
trap cleanup EXIT

# Dump first, then read immutable audio files: later uploads may add harmless extra
# files, while every row in this DB snapshot already points to a fully saved file.
# If deletion/retention is introduced later, this ordering must be revisited.
"${compose[@]}" exec -T postgres sh -c \
    'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump --format=custom --no-owner --no-acl -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
    > "$work_dir/database.dump.partial"
test -s "$work_dir/database.dump.partial"
mv -- "$work_dir/database.dump.partial" "$work_dir/database.dump"
(cd -- "$work_dir" && sha256sum database.dump > database.dump.sha256)

restic backup --host eduflow-production --tag eduflow \
    --exclude "$project_dir/.git" \
    --exclude "$project_dir/.tools" \
    --exclude "$project_dir/.venv" \
    --exclude "$project_dir/.env.backup" \
    --exclude "$project_dir/backups" \
    --exclude '**/__pycache__' \
    --exclude '**/.pytest_cache' \
    --exclude '**/.mypy_cache' \
    --exclude '**/.ruff_cache' \
    -- "$work_dir" "$audio_dir" "$project_dir"

# Never prune if the repository check failed. Filters scope retention to this app.
restic check
restic forget --host eduflow-production --tag eduflow --group-by host,tags \
    --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune
printf 'EduFlow backup and repository check completed.\n'
