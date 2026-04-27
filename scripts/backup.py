#!/usr/bin/env python3
"""
Snapshot quotidien des transactions Natura Tif depuis Supabase.

Usage :
    python scripts/backup.py               # snapshot table 'transactions' (prod)
    python scripts/backup.py --table transactions_test
    python scripts/backup.py --retention 90    # garde 90 jours (defaut)
    python scripts/backup.py --quiet           # pas de log stdout (pour tâche planifiée)

Prérequis : Python 3.8+, stdlib uniquement (pas de dépendance externe).

Sortie :
- backups/snapshot-YYYY-MM-DD-HHmm.json   (le snapshot)
- backups/_index.json                     (manifest des snapshots)

Authentification : signInAnonymously sur Supabase à chaque run (RLS exige une session).
La clé anon publique est la même que celle de la PWA — pas de secret ici.
"""
import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# --- Config (clé anon publique, déjà visible dans index.html) ---
SUPABASE_URL = 'https://rgwsqufbdimryxqefvhx.supabase.co'
SUPABASE_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InJnd3NxdWZiZGltcnl4cWVmdmh4Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzQ5NTI4OTQsImV4cCI6MjA5MDUyODg5NH0.VNIY3iq3WZz-SVveQdMMqNth2ZmBP7aoJJ3PhfsmN-0'

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKUPS_DIR = PROJECT_ROOT / 'backups'
INDEX_FILE = BACKUPS_DIR / '_index.json'

CTX = ssl.create_default_context()


def http(method, path, body=None, token=None, base=SUPABASE_URL):
    headers = {'apikey': SUPABASE_KEY, 'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    url = f'{base}{path}'
    data = json.dumps(body).encode('utf-8') if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=30) as r:
            raw = r.read().decode('utf-8')
            return r.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8') if e.fp else ''
        return e.code, {'error': body}


def sign_in_anonymously():
    """Crée une session anonyme et retourne le JWT."""
    code, data = http('POST', '/auth/v1/signup', body={})
    if code >= 400 or 'access_token' not in data:
        raise RuntimeError(f'Auth anonyme échouée (code {code}): {data}')
    return data['access_token']


def fetch_all(table, token):
    """Récupère toutes les lignes de la table, en paginant pour ne pas se prendre les limites."""
    rows = []
    offset = 0
    page_size = 1000
    while True:
        path = f'/rest/v1/{table}?select=*&order=date.asc&limit={page_size}&offset={offset}'
        code, data = http('GET', path, token=token)
        if code >= 400:
            raise RuntimeError(f'Lecture {table} échouée (code {code}): {data}')
        if not data:
            break
        rows.extend(data)
        if len(data) < page_size:
            break
        offset += page_size
    return rows


def write_snapshot(rows, table):
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    fname = f'snapshot-{now.strftime("%Y-%m-%d-%H%M")}.json'
    out = BACKUPS_DIR / fname
    payload = {
        'meta': {
            'table': table,
            'created_at': now.isoformat(),
            'row_count': len(rows),
            'tool': 'natura-tif backup.py',
        },
        'rows': rows,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    return out, payload['meta']


def update_index(meta, fname):
    idx = []
    if INDEX_FILE.exists():
        try:
            idx = json.loads(INDEX_FILE.read_text(encoding='utf-8'))
        except Exception:
            idx = []
    entry = {**meta, 'file': fname.name, 'size_bytes': fname.stat().st_size}
    idx.append(entry)
    idx.sort(key=lambda e: e['created_at'])
    INDEX_FILE.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding='utf-8')
    return idx


def apply_retention(retention_days, log):
    """Supprime les snapshots plus anciens que retention_days. Garde toujours le plus récent."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    kept, deleted = [], []
    snapshots = sorted(BACKUPS_DIR.glob('snapshot-*.json'))
    if len(snapshots) <= 1:
        return kept, deleted  # garde toujours au moins 1 snapshot
    # Le plus récent est toujours gardé
    latest = snapshots[-1]
    for snap in snapshots[:-1]:
        try:
            obj = json.loads(snap.read_text(encoding='utf-8'))
            created = datetime.fromisoformat(obj['meta']['created_at'].replace('Z', '+00:00'))
            if created < cutoff:
                snap.unlink()
                deleted.append(snap.name)
            else:
                kept.append(snap.name)
        except Exception as e:
            log(f'  ! impossible de lire {snap.name}: {e}')
    kept.append(latest.name)
    # Reconstruit l'index sur la base des snapshots restants
    if deleted:
        rebuild_index(log)
    return kept, deleted


def rebuild_index(log):
    new_idx = []
    for snap in sorted(BACKUPS_DIR.glob('snapshot-*.json')):
        try:
            obj = json.loads(snap.read_text(encoding='utf-8'))
            new_idx.append({**obj['meta'], 'file': snap.name, 'size_bytes': snap.stat().st_size})
        except Exception:
            pass
    INDEX_FILE.write_text(json.dumps(new_idx, ensure_ascii=False, indent=2), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description='Snapshot Natura Tif')
    parser.add_argument('--table', default='transactions', choices=['transactions', 'transactions_test'])
    parser.add_argument('--retention', type=int, default=90, help='Jours de rétention (défaut 90)')
    parser.add_argument('--quiet', action='store_true', help='Pas de log stdout')
    args = parser.parse_args()

    def log(msg):
        if not args.quiet:
            print(msg, flush=True)

    log(f'== Backup Natura Tif | table={args.table} | retention={args.retention}j ==')

    log('1. Auth anonyme Supabase...')
    token = sign_in_anonymously()

    log(f'2. Lecture {args.table}...')
    rows = fetch_all(args.table, token)
    log(f'   → {len(rows)} lignes récupérées')

    log('3. Écriture snapshot...')
    fname, meta = write_snapshot(rows, args.table)
    log(f'   → {fname.name} ({fname.stat().st_size} octets)')

    log('4. Mise à jour index...')
    idx = update_index(meta, fname)
    log(f'   → {len(idx)} snapshots indexés')

    log(f'5. Rétention {args.retention} jours...')
    kept, deleted = apply_retention(args.retention, log)
    if deleted:
        log(f'   → supprimés : {", ".join(deleted)}')
    else:
        log(f'   → rien à supprimer ({len(kept)} snapshots gardés)')

    result = {
        'ok': True,
        'snapshot': fname.name,
        'rows': len(rows),
        'size_bytes': fname.stat().st_size,
        'index_size': len(idx),
        'deleted_old': deleted,
    }
    if args.quiet:
        # Sortie JSON pour orchestration (cockpit / tâche planifiée)
        print(json.dumps(result), flush=True)
    else:
        log(f'\n✓ Terminé. Snapshot : {fname}')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:
        err = {'ok': False, 'error': str(e)}
        print(json.dumps(err) if '--quiet' in sys.argv else f'\n❌ ERREUR : {e}', flush=True)
        sys.exit(1)
