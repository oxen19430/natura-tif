#!/usr/bin/env python3
"""
Serveur local de développement pour Natura Tif.

- Sert les fichiers statiques de l'app (cache désactivé pour voir les modifs immédiatement)
- /api/status            : état git (changements non déployés, commits ahead, dernier commit)
- /api/deploy   (POST)   : commit + push vers GitHub
- /api/backup   (POST)   : déclenche scripts/backup.py et renvoie le résultat
- /api/backups  (GET)    : liste les snapshots (lit backups/_index.json)
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


def run_git(*args):
    return subprocess.run(['git'] + list(args), cwd=PROJECT_DIR, capture_output=True, text=True)


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
            return self.handle_status()
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

    # ===== /api/status =====
    def handle_status(self):
        status = run_git('status', '--porcelain')
        if status.returncode != 0:
            return self._send_json(500, {'error': status.stderr.strip()})
        lines = [l for l in status.stdout.splitlines() if l.strip()]
        relevant = [l for l in lines if not l.startswith('??') or any(l.endswith(ext) for ext in ('.html', '.js', '.json', '.png', '.py', '.md'))]
        last = run_git('log', '-1', '--format=%h %s (%cr)')
        ahead = run_git('rev-list', '--count', '@{u}..HEAD')
        ahead_n = int(ahead.stdout.strip() or '0') if ahead.returncode == 0 else 0
        return self._send_json(200, {
            'changed_files': [l[3:] if len(l) > 3 else l for l in relevant],
            'changed_count': len(relevant),
            'commits_ahead': ahead_n,
            'last_commit': last.stdout.strip(),
            'has_changes_to_deploy': len(relevant) > 0 or ahead_n > 0,
        })

    # ===== /api/deploy =====
    def handle_deploy(self):
        length = int(self.headers.get('Content-Length', '0') or 0)
        raw = self.rfile.read(length).decode('utf-8') if length else '{}'
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {}
        message = (payload.get('message') or '').strip() or 'Update from local test environment'

        add = run_git('add', '-u')
        if add.returncode != 0:
            return self._send_json(500, {'step': 'add', 'error': add.stderr.strip()})
        for fname in ('index.html', 'sw.js', 'manifest.json', 'cockpit.html', 'icon-192.png', 'icon-512.png'):
            run_git('add', fname)

        diff = run_git('diff', '--cached', '--quiet')
        had_staged = diff.returncode == 1
        commit_output = ''
        if had_staged:
            commit = run_git('commit', '-m', message)
            commit_output = commit.stdout.strip() + '\n' + commit.stderr.strip()
            if commit.returncode != 0:
                return self._send_json(500, {'step': 'commit', 'error': commit_output})

        push = run_git('push', 'origin', 'HEAD')
        if push.returncode != 0:
            return self._send_json(500, {'step': 'push', 'error': push.stderr.strip(), 'out': push.stdout.strip()})

        return self._send_json(200, {
            'ok': True,
            'committed': had_staged,
            'commit_message': message if had_staged else None,
            'commit_output': commit_output,
            'push_output': (push.stdout + push.stderr).strip(),
        })

    # ===== /api/backup (POST) =====
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
            return self._send_json(500, {'error': f'script not found: {script}'})

        cmd = [sys.executable, script, '--table', table, '--retention', str(retention), '--quiet']
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=PROJECT_DIR, timeout=120)
        out = proc.stdout.strip()
        err = proc.stderr.strip()
        if proc.returncode != 0:
            return self._send_json(500, {'step': 'run', 'returncode': proc.returncode, 'stdout': out, 'stderr': err})
        try:
            result = json.loads(out)
        except json.JSONDecodeError:
            return self._send_json(500, {'step': 'parse', 'stdout': out, 'stderr': err})
        return self._send_json(200, result)

    # ===== /api/backups (GET) =====
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
        # Total stats
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
