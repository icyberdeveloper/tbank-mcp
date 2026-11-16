"""Exercise the VPS wizard with a fake Docker CLI; no network or bank access."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory() as tmp:
    base = Path(tmp)
    shutil.copy(ROOT / 'tbank', base / 'tbank')
    shutil.copy(ROOT / 'compose.yaml', base / 'compose.yaml')
    docker = base / 'docker'
    docker.write_text('''#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_DOCKER_LOG"
case "$*" in
  *'run --rm --no-deps --entrypoint'*) exit "${FAKE_LOGIN_EXIT:-0}" ;;
esac
''')
    docker.chmod(0o755)
    log = base / 'docker.log'
    env = dict(os.environ, PATH=str(base) + os.pathsep + os.environ['PATH'],
               FAKE_DOCKER_LOG=str(log))
    def run(args, data='', **overrides):
        return subprocess.run(['bash', str(base / 'tbank'), *args], input=data,
                              text=True, capture_output=True,
                              env=dict(env, **overrides), timeout=10)
    secret = 'sk-synthetic-runtime-key'
    result = run(['setup'], f'tunnel_synthetic\n{secret}\n+79991234567\n')
    assert result.returncode == 0, result.stderr
    assert secret not in result.stdout + result.stderr + log.read_text()
    config = base / '.env'
    assert config.stat().st_mode & 0o777 == 0o600
    original = config.read_text()
    assert f'CONTROL_PLANE_API_KEY={secret}' in original
    calls = log.read_text()
    assert calls.index('pull') < calls.index('stop bank') < calls.index('run --rm') < calls.index('up -d')
    result = run(['setup'], '+79991234567\n')
    assert result.returncode == 0 and config.read_text() == original
    log.write_text('')
    result = run(['login', '+79991234567'], FAKE_LOGIN_EXIT='1')
    assert result.returncode != 0 and 'up -d' not in log.read_text()
    log.write_text('')
    result = run(['login', 'invalid-phone'])
    assert result.returncode != 0 and 'stop bank' not in log.read_text()
print('VPS wizard: hidden credentials, file permissions, reuse, login failure and ordering OK')
