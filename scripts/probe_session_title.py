"""Reproduce unsafe titles and verify the fix in an isolated OpenCode 2.2.5 TUI.

Requires OpenCode and Bun on PATH and a POSIX pseudo-terminal. All host state
lives in a temporary directory; no model requests or user sessions are used.
"""
from pathlib import Path
import base64
import fcntl
import json
import os
import pty
import select
import shutil
import signal
import socket
import struct
import subprocess
import tempfile
import termios
import time
import urllib.request


def main():
    executable = shutil.which('opencode')
    if not executable:
        raise RuntimeError('opencode must be available on PATH')
    root = Path(tempfile.mkdtemp(prefix='aro-native-title-'))
    env = os.environ.copy()
    for name in ('DATA', 'CONFIG', 'STATE', 'CACHE'):
        directory = root / name.lower()
        directory.mkdir()
        env[f'XDG_{name}_HOME'] = str(directory)
    workspace = root / 'project'
    workspace.mkdir()
    config = root / 'config' / 'opencode'
    config.mkdir()
    (config / 'opencode.jsonc').write_text('{"plugins":[]}')
    password = 'isolated-title-probe'
    env.update(OPENCODE_SERVER_PASSWORD=password, OPENCODE_DISABLE_MODELS_FETCH='1',
               TERM='xterm-256color')
    with socket.socket() as reserved:
        reserved.bind(('127.0.0.1', 0))
        port = reserved.getsockname()[1]
    url = f'http://127.0.0.1:{port}'
    auth = base64.b64encode(f'opencode:{password}'.encode()).decode()

    def request(path, data=None):
        req = urllib.request.Request(url + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers={'Authorization': 'Basic ' + auth, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=2) as response:
            return json.load(response)

    # Exercise the actual helper; the unsafe case models the original input.
    plugin = Path(__file__).resolve().parents[1] / 'plugin'
    fixed_title = subprocess.check_output(['bun', '-e',
        'import { sessionTitle } from "./src/session-title"; '
        'process.stdout.write(sessionTitle("FA\\nFFN"))'], cwd=plugin, env=env,
        text=True, timeout=10)
    results = []
    with (root / 'server.log').open('wb') as log:
        server = subprocess.Popen([executable, 'serve', '--hostname', '127.0.0.1',
            '--port', str(port)], cwd=workspace, env=env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 15
            while True:
                try:
                    request('/api/info')
                    break
                except (OSError, urllib.error.URLError):
                    if time.monotonic() > deadline or server.poll() is not None:
                        raise RuntimeError('The isolated host did not start')
                    time.sleep(0.1)
            for label, title in [('bad', 'FA\nFFN'), ('fixed', fixed_title)]:
                session_id = 'ses_title_probe_' + label
                request('/api/session', {'id': session_id, 'title': title,
                    'location': {'directory': str(workspace)}})
                master, slave = pty.openpty()
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 35, 120, 0, 0))
                process = subprocess.Popen([executable, '--server', url, '--session',
                    session_id, str(workspace)], env=env, cwd=workspace, stdin=slave,
                    stdout=slave, stderr=slave, start_new_session=True)
                os.close(slave)
                chunks = []
                try:
                    deadline = time.monotonic() + 7
                    while time.monotonic() < deadline:
                        if select.select([master], [], [], 0.2)[0]:
                            try:
                                chunk = os.read(master, 262144)
                            except OSError:
                                break
                            if not chunk:
                                break
                            chunks.append(chunk)
                        if process.poll() is not None:
                            break
                    data = b''.join(chunks)
                    (root / f'{label}.terminal').write_bytes(data)
                    results.append({'case': label,
                        'crashed': b'ot_session_control failed' in data,
                        'crash_screen': b'OpenCode crashed' in data,
                        'title_visible': b'FA' in data, 'bytes': len(data),
                        'process_alive': process.poll() is None})
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                    os.close(master)
        finally:
            server.terminate()
            try:
                server.wait(timeout=3)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
    result = {'evidence': str(root), 'results': results}
    (root / 'result.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result))
    assert results[0]['crashed'], results
    assert (not results[1]['crashed'] and not results[1]['crash_screen']
            and results[1]['title_visible'] and results[1]['process_alive']), results


if __name__ == '__main__':
    main()
