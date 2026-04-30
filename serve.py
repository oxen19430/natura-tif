#!/usr/bin/env python3
"""
Serveur local de développement pour Natura Tif.

Endpoints :
- GET  /api/status            : (legacy) compatibilité
- GET  /api/predeploy         : pre-deploy checks via scripts/deploy.py --dry-run
- POST /api/deploy            : commit + push via scripts/deploy.py
- POST /api/backup            : snapshot via scripts/backup.py
- GET  /api/backups           : liste des snapshots
"""
import http.server
import json
import os
import socketserver
import subprocess
import sys
import urllib.parse

PORT = int(os.environ.get('NT_PORT', '8765'))
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKUPS_DIR = os.path.join(PROJECT_DIR, 'backups')


def run_subproc(args, timeout=180):
    return subprocess.run(args, cwd=PROJECT_DIR, capture_output=True, text=True, timeout=timeout)


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=PROJECT_DIR, **kwargs)

    def log_message(self, format, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))

    def _send_json(self, code, payload):
        body = json.dumps(payload).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == '/api/status':
            return self.handle_status_legacy()
        if parsed.path == '/api/predeploy':
            return self.handle_predeploy()
        if parsed.path == '/api/backups':
            return self.handle_backups_list()
        return super().do_GET()

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        super().end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == '/api/deploy':
            return self.handle_deploy()
        if parsed.path == '/api/backup':
            return self.handle_backup()
        self._send_json(404, {'error': 'not found'})

    # ===== /api/status (legacy compatibilité minimale) =====
    def handle_status_legacy(self):
        # Compat avec ancien admin.html : on dit qu'il y a "rien à déployer" sans infos git
        # car ce dossier n'est plus le repo (le repo vit sur le MacBook / GitHub).
        return self._send_json(200, {
            'changed_files': [], 'changed_count': 0, 'commits_ahead': 0,
            'last_commit': '(repo source: github.com/oxen19430/natura-tif)',
            'has_changes_to_deploy': False,
            'note': 'Utilise /api/predeploy pour les checks réels',
        })

    # ===== /api/predeploy (GET) — checks live =====
    def handle_predeploy(self):
        script = os.path.join(PROJECT_DIR, 'scripts', 'deploy.py')
        if not os.path.exists(script):
            return self._send_json(500, {'error': 'scripts/deploy.py not found'})
        # On lance --dry-run --quiet --message "predeploy-check" --force pour récupérer les checks même si rouge
        # Note : --force passe outre, mais on récupère bien la liste des checks
        cmd = [sys.executable, script, '--message', 'predeploy-check', '--dry-run', '--quiet', '--force']
        try:
            proc = run_subproc(cmd, timeout=60)
        except subprocess.TimeoutExpired:
            return self._send_json(504, {'error': 'predeploy timeout'})
        out = proc.stdout.strip().splitlines()
        # En --dry-run + --force, on récupère le résultat final qui contient les checks
        last_line = out[-1] if out else '{}'
        try:
            data = json.loads(last_line)
        except json.JSONDecodeError:
            return self._send_json(500, {'error': 'parse error', 'stdout': proc.stdout, 'stderr': proc.stderr})
        return self._send_json(200, data)

    # ===== /api/deploy (POST) — push réel =====
    def handle_deploy(self):
        length = int(self.headers.get('Content-Length', '0') or 0)
        raw = self.rfile.read(length).decode('utf-8') if length else '{}'
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {}
        message = (payload.get('message') or '').strip()
        if not message:
            return self._send_json(400, {'error': 'message required'})
        force = bool(payload.get('force'))
        no_bump = bool(payload.get('no_bump'))

        script = os.path.join(PROJECT_DIR, 'scripts', 'deploy.py')
        if not os.path.exists(script):
            return self._send_json(500, {'error': 'scripts/deploy.py not found'})
        cmd = [sys.executable, script, '--message', message, '--quiet']
        if force:
            cmd.append('--force')
        if no_bump:
            cmd.append('--no-bump')

        try:
            proc = run_subproc(cmd, timeout=180)
        except subprocess.TimeoutExpired:
            return self._send_json(504, {'error': 'deploy timeout'})

        out = proc.stdout.strip().splitlines()
        last_line = out[-1] if out else '{}'
        try:
            data = json.loads(last_line)
        except json.JSONDecodeError:
            return self._send_json(500, {'error': 'parse error', 'stdout': proc.stdout, 'stderr': proc.stderr})
        # 200 si OK, 422 si checks rouges, 500 si autre erreur
        if data.get('ok'):
            return self._send_json(200, data)
        if data.get('step') == 'checks':
            return self._send_json(422, data)
        return self._send_json(500, data)

    # ===== /api/backup (POST) — snapshot =====
    def handle_backup(self):
        length = int(self.headers.get('Content-Length', '0') or 0)
        raw = self.rfile.read(length).decode('utf-8') if length else '{}'
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {}
        table = payload.get('table', 'transactions')
        if table not in ('transactions', 'transactions_test'):
            return self._send_json(400, {'error': 'invalid table'})
        retention = int(payload.get('retention', 90))

        script = os.path.join(PROJECT_DIR, 'scripts', 'backup.py')
        if not os.path.exists(script):
            return self._send_json(500, {'error': 'scripts/backup.py not found'})
        cmd = [sys.executable, script, '--table', table, '--retention', str(retention), '--quiet']
        try:
            proc = run_subproc(cmd, timeout=120)
        except subprocess.TimeoutExpired:
            return self._send_json(504, {'error': 'backup timeout'})
        out = proc.stdout.strip()
        if proc.returncode != 0:
            return self._send_json(500, {'step': 'run', 'returncode': proc.returncode, 'stdout': out, 'stderr': proc.stderr})
        try:
            result = json.loads(out)
        except json.JSONDecodeError:
            return self._send_json(500, {'step': 'parse', 'stdout': out, 'stderr': proc.stderr})
        return self._send_json(200, result)

    # ===== /api/backups (GET) — liste snapshots =====
    def handle_backups_list(self):
        if not os.path.isdir(BACKUPS_DIR):
            return self._send_json(200, {'snapshots': [], 'index_exists': False})
        index_path = os.path.join(BACKUPS_DIR, '_index.json')
        snapshots = []
        if os.path.exists(index_path):
            try:
                with open(index_path, 'r', encoding='utf-8') as f:
                    snapshots = json.load(f)
            except Exception as e:
                return self._send_json(500, {'error': f'index unreadable: {e}'})
        total_size = 0
        for entry in snapshots:
            fpath = os.path.join(BACKUPS_DIR, entry.get('file', ''))
            if os.path.exists(fpath):
                total_size += os.path.getsize(fpath)
        return self._send_json(200, {
            'snapshots': snapshots,
            'count': len(snapshots),
            'total_size_bytes': total_size,
            'index_exists': os.path.exists(index_path),
        })


def main():
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(('127.0.0.1', PORT), Handler) as httpd:
        print(f'Natura Tif (test) → http://localhost:{PORT}/')
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print('\nArrêt du serveur.')


if __name__ == '__main__':
    main()
