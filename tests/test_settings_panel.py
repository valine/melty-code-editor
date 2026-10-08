"""Root settings and SSH discovery, using only isolated settings/loopback sockets."""
import json
import socket
import threading
import time
from types import SimpleNamespace

import pytest

import settings_panel as panel
from meltygui.model.ssh_file_model import SSH


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
    assert panel.local_networks() == {'lan · 10.3.4.0/24': '10.3.4.0/24'}
    with pytest.raises(ValueError, match='256'):
        panel.start_scan('10.0.0.0/8')


def test_ios_networks_without_psutil(monkeypatch):
    monkeypatch.setattr(panel, 'sys', SimpleNamespace(platform='ios'))
    monkeypatch.setitem(__import__('sys').modules, 'psutil', None)
    monkeypatch.setattr(panel, 'ios_interfaces', lambda: [
        ('en0', '192.168.3.4', '255.255.0.0'),
        ('en1', '10.0.0.2', '255.255.255.252'),
        ('lo0', '127.0.0.1', '255.0.0.0')])
    assert panel.local_networks() == {'en0 · 192.168.3.0/24': '192.168.3.0/24',
                                      'en1 · 10.0.0.0/30': '10.0.0.0/30'}


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
        def address(ip, family=socket.AF_INET):
            value = address_type(length=16, family=family)
            value.address[:] = socket.inet_aton(ip)
            allocated.append(value)
            return c.pointer(value)
        nodes = [entry_type(name=b'en0', flags=0x43, address=address('192.168.4.9'),
                            netmask=address('255.255.255.0')),
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
    monkeypatch.setattr(panel, 'ssh_available', lambda host: host.endswith('.1'))
    job = panel.start_scan('192.168.1.0/30', wake=lambda: None)
    wait_done(job)
    assert job['hosts'] == ('192.168.1.1',)
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
    def fail(host):
        raise RuntimeError('probe failed')
    monkeypatch.setattr(panel, 'ssh_available', fail)
    job = panel.start_scan('192.168.1.0/30', wake=lambda: None)
    wait_done(job)
    assert job['error'] == 'probe failed'
