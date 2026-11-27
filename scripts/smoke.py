import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
import socket

with tempfile.TemporaryDirectory() as directory:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env = dict(os.environ, TASK_DB=os.path.join(directory, 'smoke.sqlite3'))
    base = f'http://127.0.0.1:{port}'
    process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app:app', '--host', '127.0.0.1', '--port', str(port)], env=env)
    try:
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError('Server exited during startup')
            try:
                with urllib.request.urlopen(base+'/ready', timeout=1) as response:
                    assert json.load(response) == {'status':'ready'}
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError('Server did not become ready')
        request = urllib.request.Request(base+'/tasks', data=b'{"title":"smoke"}', headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 201
            task = json.load(response)
        with urllib.request.urlopen(base+'/tasks', timeout=5) as response:
            assert json.load(response) == [task]
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
