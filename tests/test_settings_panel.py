"""Root settings and SSH discovery, using only isolated settings/loopback sockets."""
import json
import socket
import struct
import threading
import time
from types import SimpleNamespace

import pytest

import settings_panel as panel
from meltygui.model.ssh_file_model import SSH


def test_authentication_prompt_keeps_credentials_out_of_settings(monkeypatch):
    import sys
    state = panel.SettingsState()
    root = SSH('server.local')
    prompt = object()
    state.auth = dict(name='Server', root=root, prompt=prompt, job=None)
    jobs = []
    monkeypatch.setitem(sys.modules, '_melty_ios', SimpleNamespace(
        ssh_configuration_status=lambda value: dict(status='saved', username='alice', error='')))
    monkeypatch.setattr(panel, 'check_ssh', lambda value: jobs.append(value) or dict(done=False))
    panel.poll_ssh_auth(state)
    assert jobs == [root]
    assert state.auth['prompt'] is None
    assert 'auth' in state.__no_save__
    assert root.target == 'server.local'  # Existing open-file identities remain valid.
    panel.poll_ssh_auth(state)
    assert jobs == [root]


def test_authentication_requires_explicit_host_trust(monkeypatch):
    from contextlib import contextmanager
    from meltygui.model import ssh_auth_model as auth, ssh_file_model as ssh
    import paramiko
    key = paramiko.RSAKey.generate(2048)
    trusted, refreshed = [], []
    root = SSH('alice@server.local')

    @contextmanager
    def sftp(location):
        if not trusted:
            raise auth.UnknownHostKey('server.local', key)
        yield SimpleNamespace(stat=lambda path: None)

    monkeypatch.setattr(ssh, 'sftp', sftp)
    monkeypatch.setattr(ssh, 'native_path', lambda client, location: '/home/alice')
    monkeypatch.setattr(ssh, 'request', lambda *a, **kw: refreshed.append((a, kw)))
    monkeypatch.setattr(auth, 'trust_host', lambda location, value: trusted.append((location, value)))
    monkeypatch.setattr(panel, 'request_render', lambda: None)

    def finish(job):
        deadline = time.monotonic() + 3
        while not job['done'] and time.monotonic() < deadline:
            time.sleep(.01)
        assert job['done']
        return job

    job = finish(panel.check_ssh(root))
    assert job['key'] is key and job['fingerprint'].startswith('SHA256:')
    assert trusted == refreshed == []
    assert finish(panel.check_ssh(root, job['key']))['error'] == ''
    assert trusted == [(root.location, key)]
    assert refreshed == [((root.location, 'rows'), {'refresh': True})]


def test_roots_share_codedict_and_persist(tmp_path, monkeypatch):
    from editor_settings import CodeEditorSettings, settings
    from meltygui.core.runtime import launch_override
    from meltygui.model.code_dict_model import CodeDict

    monkeypatch.setattr(CodeEditorSettings.Editor, 'project_roots', {'Local': str(tmp_path)})
    path = tmp_path / 'launch_overrides.json'
    monkeypatch.setattr(launch_override, '_state',
                        dict(path=path, overrides={}, dirty=False, hooked=True))
    settings['Editor']['project_roots'] = {'Local': str(tmp_path)}
    state = panel.SettingsState()
    state.name, state.host, state.port = 'Server', 'me@host', '2222'
    panel.add_root(settings, state)
    other = CodeDict(CodeEditorSettings)
    expected = SSH('me@host', '~', 2222)
    assert other['Editor']['project_roots']['Server'] == expected
    assert other['Editor']['project_roots']['Local'] == str(tmp_path)
    with pytest.raises(ValueError, match='already in use'):
        panel.add_root(settings, state)
    launch_override.flush()
    assert 'SSH(' in json.dumps(json.loads(path.read_text()))
    monkeypatch.setattr(CodeEditorSettings.Editor, 'project_roots', {})
    import editor_settings
    launch_override.apply_to_module(editor_settings)
    assert CodeEditorSettings.Editor.project_roots['Server'] == expected
    panel.remove_root(settings, 'Server')
    assert dict(other['Editor']['project_roots']) == {'Local': str(tmp_path)}


@pytest.mark.parametrize('attribute,value', [('name', ''), ('host', '-bad'),
                                             ('port', 'abc'), ('port', '65536'),
                                             ('folder', 'relative')])
def test_invalid_root_does_not_change_settings(attribute, value):
    settings = {'Editor': {'project_roots': {'Keep': '/tmp'}}}
    state = panel.SettingsState()
    state.name, state.host = 'New', 'user@host'
    setattr(state, attribute, value)
    with pytest.raises(ValueError):
        panel.add_root(settings, state)
    assert settings['Editor']['project_roots'] == {'Keep': '/tmp'}


def test_local_root(tmp_path):
    state = panel.SettingsState()
    state.kind, state.name, state.folder = 'Local', 'Folder', str(tmp_path)
    settings = {'Editor': {'project_roots': {}}}
    panel.add_root(settings, state)
    assert settings['Editor']['project_roots'] == {'Folder': str(tmp_path)}


def test_networks_are_connected_and_bounded(monkeypatch):
    import psutil
    def address(ip, mask):
        return SimpleNamespace(family=socket.AF_INET, address=ip, netmask=mask)
    monkeypatch.setattr(psutil, 'net_if_stats', lambda: {
        'lan': SimpleNamespace(isup=True), 'lo': SimpleNamespace(isup=True),
        'down': SimpleNamespace(isup=False)})
    monkeypatch.setattr(psutil, 'net_if_addrs', lambda: {
        'lan': [address('10.3.4.5', '255.255.0.0')],
        'lo': [address('127.0.0.1', '255.0.0.0')],
        'down': [address('192.168.1.1', '255.255.255.0')]})
    assert panel.local_networks() == {'lan  10.3.4.0/24': '10.3.4.0/24'}
    with pytest.raises(ValueError, match='256'):
        panel.start_scan('10.0.0.0/8')


def test_ios_networks_without_psutil(monkeypatch):
    monkeypatch.setattr(panel, 'sys', SimpleNamespace(platform='ios'))
    monkeypatch.setitem(__import__('sys').modules, 'psutil', None)
    monkeypatch.setattr(panel, 'ios_interfaces', lambda: [
        ('en0', '192.168.3.4', '255.255.0.0'),
        ('en1', '10.0.0.2', '255.255.255.252'),
        ('lo0', '127.0.0.1', '255.0.0.0')])
    assert panel.local_networks() == {'en0  192.168.3.0/24': '192.168.3.0/24',
                                      'en1  10.0.0.0/30': '10.0.0.0/30'}


@pytest.mark.parametrize('fails', [False, True])
def test_ios_getifaddrs_abi_and_cleanup(monkeypatch, fails):
    import ctypes as c
    allocated, freed = [], []

    def getifaddrs(out):
        if fails:
            c.set_errno(13)
            return -1
        entry_type = getifaddrs.argtypes[0]._type_._type_
        address_type = dict(entry_type._fields_)['address']._type_
        def address(ip, family=socket.AF_INET, length=16):
            value = address_type(length=length, family=family)
            value.address[:] = socket.inet_aton(ip)
            allocated.append(value)
            return c.pointer(value)
        nodes = [entry_type(name=b'en0', flags=0x43, address=address('192.168.4.9'),
                            netmask=address('255.255.255.0', length=7)),
                 entry_type(name=b'pdp_ip0', flags=0x51, address=address('10.0.0.1'),
                            netmask=address('255.0.0.0')),
                 entry_type(name=b'en1', flags=0x42, address=address('10.0.0.2'),
                            netmask=address('255.0.0.0')),
                 entry_type(name=b'en0', flags=0x43, address=address('0.0.0.0', 30)),
                 entry_type(name=b'en0', flags=0x43)]
        for first, second in zip(nodes, nodes[1:]):
            first.next = c.pointer(second)
        allocated.extend(nodes)
        c.cast(out, getifaddrs.argtypes[0])[0] = c.pointer(nodes[0])
        return 0

    def freeifaddrs(head):
        freed.append(c.addressof(head.contents))

    def library(name, *, use_errno):
        assert name is None and use_errno
        return SimpleNamespace(getifaddrs=getifaddrs, freeifaddrs=freeifaddrs)

    monkeypatch.setattr(c, 'CDLL', library)
    if fails:
        with pytest.raises(OSError) as error:
            panel.ios_interfaces()
        assert error.value.errno == 13
        assert freed == []
    else:
        assert panel.ios_interfaces() == [('en0', '192.168.4.9', '255.255.255.0')]
        assert len(freed) == 1


@pytest.mark.parametrize('banner,expected', [(b'SSH-2.0-test\r\n', True),
                                            (b'Notice\r\nSSH-1.99-test\r\n', True),
                                            (b'HTTP/1.1 200 OK\r\n', False),
                                            (b'', False)])
def test_discovery_reads_only_identification(banner, expected):
    received = []
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        def serve():
            with listener.accept()[0] as connection:
                connection.sendall(banner)
                connection.shutdown(socket.SHUT_WR)
                received.append(connection.recv(16))
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        assert panel.ssh_available('127.0.0.1', listener.getsockname()[1]) is expected
        thread.join(2)
        assert received == [b'']


def wait_done(job):
    deadline = time.monotonic() + 3
    while not job['done'] and time.monotonic() < deadline:
        time.sleep(.01)
    assert job['done']


def test_scan_results_and_cancel_cleanup(monkeypatch):
    monkeypatch.setattr(panel, 'local_networks', lambda: {'small': '192.168.1.0/30', 'large': '192.168.1.0/24'})
    monkeypatch.setattr(panel, 'ssh_available', lambda host: host.endswith('.1'))
    monkeypatch.setattr(panel, 'network_name', lambda host: 'studio.local')
    job = panel.start_scan('192.168.1.0/30', wake=lambda: None)
    wait_done(job)
    assert job['hosts'] == ('192.168.1.1',)
    assert job['names'] == {'192.168.1.1': 'studio.local'}
    assert job['completed'] == job['total'] == 2
    entered, release = threading.Event(), threading.Event()
    def probe(host):
        entered.set()
        release.wait(2)
        return False
    monkeypatch.setattr(panel, 'ssh_available', probe)
    state = panel.SettingsState()
    state.scan = panel.start_scan('192.168.1.0/24', wake=lambda: None)
    assert entered.wait(2)
    panel.cleanup_settings(SimpleNamespace(misc={'state': state}))
    release.set()
    wait_done(state.scan)
    assert state.scan['cancel'].is_set()
    assert state.scan['completed'] <= 16


def test_scan_failure_finishes(monkeypatch):
    monkeypatch.setattr(panel, 'local_networks', lambda: {'test': '192.168.1.0/30'})
    def fail(host):
        raise RuntimeError('probe failed')
    monkeypatch.setattr(panel, 'ssh_available', fail)
    job = panel.start_scan('192.168.1.0/30', wake=lambda: None)
    wait_done(job)
    assert job['error'] == 'probe failed'


def test_scan_requests_permission_then_refreshes_empty_network(monkeypatch):
    released, seen = [], []
    statuses = iter(['pending', 'denied', 'granted'])
    class Request:
        def __del__(self):
            released.append(True)
    native = SimpleNamespace(request_local_network_access=Request,
                             local_network_access_status=lambda request: next(statuses))
    monkeypatch.setitem(__import__('sys').modules, '_melty_ios', native)
    monkeypatch.setattr(panel, 'sys', SimpleNamespace(platform='ios'))
    def networks():
        assert released, 'Discovery must wait for permission, and release its probe.'
        return {'new Wi-Fi': '192.168.2.0/30'}
    monkeypatch.setattr(panel, 'local_networks', networks)
    monkeypatch.setattr(panel, 'ssh_available', lambda host: seen.append(host) or False)
    job = panel.start_scan('', wake=lambda: None)
    wait_done(job)
    assert not job['error']
    assert job['permission'] == 'granted'
    assert job['network'] == '192.168.2.0/30'
    assert sorted(seen) == ['192.168.2.1', '192.168.2.2']


@pytest.mark.parametrize('status', ['denied', 'failed'])
def test_permission_probe_released_on_cancel_or_failure(monkeypatch, status):
    released = []
    class Request:
        def __del__(self):
            released.append(True)
    native = SimpleNamespace(request_local_network_access=Request,
                             local_network_access_status=lambda request: status)
    monkeypatch.setitem(__import__('sys').modules, '_melty_ios', native)
    monkeypatch.setattr(panel, 'sys', SimpleNamespace(platform='ios'))
    job = dict(cancel=threading.Event(), permission='pending')
    if status == 'denied':
        assert not panel.wait_for_network_access(job, wake=job['cancel'].set)
    else:
        with pytest.raises(OSError, match='unavailable'):
            panel.wait_for_network_access(job, wake=lambda: None)
    assert released == [True]


def test_scan_refreshes_disconnected_selection(monkeypatch):
    monkeypatch.setattr(panel, 'local_networks', lambda: {'new': '192.168.2.0/30'})
    monkeypatch.setattr(panel, 'ssh_available', lambda host: False)
    job = panel.start_scan('192.168.1.0/24', wake=lambda: None)
    wait_done(job)
    assert job['network'] == '192.168.2.0/30'
    assert job['total'] == 2


def test_scan_streams_fast_hosts_before_a_silent_host_finishes(monkeypatch):
    release, named = threading.Event(), threading.Event()
    monkeypatch.setattr(panel, 'local_networks', lambda: {'test': '192.168.1.0/30'})
    def probe(host):
        if host.endswith('.1'):
            release.wait(2)
            return False
        return True
    def name(host):
        named.set()
        return 'mac-mini.local'
    monkeypatch.setattr(panel, 'ssh_available', probe)
    monkeypatch.setattr(panel, 'network_name', name)
    job = panel.start_scan('192.168.1.0/30', wake=lambda: None)
    try:
        assert named.wait(1)
        assert job['hosts'] == ('192.168.1.2',)
        assert not job['done']
    finally:
        release.set()
        wait_done(job)


def test_scan_keeps_64_probes_busy_and_cancels_queued_work(monkeypatch):
    release, busy, lock = threading.Event(), threading.Event(), threading.Lock()
    seen = []
    monkeypatch.setattr(panel, 'local_networks', lambda: {'test': '192.168.1.0/24'})
    def probe(host):
        with lock:
            seen.append(host)
            if len(seen) == 64:
                busy.set()
        release.wait(2)
        return False
    monkeypatch.setattr(panel, 'ssh_available', probe)
    job = panel.start_scan('192.168.1.0/24', wake=lambda: None)
    try:
        assert busy.wait(1)
        job['cancel'].set()
    finally:
        release.set()
        wait_done(job)
    assert len(seen) == 64


def test_network_name_from_device_unicast_reply():
    # A real loopback datagram exchange; owner is compressed back to the question.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
        server.bind(('127.0.0.1', 0))
        server.settimeout(2)
        received = []
        def reply():
            query, peer = server.recvfrom(9000)
            received.append(query)
            name = b'\x08mac-mini\x05local\x00'
            packet = (struct.pack('!6H', 0, 0x8400, 1, 1, 0, 0) + query[12:] +
                      b'\xc0\x0c' + struct.pack('!HHIH', 12, 0x8001, 120, len(name)) + name)
            server.sendto(packet, peer)
        thread = threading.Thread(target=reply, daemon=True)
        thread.start()
        assert panel.network_name('127.0.0.1', port=server.getsockname()[1]) == 'mac-mini.local'
        thread.join(2)
        assert received[0].endswith(struct.pack('!HH', 12, 1))


def test_network_name_timeout():
    # An open responder that never answers must not stall a scan.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
        server.bind(('127.0.0.1', 0))
        started = time.monotonic()
        assert panel.network_name('127.0.0.1', port=server.getsockname()[1], timeout=.03) == ''
        assert time.monotonic() - started < .5


@pytest.mark.parametrize('packet', [b'', b'\xc0\x00', b'\xc0', b'\x04ab', b'\x01\n\0', b'\x40' + b'a' * 64])
def test_dns_name_rejects_truncated_cyclic_and_invalid_names(packet):
    with pytest.raises(ValueError):
        panel.dns_name(packet, 0)


def test_ptr_ignores_another_devices_name():
    owner = b'\x01x\0'
    name = b'\x05other\x05local\0'
    packet = struct.pack('!6H', 0, 0x8400, 0, 1, 0, 0) + owner + struct.pack('!HHIH', 12, 1, 120, len(name)) + name
    assert panel.ptr_hostname(packet, '1.0.0.127.in-addr.arpa') == ''
