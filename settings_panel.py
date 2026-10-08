"""The Settings tile: shared settings, named roots, and bounded SSH discovery."""
import ipaddress
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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


@no_save('networks', 'scan', 'error')
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
            if address.family != socket.AF_INET or address.length < 8 or netmask.length < 8:
                continue
            result.append((entry.name.decode(), socket.inet_ntoa(bytes(address.address)),
                           socket.inet_ntoa(bytes(netmask.address))))
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


def start_scan(network, wake=request_render):
    """One cancellable, bounded job per view; workers only write plain job fields."""
    subnet = ipaddress.IPv4Network(network)
    if subnet.num_addresses > 256:
        raise ValueError('Choose a network with at most 256 addresses.')
    hosts = [str(host) for host in subnet.hosts()]
    job = dict(cancel=threading.Event(), done=False, completed=0,
               total=len(hosts), hosts=(), error='')

    def run():
        def probe(host):
            return not job['cancel'].is_set() and ssh_available(host)

        try:
            with ThreadPoolExecutor(max_workers=16, thread_name_prefix='ssh-discovery') as pool:
                # Only one batch is outstanding, so Stop does not leave queued probes.
                for offset in range(0, len(hosts), 16):
                    if job['cancel'].is_set():
                        break
                    batch = hosts[offset:offset + 16]
                    for host, found in zip(batch, pool.map(probe, batch)):
                        if found:
                            job['hosts'] += (host,)
                        job['completed'] += 1
                    wake()
        except Exception as error:
            job['error'] = str(error)
        finally:
            job['done'] = True
            wake()

    threading.Thread(target=run, name='ssh-discovery', daemon=True).start()
    return job


def cleanup_settings(draw_state):
    state = draw_state.misc.get('state')
    if state is not None and state.scan is not None:
        state.scan['cancel'].set()


@render_func(selectable=False, on_cleanup=cleanup_settings)
def draw_settings(input_value: CodeDict, draw_state, state: SettingsState = None):
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
    refresh_requested = False

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
            changed = True
        top += row - 6
        location = f'{root.target}{":" + str(root.port) if root.port else ""} · {root.directory}' if isinstance(root, SSH) else str(root)
        label(location, available=width - 36)
        top += row + 4
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
                                          name='scan-network', width=width - 116,
                                          trigger_height=row - 2, show_header=False)
        refresh_requested = button('\uf021', 'refresh-networks', width - 110, 30)
        if button('Stop' if scanning else 'Scan', 'scan', width - 72, 72):
            if scanning:
                state.scan['cancel'].set()
            elif state.network:
                scan_requested = True
        top += row + 4
        if not state.networks:
            label('No local IPv4 network')
            top += row
        if state.scan is not None:
            job = state.scan
            status = 'Stopped' if job['cancel'].is_set() else ('Done' if job['done'] else f'{job["completed"]}/{job["total"]}')
            label(job['error'] or f'{status} · {len(job["hosts"])} found')
            top += row
            for host in job['hosts']:
                if button(host, ('ssh-host', host), 0, width):
                    user, sep, _ = state.host.rpartition('@')
                    state.host = f'{user}@{host}' if sep else host
                    state.name = state.name or host
                    state.port = '22'
                top += row + 2

    imgui.set_cursor_screen_pos((left, top))
    imgui.dummy(width, 1)
    if scan_requested:
        state.scan = start_scan(state.network)
        draw_state.invalidate()
    if refresh_requested:
        state.networks = None
        state.error = ''
    return changed
