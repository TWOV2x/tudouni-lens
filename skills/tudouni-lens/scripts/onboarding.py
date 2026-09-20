"""One local password form. The assistant operates the CLI; the customer only enters a Key."""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SESSION_SECONDS = 900


def session_path(lens) -> Path:
    return lens.project_dir() / '.tudouni' / 'setup-session.json'


def read_state(path: Path) -> dict:
    try:
        value = json.loads(path.read_text('utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def public_state(state: dict) -> dict:
    status = state.get('status', 'not_configured')
    if status not in ('saved', 'configured') and state.get('expires_at', 0) <= time.time():
        status = 'expired'
    result = {'ok': status not in ('error', 'expired'), 'status': status,
              'ready': status in ('saved', 'configured'), 'needs_key': status == 'awaiting_key'}
    if status == 'awaiting_key':
        result['setup_url'] = state.get('url', '')
        result['message'] = '请在打开的配置窗口粘贴 Key，点击“保存并开始使用”；完成后助手会继续。'
    elif status == 'saved':
        result['message'] = 'Key 已验证并保存，可以继续原来的创作。'
    elif status == 'configured':
        result['message'] = '已有本机凭据，不需要再次输入。'
    elif status == 'expired':
        result['message'] = '配置窗口已过期，请由助手重新打开配置窗口。'
    elif status == 'error':
        if state.get('error_code') == 'package_integrity':
            result['error_code'] = 'package_integrity'
            result['message'] = '安装包不完整或版本不匹配，请由助手重新安装官方完整包；不需要更换 Key。'
        else:
            result['message'] = '配置窗口未能启动，请由助手检查本机运行环境；不需要客户操作终端。'
    return result


def status(lens) -> dict:
    state = read_state(session_path(lens))
    if state.get('status') in ('starting', 'awaiting_key') and state.get('destination'):
        target = Path(state['destination'])
        # The Key write is authoritative. If a later receipt write fails, never
        # strand the customer after successful input. File identity is not a Key.
        if file_stamp(target) != state.get('initial_stamp'):
            try:
                value = lens.read_keybox_file(target)
                if value and lens.validate_key(value):
                    return public_state({'status': 'configured'})
            except SystemExit:
                pass
    if state.get('status') == 'awaiting_key' and not live_session(state):
        return public_state({'status': 'expired'})
    return public_state(state)


def file_stamp(path: Path):
    try:
        stat = path.stat()
        return [stat.st_ino, stat.st_mtime_ns, stat.st_size]
    except OSError:
        return None


def verify_key(lens, key: str, base: str) -> tuple[bool, str]:
    """The only external onboarding call is this read-only first-party catalog lookup."""
    response = lens.http_json('GET', base.rstrip('/') + '/v1/models', key, timeout=12, fail=False)
    code = response.get('_http_error')
    if code == 401:
        return False, '这把 Key 未通过验证，请检查是否粘贴完整，或使用本站有效的 Key。'
    if code == 403:
        return False, '这把 Key 当前无权读取可用模型，请检查网站上的权限；不需要移动或重新填写 Key。'
    if code == 429:
        return False, '连接暂时繁忙，请稍后在这个窗口重试，不需要更换保存位置。'
    if code is not None or not isinstance(response.get('data'), list):
        return False, '暂时无法连接网站，请在这个窗口重试。原来的 Key 没有被替换。'
    return True, '连接成功，Key 已保存。返回助手后会继续原来的创作。'


class SetupServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, lens, state_file: Path, state: dict, validator=None):
        lens._brand_lock()
        self.lens, self.state_file, self.state = lens, state_file, state
        self.validator = validator or (lambda key: verify_key(lens, key, state['base']))
        self.gate = threading.Lock()
        self.nonce = secrets.token_urlsafe(24)
        state.setdefault('initial_stamp', file_stamp(Path(state['destination'])))
        super().__init__(('127.0.0.1', 0), SetupHandler)
        self.origin = 'http://127.0.0.1:' + str(self.server_port)
        self.state.update(status='awaiting_key', port=self.server_port,
                          url=self.origin + '/#' + state['capability'], pid=os.getpid())
        self.persist()

    def persist(self):
        self.lens.write_keybox_file(self.state_file, json.dumps(self.state, ensure_ascii=False))


class SetupHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # Never log a request body, Key, or local session capability.

    def send(self, code: int, payload, content_type='application/json; charset=utf-8'):
        body = payload if isinstance(payload, bytes) else payload.encode('utf-8') if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'none'; img-src 'self'; script-src 'nonce-%s'; style-src 'nonce-%s'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'" % (self.server.nonce, self.server.nonce))
        self.end_headers()
        self.wfile.write(body)

    def allowed(self, mutate=False) -> bool:
        if self.headers.get('Host') != '127.0.0.1:' + str(self.server.server_port):
            self.send(403, {'ok': False, 'message': '请求来源不正确。'})
            return False
        if time.time() > self.server.state['expires_at']:
            self.send(410, {'ok': False, 'message': '窗口已过期，请让助手重新打开。'})
            return False
        if mutate and self.headers.get('Origin') != self.server.origin:
            self.send(403, {'ok': False, 'message': '请在本机配置窗口操作。'})
            return False
        return True

    def authenticated(self) -> bool:
        actual = self.headers.get('X-Setup-Token', '')
        if not secrets.compare_digest(actual, self.server.state['capability']):
            self.send(403, {'ok': False, 'message': '窗口连接已失效，请让助手重新打开。'})
            return False
        return True

    def do_GET(self):
        if not self.allowed():
            return
        if self.path == '/':
            html = (Path(__file__).resolve().parent.parent / 'assets' / 'setup.html').read_text('utf-8')
            self.send(200, html.replace('__NONCE__', self.server.nonce), 'text/html; charset=utf-8')
        elif self.path == '/logo.png':
            logo = Path(__file__).resolve().parent.parent / 'assets' / 'tudouni-logo.png'
            self.send(200, logo.read_bytes(), 'image/png')
        elif self.path == '/status' and self.authenticated():
            # Do not return paths, capabilities, saved values or Key fragments to the form.
            self.send(200, {'ok': True, 'status': self.server.state['status']})
        elif self.path != '/status':
            self.send(404, {'ok': False})

    def do_POST(self):
        if not self.allowed(mutate=True) or not self.authenticated():
            return
        if self.path == '/cancel':
            if not self.server.gate.acquire(blocking=False):
                self.send(409, {'ok': False, 'message': '正在验证，请稍候。'})
                return
            try:
                self.server.state['status'] = 'cancelled'
                try:
                    self.server.persist()
                except OSError:
                    pass  # The old handler rejects all writes after cancellation.
                self.send(200, {'ok': True})
            finally:
                self.server.gate.release()
            return
        if self.path != '/save':
            self.send(404, {'ok': False})
            return
        if self.headers.get_content_type() != 'application/json':
            self.send(415, {'ok': False, 'message': '请求格式不正确。'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length <= 0 or length > 16384:
                raise ValueError()
            data = json.loads(self.rfile.read(length))
            key = data.get('key') if isinstance(data, dict) else None
            if not isinstance(key, str):
                raise ValueError()
            # This is the same validation as the API client, with errors kept in the form.
            key = self.server.lens.validate_key(key)
        except (ValueError, TypeError, UnicodeError, SystemExit):
            self.send(400, {'ok': False, 'message': 'Key 格式不正确，请粘贴完整的 Key。'})
            return
        if not self.server.gate.acquire(blocking=False):
            self.send(409, {'ok': False, 'message': '正在验证，请稍候。'})
            return
        try:
            if self.server.state['status'] != 'awaiting_key':
                self.send(409, {'ok': False, 'message': '这个窗口已完成或已被替换，请返回助手继续。'})
                return
            ok, message = self.server.validator(key)
            if not ok:
                self.send(400, {'ok': False, 'message': message})
                return
            destination = Path(self.server.state['destination'])
            self.server.lens.write_keybox_file(destination, key)
            self.server.state['status'] = 'saved'
            try:
                self.server.persist()
            except OSError:
                pass  # status() detects the successful atomic Key replacement.
            self.send(200, {'ok': True, 'status': 'saved', 'message': message})
        except SystemExit:
            self.server.state.update(status='error', error_code='package_integrity')
            try:
                self.server.persist()
            except OSError:
                pass
            self.send(409, {'ok': False, 'message': public_state(self.server.state)['message']})
        except Exception:
            self.send(500, {'ok': False, 'message': '本机暂时无法保存，请让助手检查写入权限；不要移动或重复创建 Key 文件。'})
        finally:
            self.server.gate.release()


def destination(lens, ns) -> Path:
    explicit = getattr(ns, 'key_file', None)
    if explicit:
        return Path(explicit).expanduser().resolve()
    for path in lens.keybox_paths():
        try:
            populated = bool(lens.read_keybox_file(path))
        except SystemExit:
            # A broken higher-priority source blocks resolution; repair that same source.
            return path.resolve()
        if populated:
            return path.resolve()
    return lens.local_key_file().resolve()


def prepare(lens, target: Path):
    """Detect inability to save before asking the customer to enter any credential."""
    target.parent.mkdir(parents=True, exist_ok=True)
    probe = target.parent / ('.setup-write-' + secrets.token_hex(8))
    fd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    probe.unlink()
    if target.parent.name == '.tudouni' and not (target.parent / '.gitignore').exists():
        lens.write_keybox_file(target.parent / '.gitignore', '*')


def live_session(state: dict) -> bool:
    if state.get('status') != 'awaiting_key' or state.get('expires_at', 0) <= time.time():
        return False
    port = state.get('port')
    if not isinstance(port, int) or not 1024 <= port <= 65535:
        return False
    try:
        request = urllib.request.Request('http://127.0.0.1:%d/status' % port,
                                         headers={'X-Setup-Token': state.get('capability', '')})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=1) as response:
            return json.load(response).get('status') == 'awaiting_key'
    except (OSError, ValueError):
        return False


def start_setup(lens, ns) -> dict:
    state_file = session_path(lens)
    if getattr(ns, 'status', False):
        return wait_status(lens, getattr(ns, 'wait', 0))
    if not getattr(ns, 'replace', False):
        try:
            if lens.resolve_key(ns):
                return public_state({'status': 'configured'})
        except SystemExit:
            pass
    target = destination(lens, ns)
    base = lens.resolve_base(ns)
    state = read_state(state_file)
    if live_session(state):
        if state.get('destination') == str(target) and state.get('base') == base:
            if not getattr(ns, 'no_open', False):
                webbrowser.open(state['url'])
            return public_state(state)
        try:
            origin = 'http://127.0.0.1:%d' % state['port']
            request = urllib.request.Request(origin + '/cancel', data=b'{}', headers={
                'Origin': origin, 'Content-Type': 'application/json', 'X-Setup-Token': state['capability']})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=2) as response:
                if not json.load(response).get('ok'):
                    raise ValueError()
        except (OSError, ValueError):
            return {'ok': False, 'status': 'busy', 'message': '已有窗口正在验证，请由助手等待它完成，不能把 Key 保存到另一个位置。'}
    prepare(lens, target)
    prepare(lens, state_file)
    state = {'status': 'starting', 'project': str(lens.project_dir()), 'destination': str(target),
             'base': base, 'capability': secrets.token_urlsafe(32),
             'expires_at': time.time() + SESSION_SECONDS, 'initial_stamp': file_stamp(target),
             'open_browser': not getattr(ns, 'no_open', False)}
    lens.write_keybox_file(state_file, json.dumps(state, ensure_ascii=False))
    kwargs = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL, 'close_fds': True}
    if os.name == 'nt':
        kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs['start_new_session'] = True
    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--serve', str(state_file)], **kwargs)
    for _ in range(50):
        state = read_state(state_file)
        if state.get('status') == 'awaiting_key':
            return public_state(state)
        if process.poll() is not None:
            break
        time.sleep(0.1)
    if state.get('status') == 'error':
        return public_state(state)
    return {'ok': False, 'status': 'error', 'message': '配置窗口未能启动，请由助手检查本机运行环境；不需要客户打开终端。'}


def wait_status(lens, seconds: int) -> dict:
    until = time.monotonic() + min(max(seconds, 0), 60)
    while True:
        result = status(lens)
        if result['status'] != 'awaiting_key' or time.monotonic() >= until:
            return result
        time.sleep(0.5)


def serve(state_file: Path):
    import tudouni as lens
    state = read_state(state_file)
    if not state or state.get('expires_at', 0) <= time.time():
        return
    try:
        lens._brand_lock()
    except SystemExit:
        state['status'] = 'error'
        state['error_code'] = 'package_integrity'
        lens.write_keybox_file(state_file, json.dumps(state, ensure_ascii=False))
        return
    lens.configure_workspace(state['project'])
    lens.assert_tudouni_origin(state['base'])
    with SetupServer(lens, state_file, state) as server:
        server.timeout = 0.5
        if state.get('open_browser', True):
            webbrowser.open(server.state['url'])
        saved_at = None
        while time.time() < state['expires_at']:
            server.handle_request()
            if server.state['status'] == 'cancelled':
                return
            if server.state['status'] == 'saved':
                saved_at = saved_at or time.monotonic()
                if time.monotonic() - saved_at > 3:
                    return


if __name__ == '__main__' and len(sys.argv) == 3 and sys.argv[1] == '--serve':
    serve(Path(sys.argv[2]).resolve())
