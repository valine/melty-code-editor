"""Authenticate against an isolated SSH server without touching real credentials."""
import io
import json
import socket
import sys
import threading
from types import SimpleNamespace

import paramiko
import pytest

from meltygui.model import ssh_auth_model as auth


@pytest.fixture
def ssh_login(tmp_path, monkeypatch):
    host_key = paramiko.RSAKey.generate(2048)
    login_key = paramiko.RSAKey.generate(2048)
    attempts = []
    transports = []
    finished = threading.Event()

    class Login(paramiko.ServerInterface):
        def get_allowed_auths(self, username):
            return 'password,publickey'

        def check_auth_password(self, username, password):
            attempts.append(username)
            return paramiko.AUTH_SUCCESSFUL if (username, password) == ('alice', 'test-password') else paramiko.AUTH_FAILED

        def check_auth_publickey(self, username, key):
            attempts.append(username)
            return paramiko.AUTH_SUCCESSFUL if username == 'alice' and key == login_key else paramiko.AUTH_FAILED

    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    listener.settimeout(.1)

    def serve():
        while not finished.is_set():
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            transport = paramiko.Transport(connection)
            transports.append(transport)
            transport.add_server_key(host_key)
            try:
                transport.start_server(server=Login())
            except (EOFError, OSError, paramiko.SSHException):
                transport.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    location = f'sftp://alice@127.0.0.1:{listener.getsockname()[1]}/~'
    secrets = {auth.credential_account(location): json.dumps({'password': 'test-password'})}
    monkeypatch.setitem(sys.modules, '_melty_ios', SimpleNamespace(ssh_credentials=secrets.get))
    monkeypatch.setattr(auth, 'known_hosts_path', lambda: tmp_path / 'known_hosts')
    yield location, secrets, host_key, login_key, attempts
    finished.set()
    listener.close()
    for transport in transports:
        transport.close()
    thread.join(2)


def test_host_confirmation_precedes_password(ssh_login):
    location, _, key, _, attempts = ssh_login
    with pytest.raises(auth.UnknownHostKey) as caught:
        auth.connect(location)
    assert caught.value.key == key
    assert caught.value.fingerprint.startswith('SHA256:')
    assert not attempts
    auth.trust_host(location, caught.value.key)
    client = auth.connect(location)
    try:
        assert client.get_transport().is_authenticated()
        assert attempts == ['alice']
    finally:
        client.close()
    assert auth.known_hosts_path().stat().st_mode & 0o777 == 0o600


def test_wrong_password_and_changed_host_are_refused(ssh_login):
    location, secrets, key, _, attempts = ssh_login
    auth.trust_host(location, key)
    secrets[auth.credential_account(location)] = json.dumps({'password': 'wrong-secret'})
    with pytest.raises(OSError, match='authentication failed') as caught:
        auth.connect(location)
    assert 'wrong-secret' not in str(caught.value)
    other = paramiko.RSAKey.generate(2048)
    with pytest.raises(OSError, match='host key has changed'):
        auth.trust_host(location, other)
    hosts = paramiko.HostKeys()
    hosts.add(auth.host_key_name(location), other.get_name(), other)
    hosts.save(str(auth.known_hosts_path()))
    attempts.clear()
    with pytest.raises(OSError, match='host key changed'):
        auth.connect(location)
    assert not attempts


def test_encrypted_private_key_and_host_only_root(ssh_login):
    location, secrets, host_key, login_key, _ = ssh_login
    location = location.replace('alice@', '')
    text = io.StringIO()
    login_key.write_private_key(text, password='test-passphrase')
    secret = dict(username='alice', private_key=text.getvalue(), passphrase='test-passphrase')
    secrets[auth.credential_account(location)] = json.dumps(secret)
    auth.trust_host(location, host_key)
    client = auth.connect(location)
    try:
        assert client.get_transport().is_authenticated()
    finally:
        client.close()
    secret['passphrase'] = 'wrong-passphrase'
    secrets[auth.credential_account(location)] = json.dumps(secret)
    with pytest.raises(OSError, match='Could not unlock'):
        auth.connect(location)


def test_credentials_do_not_cross_accounts(ssh_login):
    location, *_ = ssh_login
    assert auth.credentials(location)['password'] == 'test-password'
    with pytest.raises(OSError, match='Set up Password or SSH Key'):
        auth.credentials(location.replace('alice@', 'bob@'))
    with pytest.raises(ValueError, match='Invalid SSH location'):
        auth.server('sftp://alice:password@host/~')
