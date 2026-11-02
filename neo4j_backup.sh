#!/usr/bin/env bash
# Offline Neo4j Community backup/restore using official neo4j-admin dump/load.
# The output is a .tar.gz containing neo4j.dump + manifest.json (SHA-256).
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
SERVICE="${NEO4J_SERVICE:-neo4j}"
DATABASE="${NEO4J_BACKUP_DATABASE:-neo4j}"
BACKUP_DIR="${NEO4J_BACKUP_DIR:-$SCRIPT_DIR/backups}"
mkdir -p -- "$BACKUP_DIR"
BACKUP_DIR="$(cd -- "$BACKUP_DIR" && pwd)"
chmod 700 -- "$BACKUP_DIR"

for bin in docker python3; do
  command -v "$bin" >/dev/null || { echo "ERRORE: manca $bin" >&2; exit 1; }
done
docker compose version >/dev/null || { echo 'ERRORE: docker compose non disponibile' >&2; exit 1; }
[[ "$DATABASE" =~ ^[a-zA-Z0-9_-]+$ ]] || { echo 'ERRORE: nome database non valido' >&2; exit 1; }

WORK_DIR=''
WAS_RUNNING=0
STOPPED=0
LEAVE_STOPPED=0
cleanup() {
  code=$?
  trap - EXIT
  if (( STOPPED && WAS_RUNNING && ! LEAVE_STOPPED )); then
    echo '[Neo4j] Riavvio del servizio...'
    if ! docker compose start "$SERVICE"; then
      echo 'ERRORE: riavvio Neo4j fallito; verificare docker compose logs.' >&2
      code=1
    fi
  elif (( LEAVE_STOPPED )); then
    echo 'ATTENZIONE: restore fallito dopo l’avvio della sostituzione: Neo4j resta fermo.' >&2
    echo 'Ripristinare dal backup di sicurezza creato prima del restore.' >&2
  fi
  [[ -z "$WORK_DIR" ]] || rm -rf -- "$WORK_DIR"
  exit "$code"
}
trap cleanup EXIT

compose_container() {
  local id
  id="$(docker compose ps -aq "$SERVICE" | head -n 1)"
  [[ -n "$id" ]] || { echo "ERRORE: container $SERVICE inesistente. Avvialo almeno una volta con docker compose up -d." >&2; exit 1; }
  echo "$id"
}

image_of() { docker inspect -f '{{.Config.Image}}' "$1"; }

stop_if_running() {
  local id="$1" running
  running="$(docker inspect -f '{{.State.Running}}' "$id")"
  if [[ "$running" == true ]]; then
    WAS_RUNNING=1
    echo "[Neo4j] Arresto del servizio $SERVICE (backup offline)..."
    docker compose stop -t 90 "$SERVICE"
    STOPPED=1
  fi
  running="$(docker inspect -f '{{.State.Running}}' "$id")"
  [[ "$running" == false ]] || { echo 'ERRORE: container ancora in esecuzione!' >&2; exit 1; }
}

# The official Neo4j admin CLI streams the .dump directly to stdout,
# avoiding permissions problems on Docker bind mounts.
dump_database() {
  local destination="$1"
  docker compose run --rm --no-deps -T \
    --user neo4j --entrypoint neo4j-admin "$SERVICE" \
    database dump "$DATABASE" --to-stdout > "$destination"
  [[ -s "$destination" ]] || { echo 'ERRORE: dump vuoto' >&2; exit 1; }
  # Check the internal Neo4j dump format without actually restoring it.
  docker compose run --rm --no-deps -T \
    --user neo4j --entrypoint neo4j-admin "$SERVICE" \
    database load "$DATABASE" --from-stdin --info < "$destination" >/dev/null
}

make_archive() {
  local dump="$1" archive="$2" image="$3"
  python3 - "$dump" "$archive" "$DATABASE" "$image" <<'PY'
import datetime, hashlib, json, pathlib, sys, tarfile, io
src, out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
name, image = sys.argv[3:5]
hash_ = hashlib.sha256()
with src.open('rb') as file:
    for block in iter(lambda: file.read(1024 * 1024), b''):
        hash_.update(block)
manifest = {
    'format': 'presskg-neo4j-dump-targz-v1',
    'database': name,
    'neo4j_image': image,
    'created_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'dump_file': f'{name}.dump',
    'dump_sha256': hash_.hexdigest(),
    'dump_size_bytes': src.stat().st_size,
    'note': 'Database-only dump; Neo4j system users and credentials are not included.'
}
serialized = json.dumps(manifest, indent=2, ensure_ascii=False).encode()
with tarfile.open(out, 'w:gz') as archive:
    archive.add(src, arcname=f'{name}.dump', recursive=False)
    meta = tarfile.TarInfo('manifest.json')
    meta.size = len(serialized)
    meta.mode = 0o600
    archive.addfile(meta, io.BytesIO(serialized))
print('Archivio:', out)
print('Dump SHA-256:', hash_.hexdigest())
PY
}

# Validate tar member names, types and dump checksum. Extract via file streams,
# not tar.extractall(), to avoid path traversal/symlink issues.
verify_archive() {
  local archive="$1" extracted="$2"
  python3 - "$archive" "$extracted" <<'PY'
import hashlib, json, pathlib, sys, tarfile
archive_path, dest = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
with tarfile.open(archive_path, 'r:gz') as archive:
    members = archive.getmembers()
    names = [m.name for m in members]
    if len(names) != 2 or len(set(names)) != 2 or 'manifest.json' not in names:
        raise ValueError('Archivio inatteso: previsti solo manifest.json e database.dump')
    if not all(m.isfile() for m in members):
        raise ValueError('Archivio contiene membri non regolari (directory, link, ecc.)')
    with archive.extractfile('manifest.json') as stream:
        manifest = json.load(stream)
    if manifest.get('format') != 'presskg-neo4j-dump-targz-v1':
        raise ValueError('Formato backup non riconosciuto')
    db = manifest.get('database', '')
    if not db or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in db):
        raise ValueError('Nome database non valido nel manifest')
    dumpname = f'{db}.dump'
    if sorted(names) != sorted([dumpname, 'manifest.json']):
        raise ValueError('Nome dump non corrispondente al manifest')
    if str(dest) != '-':
        dest.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    total = 0
    with archive.extractfile(dumpname) as source:
        target = dest.open('wb') if str(dest) != '-' else None
        try:
            while True:
                block = source.read(1024 * 1024)
                if not block: break
                digest.update(block)
                total += len(block)
                if target is not None:
                    target.write(block)
        finally:
            if target is not None:
                target.close()
    if digest.hexdigest() != manifest.get('dump_sha256') or total != manifest.get('dump_size_bytes'):
        if str(dest) != '-':
            dest.unlink(missing_ok=True)
        raise ValueError('Checksum o dimensione del dump errati')
print('VERIFICATO: database=', db, 'image=', manifest.get('neo4j_image'), 'bytes=', total)
if str(dest) != '-':
    (dest.parent / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
PY
}

new_archive_name() {
  local prefix="$1" stamp
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  printf '%s/%s_%s_%s.tar.gz' "$BACKUP_DIR" "$prefix" "$DATABASE" "$stamp"
}

create_backup() {
  local nameprefix="$1" image="$2" target temp
  target="$(new_archive_name "$nameprefix")"
  [[ ! -e "$target" ]] || { echo "ERRORE: file già esistente: $target" >&2; exit 1; }
  temp="$WORK_DIR/$(basename "$target").tmp"
  echo "[Backup] Dump offline database: $DATABASE"
  dump_database "$WORK_DIR/$DATABASE.dump"
  make_archive "$WORK_DIR/$DATABASE.dump" "$temp" "$image"
  # Verify the tar+checksum before exposing the final backup filename.
  verify_archive "$temp" "-"
  mv -- "$temp" "$target"
  echo "[Backup] Creato: $target"
  printf '%s\n' "$target"
}

usage() {
  cat <<'EOF'
Uso (dalla stessa directory del compose.yaml):
  ./neo4j_backup.sh backup
  ./neo4j_backup.sh list
  ./neo4j_backup.sh verify backups/backup_neo4j_....tar.gz
  ./neo4j_backup.sh restore backups/backup_neo4j_....tar.gz
  ./neo4j_backup.sh restore backups/backup_neo4j_....tar.gz --yes

Variabili opzionali: NEO4J_SERVICE=neo4j NEO4J_BACKUP_DATABASE=neo4j
                    NEO4J_BACKUP_DIR=/path/backup
Backup e restore richiedono Neo4j fermo; lo script lo arresta/riavvia.
Il restore sostituisce il database e prima salva automaticamente lo stato attuale.
EOF
}

action="${1:-}"
case "$action" in
  list)
    find "$BACKUP_DIR" -maxdepth 1 -type f -name '*.tar.gz' -printf '%f\n' | sort
    ;;
  verify)
    [[ $# -eq 2 ]] || { usage; exit 2; }
    [[ -f "$2" ]] || { echo "Archivio non trovato: $2" >&2; exit 1; }
    WORK_DIR="$(mktemp -d "$BACKUP_DIR/.neo4j-work-XXXXXXXX")"
    verify_archive "$2" "-"
    ;;
  backup)
    [[ $# -eq 1 ]] || { usage; exit 2; }
    id="$(compose_container)"
    image="$(image_of "$id")"
    WORK_DIR="$(mktemp -d "$BACKUP_DIR/.neo4j-work-XXXXXXXX")"
    stop_if_running "$id"
    create_backup backup "$image"
    ;;
  restore)
    [[ $# -eq 2 || ( $# -eq 3 && "$3" == '--yes' ) ]] || { usage; exit 2; }
    [[ -f "$2" ]] || { echo "Archivio non trovato: $2" >&2; exit 1; }
    id="$(compose_container)"
    image="$(image_of "$id")"
    WORK_DIR="$(mktemp -d "$BACKUP_DIR/.neo4j-work-XXXXXXXX")"
    # Validate before stopping the database or asking for confirmation.
    verify_archive "$2" "$WORK_DIR/restore.dump"
    read -r archive_db archive_image < <(python3 - "$WORK_DIR/manifest.json" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
print(m['database'], m['neo4j_image'])
PY
)
    [[ "$archive_db" == "$DATABASE" ]] || { echo "ERRORE: archivio per $archive_db, database scelto $DATABASE" >&2; exit 1; }
    [[ "$archive_image" == "$image" ]] || { echo "ERRORE: versione immagine differente ($archive_image vs $image). Usare l'immagine Neo4j originale per un restore sicuro." >&2; exit 1; }
    if [[ "${3:-}" != '--yes' ]]; then
      echo "ATTENZIONE: ripristinerai il database $DATABASE dall'archivio: $2"
      echo 'Lo stato attuale sarà salvato in un backup di sicurezza prima di essere sostituito.'
      read -r -p 'Digita RESTORE per confermare: ' reply
      [[ "$reply" == RESTORE ]] || { echo 'Annullato.'; exit 0; }
    fi
    stop_if_running "$id"
    echo '[Restore] Backup di sicurezza dello stato corrente...'
    create_backup before_restore "$image"
    # From here a load failure can leave partially replaced database files:
    # DO NOT automatically start Neo4j on failure.
    LEAVE_STOPPED=1
    echo '[Restore] Caricamento dump (sovrascrittura database)...'
    docker compose run --rm --no-deps -T \
      --user neo4j --entrypoint neo4j-admin "$SERVICE" \
      database load "$DATABASE" --from-stdin --overwrite-destination=true \
      < "$WORK_DIR/restore.dump"
    LEAVE_STOPPED=0
    echo '[Restore] Caricamento completato.'
    ;;
  *) usage; exit 2 ;;
esac
