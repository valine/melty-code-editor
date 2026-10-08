"""The Settings tile: shared settings, named roots, and bounded SSH discovery."""
import ipaddress
import socket
import struct
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from meltygui import imgui, render_func
from meltygui.core.conversion.dict_conversion import DictConversion
from meltygui.core.rendering.core_decoration import no_save
from meltygui.core.rendering.render_dispatch import draw_any
from meltygui.core.runtime.toggles import Tint
from meltygui.core.windowing.glfw_utils import request_render
from meltygui.hdr_color import pack_color
from meltygui.model.code_dict_model import CodeDict
from meltygui.model.ssh_file_model import SSH
from meltygui.view.control_view import draw_str
from meltygui.view.dropdown_view import draw_dropdown
from meltygui.view.header_view import flat_button
from meltygui.view.tab_view import draw_tab_bar


@no_save('networks', 'scan', 'error', 'auth')
class SettingsState(DictConversion):
    def __init__(self):
        super().__init__()
        self.tab = 'Settings'
        self.kind = 'SSH'
        self.name = ''
        self.host = ''
        self.folder = '~'
        self.port = '22'
        self.network = ''
        self.networks = None
        self.scan = None
        self.error = ''
        self.auth = None


def add_root(settings, state):
    """Replace the CodeDict leaf so both Hotswap and LaunchOverride see the edit."""
    name = state.name.strip()
    roots = dict(settings['Editor']['project_roots'])
    if not name:
        raise ValueError('Enter a name.')
    if name in roots:
        raise ValueError('That name is already in use.')
    folder = state.folder.strip() or '~'
    if state.kind == 'SSH':
        try:
            port = int(state.port.strip() or '22')
        except ValueError:
            raise ValueError('Port must be a number from 1 to 65535.') from None
        value = SSH(state.host.strip(), folder, None if port == 22 else port)
    else:
        path = Path(folder).expanduser()
        if not path.is_absolute():
            raise ValueError('Use an absolute folder path.')
        if not path.is_dir():
            raise ValueError('Folder does not exist.')
        value = str(path)
    roots[name] = value
    settings['Editor']['project_roots'] = roots


def remove_root(settings, name):
    roots = dict(settings['Editor']['project_roots'])
    roots.pop(name, None)
    settings['Editor']['project_roots'] = roots


def ios_interfaces():
    """Connected LAN addresses from Darwin's getifaddrs; psutil has no iOS port."""
    import ctypes as c

    # Darwin ABI (not Linux sockaddr): length and family are each one byte.
    class SockaddrIn(c.Structure):
        _fields_ = [('length', c.c_uint8), ('family', c.c_uint8),
                    ('port', c.c_uint16), ('address', c.c_ubyte * 4),
                    ('padding', c.c_ubyte * 8)]

    class Ifaddrs(c.Structure):
        pass

    Ifaddrs._fields_ = [('next', c.POINTER(Ifaddrs)), ('name', c.c_char_p),
                       ('flags', c.c_uint), ('address', c.POINTER(SockaddrIn)),
                       ('netmask', c.POINTER(SockaddrIn)), ('destination', c.c_void_p),
                       ('data', c.c_void_p)]
    libc = c.CDLL(None, use_errno=True)
    libc.getifaddrs.argtypes = [c.POINTER(c.POINTER(Ifaddrs))]
    libc.getifaddrs.restype = c.c_int
    libc.freeifaddrs.argtypes = [c.POINTER(Ifaddrs)]
    libc.freeifaddrs.restype = None
    head = c.POINTER(Ifaddrs)()
    if libc.getifaddrs(c.byref(head)) != 0:
        raise OSError(c.get_errno(), 'Could not read local network interfaces')
    result = []
    try:
        current = head
        while current:
            entry = current.contents
            current = entry.next
            # IFF_UP | IFF_BROADCAST | IFF_RUNNING: exclude cellular and VPN links.
            if entry.flags & 0x43 != 0x43 or not (entry.name and entry.address and entry.netmask):
                continue
            address, netmask = entry.address.contents, entry.netmask.contents
            if address.family != socket.AF_INET or address.length < 8 or netmask.length < 4:
                continue
            # BSD may omit trailing zero bytes from a netmask sockaddr.
            mask = c.string_at(c.addressof(netmask) + 4, min(4, netmask.length - 4)).ljust(4, b'\0')
            result.append((entry.name.decode(), socket.inet_ntoa(bytes(address.address)),
                           socket.inet_ntoa(mask)))
    finally:
        libc.freeifaddrs(head)
    return result


def local_networks():
    """Connected IPv4 networks; large networks default to this machine's /24."""
    if sys.platform == 'ios':
        interfaces = ios_interfaces()
    else:
        import psutil
        stats = psutil.net_if_stats()
        interfaces = [(name, address.address, address.netmask)
                      for name, addresses in psutil.net_if_addrs().items()
                      if name in stats and stats[name].isup
                      for address in addresses if address.family == socket.AF_INET and address.netmask]
    result = {}
    for interface, address, netmask in interfaces:
        ip = ipaddress.IPv4Address(address)
        if ip.is_loopback or ip.is_unspecified or ip.is_multicast:
            continue
        network = ipaddress.IPv4Network(f'{ip}/{netmask}', strict=False)
        network = ipaddress.IPv4Network(f'{ip}/{max(24, network.prefixlen)}', strict=False)
        result[f'{interface} · {network}'] = str(network)
    return result


def ssh_available(host, port=22, timeout=0.6):
    """Read only the server identification: no login, keys or known-host changes."""
    deadline = time.monotonic() + timeout
    try:
        with socket.create_connection((host, port), timeout=timeout) as connection:
            banner = b''
            while len(banner) < 1024:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                connection.settimeout(remaining)
                chunk = connection.recv(1024 - len(banner))
                if not chunk:
                    break
                banner += chunk
                if any(line.startswith((b'SSH-2.0-', b'SSH-1.99-'))
                       for line in banner.splitlines()):
                    return True
    except OSError:
        pass
    return False


def dns_name(packet, offset):
    """Read a DNS name, including compressed PTR replies, without following loops."""
    labels, seen, end = [], set(), None
    while True:
        if offset in seen or offset >= len(packet) or len(seen) >= 128:
            raise ValueError('Invalid DNS name')
        seen.add(offset)
        size = packet[offset]
        offset += 1
        if size & 0xc0 == 0xc0:
            if offset >= len(packet):
                raise ValueError('Truncated DNS pointer')
            end = end or offset + 1
            offset = ((size & 0x3f) << 8) | packet[offset]
        elif size == 0:
            name = '.'.join(labels)
            if len(name.encode('utf-8')) > 253 or any(ord(char) < 32 or ord(char) == 127 for char in name):
                raise ValueError('Invalid DNS hostname')
            return name, end or offset
        else:
            if size > 63 or offset + size > len(packet):
                raise ValueError('Invalid DNS label')
            labels.append(packet[offset:offset + size].decode('utf-8'))
            offset += size


def ptr_hostname(packet, reverse):
    """Only accept a PTR for the address we asked about; ignore unrelated records."""
    _, flags, questions, answers, authorities, additional = struct.unpack_from('!6H', packet)
    if not flags & 0x8000 or flags & 0x020f:  # response, not truncated, no DNS error
        return ''
    offset = 12
    for _ in range(questions):
        _, offset = dns_name(packet, offset)
        offset += 4
    for _ in range(answers + authorities + additional):
        owner, offset = dns_name(packet, offset)
        kind, family, _, size = struct.unpack_from('!HHIH', packet, offset)
        offset += 10
        if offset + size > len(packet):
            raise ValueError('Truncated DNS record')
        if kind == 12 and family & 0x7fff == 1 and owner.casefold() == reverse.casefold():
            name, end = dns_name(packet, offset)
            if end > offset + size:
                raise ValueError('Invalid PTR record')
            return name
        offset += size
    return ''


def network_name(host, timeout=0.4, port=5353):
    """Ask this device's Bonjour responder directly (RFC 6762 §5.5).

    Unicast avoids a multicast entitlement on iOS. Unlike gethostbyaddr, this
    has a real deadline, so an absent name cannot stall scanning or app exit.
    """
    reverse = ipaddress.IPv4Address(host).reverse_pointer
    question = b''.join(bytes([len(label)]) + label.encode('ascii') for label in reverse.split('.')) + b'\0'
    query = struct.pack('!6H', 0, 0, 1, 0, 0, 0) + question + struct.pack('!HH', 12, 1)
    deadline = time.monotonic() + timeout
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
            connection.connect((host, port))
            connection.send(query)
            while (remaining := deadline - time.monotonic()) > 0:
                connection.settimeout(remaining)
                try:
                    name = ptr_hostname(connection.recv(9000), reverse)
                except (ValueError, UnicodeError, struct.error):
                    continue
                if name:
                    return name
    except OSError:
        pass
    return ''


def wait_for_network_access(job, wake):
    """iOS prompts on first use; a denial stays actionable while Settings is open."""
    if sys.platform != 'ios':
        return True
    import _melty_ios
    request = _melty_ios.request_local_network_access()
    deadline = time.monotonic() + 120
    try:
        while not job['cancel'].is_set():
            status = _melty_ios.local_network_access_status(request)
            if status != job['permission']:
                job['permission'] = status
                wake()
            if status == 'granted':
                return True
            if status == 'failed':
                raise OSError('Local network unavailable.')
            if time.monotonic() >= deadline:
                raise TimeoutError('Local Network access is required.' if status == 'denied'
                                   else 'Local network request timed out.')
            job['cancel'].wait(0.1)
        return False
    finally:
        # The native capsule cancels its listener and browser on release.
        del request


def start_scan(network, wake=request_render):
    """One cancellable, bounded job per view; workers only write plain job fields."""
    if network and ipaddress.IPv4Network(network).num_addresses > 256:
        raise ValueError('Choose a network with at most 256 addresses.')
    job = dict(cancel=threading.Event(), done=False, completed=0,
               total=0, hosts=(), error='', permission='pending' if sys.platform == 'ios' else 'granted',
               networks=None, network=network, names={})

    def run():
        def probe(host):
            return not job['cancel'].is_set() and ssh_available(host)

        try:
            if not wait_for_network_access(job, wake):
                return
            networks = local_networks()
            job['network'] = network if network in networks.values() else next(iter(networks.values()), '')
            job['networks'] = (networks, job['network'])
            if not job['network']:
                raise ValueError('No local IPv4 network.')
            subnet = ipaddress.IPv4Network(job['network'])
            if subnet.num_addresses > 256:
                raise ValueError('Choose a network with at most 256 addresses.')
            hosts = [str(host) for host in subnet.hosts()]
            job['total'] = len(hosts)
            job['permission'] = 'granted'
            wake()
            pool = ThreadPoolExecutor(max_workers=64, thread_name_prefix='ssh-discovery')
            try:
                pending = {pool.submit(probe, host): ('probe', host) for host in hosts}
                while pending and not job['cancel'].is_set():
                    ready, _ = wait(pending, timeout=0.05, return_when=FIRST_COMPLETED)
                    for future in ready:
                        if job['cancel'].is_set():
                            break
                        kind, host = pending.pop(future)
                        result = future.result()
                        if kind == 'name':
                            if result:
                                job['names'] = {**job['names'], host: result}
                        else:
                            job['completed'] += 1
                            if result:
                                job['hosts'] += (host,)
                                pending[pool.submit(network_name, host)] = ('name', host)
                    if ready:
                        wake()
            finally:
                pool.shutdown(wait=True, cancel_futures=True)
        except Exception as error:
            job['error'] = str(error)
        finally:
            job['done'] = True
            wake()

    threading.Thread(target=run, name='ssh-discovery', daemon=True).start()
    return job


def check_ssh(root, key=None):
    """Only public connection results enter view state; secrets stay in Keychain."""
    job = dict(done=False, error='', fingerprint='', key=None)

    def work():
        from meltygui.model.ssh_auth_model import UnknownHostKey, trust_host
        from meltygui.model.ssh_file_model import sftp, request, native_path
        try:
            if key is not None:
                trust_host(root.location, key)
            with sftp(root.location) as client:
                client.stat(native_path(client, root.location))
            request(root.location, 'rows', refresh=True)
        except UnknownHostKey as error:
            job.update(fingerprint=error.fingerprint, key=error.key)
        except Exception as error:
            job['error'] = str(error)
        finally:
            job['done'] = True
            request_render()

    threading.Thread(target=work, name='ssh-auth', daemon=True).start()
    return job


def poll_ssh_auth(state):
    auth = state.auth
    if auth is None or auth.get('prompt') is None:
        return
    import _melty_ios
    result = _melty_ios.ssh_configuration_status(auth['prompt'])
    if result['status'] == 'pending':
        return
    auth['prompt'] = None
    if result['status'] == 'saved':
        auth['job'] = check_ssh(auth['root'])
    elif result['status'] == 'cancelled':
        state.auth = None
    else:
        auth['job'] = dict(done=True, error=result['error'], fingerprint='')


def cleanup_settings(draw_state):
    state = draw_state.misc.get('state')
    if state is not None and state.scan is not None:
        state.scan['cancel'].set()
    if state is not None:
        state.auth = None  # Release any native credential prompt.


@render_func(selectable=False, on_cleanup=cleanup_settings)
def draw_settings(input_value: CodeDict, draw_state, state: SettingsState = None):
    poll_ssh_auth(state)
    if state.auth and (state.auth.get('prompt') is not None or not state.auth['job']['done']):
        draw_state.invalidate()
    if state.scan is not None and state.scan.get('networks') is not None:
        state.networks, state.network = state.scan['networks']
        state.scan['networks'] = None
    if state.scan is not None and not state.scan['done']:
        draw_state.invalidate()
    picked, tabs = draw_tab_bar([state.tab], collection=['Settings', 'Project Roots'],
                                name='settings-tabs', show_header=False)
    if picked and tabs:
        state.tab = tabs[-1]
    if state.tab == 'Settings':
        return draw_any(input_value, name='all-settings', show_header=False)
    return draw_roots(input_value, state, draw_state), input_value


def draw_roots(settings, state, draw_state):
    """Compact form; settings own roots, this view owns only the draft and scan."""
    left, top = imgui.get_cursor_screen_pos()
    left, top = left + 12, top + 12
    width = max(180, draw_state.content_width - 24)
    row = 30
    draw_list = imgui.get_window_draw_list()
    color = pack_color(*Tint.dd_text(), 1)
    changed = False
    scan_requested = False
    auth_requested = None

    def label(text, x=0, available=None):
        draw_list.push_clip_rect(left + x, top, left + (width if available is None else x + available), top + row, True)
        draw_list.add_text(left + x, top + 5, color, text)
        draw_list.pop_clip_rect()

    def button(text, identity, x, size):
        imgui.set_cursor_screen_pos((left + x, top))
        return flat_button(text, draw_state, view_id=identity, width=size, height=row - 2)

    def field(title, attribute):
        nonlocal top
        label(title, available=64)
        imgui.set_cursor_screen_pos((left + 68, top))
        edited, value = draw_str(getattr(state, attribute), name=attribute,
                                 width=width - 68, height=row - 2, show_header=False)
        if edited:
            setattr(state, attribute, value.replace('\n', '').replace('\r', ''))
        top += row + 4

    roots = dict(settings['Editor']['project_roots'])
    for name, root in roots.items():
        label(str(name), available=width - 36)
        if button('\uf1f8', ('delete-root', name), width - 30, 30):
            remove_root(settings, name)
            if state.auth and state.auth['name'] == name:
                state.auth = None
            changed = True
        top += row - 6
        location = f'{root.target}{":" + str(root.port) if root.port else ""} · {root.directory}' if isinstance(root, SSH) else str(root)
        label(location, available=width - 36)
        top += row + 4
        if isinstance(root, SSH) and sys.platform == 'ios':
            for index, (title, kind) in enumerate((('Password', 'password'), ('SSH Key', 'key'), ('Connect', 'connect'))):
                size = (width - 12) / 3
                if button(title, ('ssh-auth', name, kind), index * (size + 6), size):
                    auth_requested = (name, root, kind, None)
            top += row + 4
            auth = state.auth
            if auth and auth['name'] == name and auth['root'] == root:
                job = auth.get('job')
                if job is None or not job['done']:
                    label('Authenticating…' if job is None else 'Connecting…')
                    top += row
                elif job['fingerprint']:
                    label('Verify server fingerprint')
                    top += row
                    # A SHA256 fingerprint must remain fully visible on phones.
                    for part in (job['fingerprint'][:27], job['fingerprint'][27:]):
                        label(part)
                        top += row
                    if button('Trust & Connect', ('trust-ssh', name), 0, min(width, 175)):
                        auth_requested = (name, root, 'connect', job['key'])
                    top += row + 4
                else:
                    label(job['error'] or 'Connected')
                    top += row
    if roots:
        top += 8

    imgui.set_cursor_screen_pos((left, top))
    picked, kinds = draw_tab_bar([state.kind], collection=['Local', 'SSH'],
                                 name='root-kind', show_header=False)
    if picked and kinds:
        state.kind = kinds[-1]
    top = imgui.get_cursor_screen_pos()[1] + 12
    field('Name', 'name')
    if state.kind == 'SSH':
        field('Host', 'host')
    field('Folder', 'folder')
    if state.kind == 'SSH':
        field('Port', 'port')
    if button('Add', 'add-root', width - 64, 64):
        try:
            add_root(settings, state)
            state.name = state.host = state.error = ''
            changed = True
        except (ValueError, OSError) as error:
            state.error = str(error)
    top += row + 4
    if state.error:
        label(state.error)
        top += row

    if state.kind == 'SSH':
        top += 8
        if state.networks is None:
            try:
                state.networks = local_networks()
            except (ImportError, OSError) as error:
                state.networks = {}
                state.error = str(error)
        if state.network not in state.networks.values():
            state.network = next(iter(state.networks.values()), '')
        scanning = state.scan is not None and not state.scan['done']
        imgui.set_cursor_screen_pos((left, top))
        _, state.network = draw_dropdown(state.network, collection=state.networks,
                                          name='scan-network', width=width - 80,
                                          trigger_height=row - 2, show_header=False)
        if button('Stop' if scanning else 'Scan', 'scan', width - 72, 72):
            if scanning:
                state.scan['cancel'].set()
            else:
                scan_requested = True
        top += row + 4
        if not state.networks and state.scan is None:
            label('No local IPv4 network')
            top += row
        if state.scan is not None:
            job = state.scan
            status = 'Stopped' if job['cancel'].is_set() else ('Done' if job['done'] else f'{job["completed"]}/{job["total"]}')
            if scanning and job.get('permission') in ('pending', 'denied'):
                label('Local Network access required' if job['permission'] == 'denied' else 'Requesting network access…')
            else:
                label(job['error'] or f'{status} · {len(job["hosts"])} found')
            top += row
            if job.get('permission') == 'denied':
                if button('Open Settings', 'network-settings', 0, 140):
                    import _melty_ios
                    _melty_ios.open_app_settings()
                top += row + 4
            for host in sorted(job['hosts'], key=ipaddress.IPv4Address):
                name = job.get('names', {}).get(host, '')
                title = f'{name} · {host}' if name else host
                if button(title, ('ssh-host', host), 0, width):
                    user, sep, _ = state.host.rpartition('@')
                    state.host = f'{user}@{host}' if sep else host
                    state.name = state.name or name.removesuffix('.local') or host
                    state.port = '22'
                    draw_state.invalidate()  # The form above was drawn before this selection.
                top += row + 2

    imgui.set_cursor_screen_pos((left, top))
    imgui.dummy(width, 1)
    if scan_requested:
        state.error = ''
        state.scan = start_scan(state.network)
        draw_state.invalidate()
    if auth_requested:
        name, root, kind, key = auth_requested
        from meltygui.model.ssh_auth_model import server
        import _melty_ios
        state.auth = dict(name=name, root=root, prompt=None, job=None)
        if kind == 'connect':
            state.auth['job'] = check_ssh(root, key)
        else:
            state.auth['prompt'] = _melty_ios.configure_ssh(*server(root.location), kind)
        draw_state.invalidate()
    return changed
