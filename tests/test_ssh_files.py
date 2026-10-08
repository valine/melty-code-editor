"""Location, pending-file and SSH execution integration, without a window."""
import json
import os
from pathlib import Path
import pwd
import shutil
import socket
import subprocess
import sys
import threading
import time

import pytest

from meltygui.model.file_location_model import FileLocation, file_path
from meltygui.model import ssh_file_model as ssh
from meltygui.code.fileref import Address
from meltygui.code.new_codecs import TextFileCodec, SaveConflict
from meltygui.editor.pending_save import PendingSave
from meltygui.core.melty import Melty
from meltygui_pro.models.project_execution import ProjectExecution


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(ssh, '_cache', {})
    monkeypatch.setattr(ssh, '_recovery_dirty', {})
    monkeypatch.setattr(ssh, '_recovery_path', lambda location: tmp_path / ('draft-' + str(abs(hash(str(location))))))
    monkeypatch.setattr(PendingSave, 'pending_saves', {})
    monkeypatch.setattr(PendingSave, 'originals', {})
    yield
    ssh.flush_recovery()


def test_identity_and_settings_source():
    from meltygui.model.code_dict_model import _source_of
    root = ssh.SSH('user@host', '/home/user/a #b', 2222)
    assert eval(_source_of(root), {'SSH': ssh.SSH}) == root
    location = root.location
    assert str(location).endswith('/home/user/a%20%23b')
    assert (location / 'sub' / '..' / 'x.py').name == 'x.py'
    assert (location / 'x.py').is_within(location)
    assert not (location / 'x.py').is_within(FileLocation('sftp://elsewhere/home/user'))
    assert not (location / '../ab').is_within(location)
    assert file_path('/tmp/example') == Path('/tmp/example')
    with pytest.raises(TypeError):
        os.fspath(location)


def test_offline_tabs_and_projects(monkeypatch):
    from meltygui_pro.models.open_files import OpenFiles
    from meltygui_pro.models import projects
    from meltygui.models.file_meta import FileMeta
    root = FileLocation('sftp://offline/project')
    monkeypatch.setattr(projects, 'project_roots', lambda: (root, Path('/tmp/local')))
    files = OpenFiles()
    files.open_paths = [str(root / 'main.py')]
    files.prune_missing()
    assert files.paths_in(root) == files.open_paths
    assert projects.project_for(root / 'main.py') == root
    assert not projects.path_in_folder('/tmp/local/main.py', root)


@pytest.fixture
def server(tmp_path, monkeypatch):
    """Own keys, known-hosts file and loopback listener; never edit ~/.ssh."""
    sshd = shutil.which('sshd')
    keygen = shutil.which('ssh-keygen')
    if not sshd or not keygen:
        pytest.skip('OpenSSH server/client required for integration')
    host_key, key = tmp_path / 'host', tmp_path / 'client'
    for target in (host_key, key):
        subprocess.run([keygen, '-q', '-t', 'ed25519', '-N', '', '-f', str(target)],
                       check=True, close_fds=False)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    known = tmp_path / 'known_hosts'
    kind, public, *_ = host_key.with_suffix('.pub').read_text().split()
    known.write_text(f'[127.0.0.1]:{port} {kind} {public}\n')
    config = tmp_path / 'sshd_config'
    config.write_text(f'''Port {port}
ListenAddress 127.0.0.1
HostKey {host_key}
AuthorizedKeysFile {key}.pub
PidFile {tmp_path}/sshd.pid
StrictModes no
UsePAM no
PasswordAuthentication no
KbdInteractiveAuthentication no
Subsystem sftp internal-sftp
''')
    log = (tmp_path / 'sshd.log').open('w+')
    process = subprocess.Popen([sshd, '-D', '-e', '-f', str(config)], stdout=log, stderr=log, close_fds=False)
    try:
        for _ in range(100):
            if process.poll() is not None:
                log.seek(0)
                pytest.fail(log.read())
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=.1):
                    break
            except OSError:
                time.sleep(.02)
        original = ssh.ssh_arguments
        def arguments(location, **kwargs):
            args = original(location, **kwargs)
            return args[:-1] + ['-i', str(key), '-oIdentitiesOnly=yes', '-oUserKnownHostsFile=' + str(known), args[-1]]
        monkeypatch.setattr(ssh, 'ssh_arguments', arguments)
        yield ssh.SSH(f'{pwd.getpwuid(os.getuid()).pw_name}@127.0.0.1', str(tmp_path), port).location
    finally:
        process.terminate()
        process.wait(timeout=5)
        log.close()


def test_sftp_read_save_conflict_and_recovery(server):
    location = server / 'main.py'
    Path(location.remote_path).write_text('print("before")\n')
    ssh.entry(location).update(ssh._operation(location, 'data'))
    address = Address(location)
    assert TextFileCodec.load(address) == 'print("before")\n'
    PendingSave.queue_save(address, TextFileCodec, data='print("pending")\n')
    ssh.flush_recovery()
    assert ssh.recovered_edit(location)['text'] == 'print("pending")\n'
    assert PendingSave.remote_text(location) == 'print("pending")\n'
    assert TextFileCodec.save(address, 'print("saved")\n') is True
    assert Path(location.remote_path).read_text() == 'print("saved")\n'
    Path(location.remote_path).write_text('external edit with a different size\n')
    assert isinstance(TextFileCodec.save(address, 'oops'), SaveConflict)
    assert Path(location.remote_path).read_text().startswith('external')


def test_ios_direct_files_and_tasks_share_keychain_credentials(server, monkeypatch):
    from types import SimpleNamespace
    from meltygui.model import ssh_auth_model as auth
    from meltygui_pro.models import project_execution
    root = Path(server.remote_path)
    monkeypatch.setattr(auth, 'known_hosts_path', lambda: root / 'known_hosts')
    monkeypatch.setitem(sys.modules, '_melty_ios', SimpleNamespace(
        request_frame=lambda: None,
        ssh_credentials=lambda account: json.dumps({'private_key': (root / 'client').read_text()})))
    ios = SimpleNamespace(**(vars(sys) | {'platform': 'ios'}))
    monkeypatch.setattr(ssh, 'sys', ios)
    monkeypatch.setattr(project_execution, 'sys', ios)
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **kw: pytest.fail('iOS cannot launch OpenSSH'))
    test_sftp_read_save_conflict_and_recovery(server)
    state = ProjectExecution()
    state.execute(str(server), dict(cmd='echo direct-ssh; echo stderr-output >&2', cwd='.', env={}), 'direct')
    wait_run(state)
    assert state.error is None, state.error
    assert state.exit == 0
    assert 'direct-ssh' in state.output and 'stderr-output' in state.output


def test_ios_stop_during_authentication_does_not_launch_task(server, monkeypatch):
    from types import SimpleNamespace
    from meltygui.model import ssh_auth_model as auth
    from meltygui_pro.models import project_execution
    entered, proceed = threading.Event(), threading.Event()
    terminated = []

    def connecting(*args):
        entered.set()
        assert proceed.wait(5)
        return SimpleNamespace(terminate=lambda: terminated.append(True))

    monkeypatch.setattr(project_execution, 'sys', SimpleNamespace(**(vars(sys) | {'platform': 'ios'})))
    monkeypatch.setattr(auth, 'SSHProcess', connecting)
    state = ProjectExecution()
    state.execute(str(server), dict(cmd='echo should-not-run', cwd='.', env={}), 'cancel')
    assert entered.wait(2)
    assert state.running and state.process is None
    state.stop()
    proceed.set()
    wait_run(state)
    assert terminated == [True]
    assert not state.ssh_unconfirmed


def wait_run(state, timeout=15):
    deadline = time.monotonic() + timeout
    while state.running and time.monotonic() < deadline:
        Melty._drain_render_tasks()
        time.sleep(.02)
    Melty._drain_render_tasks()
    assert not state.running, state.output


def test_ssh_task_runs_pending_module_with_package_import(server):
    root = Path(server.remote_path)
    (root / 'pkg').mkdir()
    (root / 'pkg/__init__.py').write_text('')
    (root / 'pkg/value.py').write_text('answer = 42\n')
    (root / 'pkg/main.py').write_text('raise RuntimeError("disk source ran")')
    from meltygui.model.source_snapshot_model import SourceSnapshot
    text = 'from .value import answer\nprint("REMOTE", answer)\n'
    state = ProjectExecution()
    state.execute(str(server), dict(cmd='python -m pkg.main', module='pkg/main.py', cwd='.', env={}),
                  'module', source_snapshot=SourceSnapshot(str(server / 'pkg/main.py'), text))
    wait_run(state)
    assert state.error is None, state.output
    assert state.exit == 0
    assert 'Fatal Python error' not in state.output
    assert 'REMOTE 42' in state.output
    assert (root / 'pkg/main.py').read_text().startswith('raise')


def test_ssh_stop_and_context_switch(server):
    state = ProjectExecution()
    state.execute(str(server), dict(cmd='echo started; sleep 90', cwd='.', env={}), 'sleep')
    deadline = time.monotonic() + 10
    while 'started' not in state.output and time.monotonic() < deadline:
        Melty._drain_render_tasks()
        time.sleep(.02)
    assert 'started' in state.output
    state.selected_targets['/some/other/project'] = 'local'
    state.stop()
    wait_run(state)
    assert not state.ssh_unconfirmed
    assert state.project == str(server)
    assert state.exit is not None


def test_remote_manifest_pending_save_and_create(server):
    import tasks
    tasks.project_tasks = tasks.ProjectTasks()
    manifest = server / 'pyproject.toml'
    # A missing manifest is distinct from a failed/offline read.
    ssh.entry(manifest)['error'] = FileNotFoundError(2, 'absent')
    tasks.save_module_task(server, 'run', {'cmd': 'python main.py', 'module': 'main.py', 'cwd': '.', 'env': {}})
    assert tasks.read_tasks(server)['run']['module'] == 'main.py'
    address, (codec, kwargs) = next(iter(PendingSave.pending_saves.items()))
    PendingSave.apply_all_saves()
    deadline = time.monotonic() + 10
    while ssh.entry(manifest).get('saving') and time.monotonic() < deadline:
        Melty._drain_render_tasks()
        time.sleep(.02)
    assert not ssh.entry(manifest).get('save_error')
    assert 'main.py' in Path(manifest.remote_path).read_text()


def test_save_ack_keeps_newer_edit(server, monkeypatch):
    location = server / 'changing.py'
    Path(location.remote_path).write_text('old\n')
    ssh.entry(location).update(ssh._operation(location, 'data'))
    address = Address(location)
    TextFileCodec.load(address)
    PendingSave.queue_save(address, TextFileCodec, data='first\n')
    written, proceed = threading.Event(), threading.Event()
    original = ssh.write_bytes
    def slow(*args, **kwargs):
        result = original(*args, **kwargs)
        written.set()
        assert proceed.wait(5)
        return result
    monkeypatch.setattr(ssh, 'write_bytes', slow)
    PendingSave.apply_all_saves()
    assert written.wait(5)
    PendingSave.queue_save(address, TextFileCodec, data='newer edit\n')
    proceed.set()
    deadline = time.monotonic() + 5
    while ssh.entry(location).get('saving') and time.monotonic() < deadline:
        Melty._drain_render_tasks()
        time.sleep(.02)
    assert PendingSave.remote_text(location) == 'newer edit\n'
    assert Path(location.remote_path).read_text() == 'first\n'
    PendingSave.apply_all_saves()
    deadline = time.monotonic() + 5
    while ssh.entry(location).get('saving') and time.monotonic() < deadline:
        Melty._drain_render_tasks()
        time.sleep(.02)
    assert PendingSave.entry_for(address) is None
    assert Path(location.remote_path).read_text() == 'newer edit\n'


def test_ssh_disconnect_does_not_relaunch(server):
    state = ProjectExecution()
    state.execute(str(server), dict(cmd='echo started; sleep 90', cwd='.', env={}), 'sleep')
    deadline = time.monotonic() + 10
    while 'started' not in state.output and time.monotonic() < deadline:
        Melty._drain_render_tasks()
        time.sleep(.02)
    state.process.terminate()
    wait_run(state)
    assert state.ssh_unconfirmed
    previous = state.process
    state.execute(str(server), dict(cmd='echo duplicate', cwd='.', env={}), 'duplicate')
    assert state.process is previous
    assert 'unconfirmed' in state.error


def test_shared_file_value_loads_edits_and_refreshes(server):
    from meltygui_pro.editor.git import GitProxy
    path = server / 'shared.py'
    disk = Path(server.remote_path) / 'shared.py'
    disk.write_text('before = 1\n')
    repo = GitProxy(server)
    current = repo.file(path)
    changes = []
    callback = lambda: changes.append(True)
    current.subscribe(callback)
    try:
        deadline = time.monotonic() + 8
        while current.get('value') is None and time.monotonic() < deadline:
            Melty._drain_render_tasks()
            time.sleep(.02)
        assert current.get('value') == 'before = 1\n', current.error
        assert set(current.versions.values()) == {'current', 'filesystem'}
        current['value'] = 'after = 2\n'
        assert PendingSave.pending_text_for(current.address) == 'after = 2\n'
        reopened = Address(path)
        assert TextFileCodec.load(reopened) == 'after = 2\n'
        assert reopened._remote_stamp == current.address._remote_stamp
        assert disk.read_text() == 'before = 1\n'
        PendingSave.apply_all_saves()
        deadline = time.monotonic() + 8
        while PendingSave.entry_for(current.address) and time.monotonic() < deadline:
            Melty._drain_render_tasks()
            time.sleep(.02)
        assert disk.read_text() == 'after = 2\n'
        disk.write_text('external = 333\n')
        ssh._operation(server, 'rows')  # The Files view's normal directory update.
        deadline = time.monotonic() + 8
        while current.get('value') != 'external = 333\n' and time.monotonic() < deadline:
            Melty._drain_render_tasks()
            time.sleep(.02)
        assert current.get('value') == 'external = 333\n', current.error
    finally:
        current.unsubscribe(callback)


def test_codec_refresh_retains_pending_edits_and_save_baseline(monkeypatch):
    path = FileLocation('sftp://example/project/main.py')
    address = Address(path)
    address._remote_stamp, address._remote_encoding = (1, 3, 33188), 'utf-8'
    state = ssh.entry(path)
    state.update(data=b'old', read_id=1, stale=True)
    requests = []
    monkeypatch.setattr(ssh, 'request', lambda *args, **kwargs: requests.append(args))
    PendingSave.mark_load(address, 'old')
    PendingSave.queue_save(address, TextFileCodec, data='my draft')
    TextFileCodec.file_status(address)
    assert requests[-1] == (path, 'data')
    state.update(data=b'other', read_id=2, loading=False, stale=False)
    reopened = Address(path)
    assert TextFileCodec.load(reopened) == 'my draft'
    assert reopened._remote_stamp == (1, 3, 33188)
    assert PendingSave.originals[address] == 'old'
    assert ssh.recovered_edit(path)['text'] == 'my draft'


def test_directory_refresh_does_not_rebase_loaded_contents(server):
    disk = Path(server.remote_path) / 'version.py'
    disk.write_text('old = 1\n')
    path = server / 'version.py'
    deadline = time.monotonic() + 5
    while 'data' not in ssh.entry(path) and time.monotonic() < deadline:
        ssh.request(path, 'data')
        time.sleep(.02)
    first = Address(path)
    assert TextFileCodec.load(first) == 'old = 1\n'
    disk.write_text('externally_changed = 222\n')
    result = ssh._operation(server, 'rows')
    assert result['rows']
    second = Address(path)
    assert TextFileCodec.load(second) == 'old = 1\n'
    assert second._remote_stamp == first._remote_stamp
    result = TextFileCodec.save(second, 'my edit\n')
    assert isinstance(result, SaveConflict)
    assert disk.read_text() == 'externally_changed = 222\n'


def test_manifest_task_cannot_run_stale_definition(server, monkeypatch):
    from tasks import TaskState, read_tasks, project_tasks
    monkeypatch.setattr(project_tasks, 'projects', {})
    monkeypatch.setattr(project_tasks, 'manifest_tasks', {})
    disk = Path(server.remote_path) / 'pyproject.toml'
    disk.write_text('[tool.melty.tasks]\nhello="echo hello"\n')
    deadline = time.monotonic() + 5
    while 'hello' not in read_tasks(server) and time.monotonic() < deadline:
        time.sleep(.02)
    assert 'hello' in read_tasks(server)
    path = server / 'pyproject.toml'
    address = Address(path)
    TextFileCodec.load(address)
    PendingSave.queue_save(address, TextFileCodec, data='[broken')
    state = TaskState()
    state.run(str(server), 'hello')
    assert state.error
    assert not state.running


@pytest.mark.parametrize('remote', [False, True])
def test_pending_view_reads_original_through_codec(tmp_path, monkeypatch, remote):
    from meltygui.view import file_view
    from meltygui.view.file_view import RenderFuncs
    from types import SimpleNamespace
    path = FileLocation('sftp://example/project/base.txt') if remote else tmp_path / 'base.txt'
    if remote:
        ssh.entry(path).update(data=b'saved text', data_stat=SimpleNamespace(
            st_mtime=1, st_size=10, st_mode=33188))
        monkeypatch.setattr(ssh, 'request', lambda *args, **kwargs: None)
    else:
        path.write_text('saved text')
    address = Address(path)
    TextFileCodec.load(address)
    expected = getattr(address, '_remote_stamp', None)
    PendingSave.queue_save(address, TextFileCodec, data='unsaved draft')
    monkeypatch.setattr(RenderFuncs, 'draw_function', lambda *args, **kwargs: None)
    monkeypatch.setattr(RenderFuncs, 'button', lambda *args, **kwargs: (False, None))
    rendered = []
    monkeypatch.setattr(RenderFuncs, 'draw_text', lambda value, **kwargs: rendered.append(value))
    monkeypatch.setattr(PendingSave, 'merge_results', [])
    file_view.draw_pending_saves.__wrapped__()
    assert PendingSave.originals[address] == 'saved text'
    assert PendingSave.pending_text_for(address) == 'unsaved draft'
    assert getattr(address, '_remote_stamp', None) == expected
    file_view.draw_pending_saves.__wrapped__()
    assert any('unsaved draft' in value for value in rendered)


def test_pending_apply_keeps_remote_conflict_visible(server):
    path = server / 'conflict.txt'
    disk = Path(path.remote_path)
    disk.write_text('original')
    ssh.entry(path).update(ssh._operation(path, 'data'))
    address = Address(path)
    PendingSave.mark_load(address, TextFileCodec.load(address))
    PendingSave.queue_save(address, TextFileCodec, data='my draft')
    disk.write_text('external change')
    PendingSave.apply_all_saves()
    deadline = time.monotonic() + 5
    while ssh.entry(path).get('saving') and time.monotonic() < deadline:
        Melty._drain_render_tasks()
        time.sleep(.02)
    assert ssh.entry(path).get('save_error')
    assert PendingSave.pending_text_for(address) == 'my draft'
    assert PendingSave.originals[address] == 'original'
    assert ssh.recovered_edit(path)['text'] == 'my draft'
    assert disk.read_text() == 'external change'
