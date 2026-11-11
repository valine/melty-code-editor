"""Renders icon/melty-code-editor.png: the syntax-colored editor as a squircle, glowing voxels on it.

Run: python3 icon/make_icon.py [LOOK [out.png]]    one look (default 'blue-p3') -> icon/melty-code-editor.png
     python3 icon/make_icon.py all                 every look -> icon/variants/<look>.png + sheet.png (HDR)
Needs blender, PIL, numpy.

Three stages, top to bottom: `texture` draws the code with PIL, `scene` (inside Blender, Cycles)
lights it with the voxels, `encode` adds bloom + shadow and writes the dock's 16-bit BT.2020 PQ PNG.
The levers are the constants below; a look in LOOKS overrides any of them by name.
Scene units are squircle half-widths, 1.0 light = WHITE nits.
"""
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

import numpy as np

try:
    import bpy
except ImportError:
    bpy = None

SIZE, SUPER = 512, 2  # output pixels, render supersample
FILL = 0.92  # squircle width / canvas width; the rest holds the shadow and glow
WHITE = 203  # nits of linear 1.0 (the HDR icons' reference white)
GAMUT = 'srgb'  # the primaries voxel colors are given in; 'p3' and 'bt2020' reach past sRGB (the code stays sRGB)
PRIMARIES = {'srgb': ((0.64, 0.33), (0.30, 0.60), (0.15, 0.06)), 'p3': ((0.68, 0.32), (0.265, 0.69), (0.15, 0.06)),
             'bt2020': ((0.708, 0.292), (0.17, 0.797), (0.131, 0.046))}

# The editor. Colors are meltygui/editor/text_editor.py COLORS. Sized for the dock (62 px): six bold lines.
FONT = Path.home() / 'Desktop/meltygui/meltygui/resources/jetbrains-weights/JetBrainsMono-Bold.ttf'
FONT_PX, LINE, LEFT, TOP = 0.088, 1.42, 0.085, 0.13  # of the squircle width; LINE in font heights
INK = {'default': '#a9b7c6', 'keyword': '#cc7832', 'def_name': '#56a8f5', 'decorator': '#bbb529',
       'string': '#6a8759', 'number': '#6897bb', 'builtin': '#8583cf', 'comment': '#5e6265', 'line_no': '#808080'}
CODE = [
    [('@render_func', 'decorator')],
    [('def ', 'keyword'), ('voxel', 'def_name'), ('(p):', 'default')],
    [('    glow = ', 'default'), ('4.0', 'number')],
    [('    emit(p, glow)', 'default')],
    [('    ', 'default'), ('return ', 'keyword'), ('"lit"', 'string')],
    [('voxel(', 'default'), ('here', 'builtin'), (')', 'default')],
]
PAPER, GROUND = (0.06, 0.075, 0.14), 12  # the code background: its hue, and the nits it emits
SKY = 0.5  # world light: the rim's shine, a few nits on the face
PANEL = True  # False: no code background at all; the code and voxels sit on whatever is behind the icon
TEXT_LIGHT = 3.8  # code brightness over its sRGB color at WHITE; 3.0 is about the real editor's on a 557-nit desktop

# The voxels: (cell, color, light x WHITE) on a grid of CUBE-long edges, GAP of an edge apart, centred on
# AT (x, y) at HEIGHT and turned yaw / tilt / roll degrees (tilt -54.7 = isometric, 0 = flat squares).
EMBER, ORANGE, GOLD = (1.0, 0.2, 0.04), (1.0, 0.5, 0.1), (1.0, 0.7, 0.22)
RAMP = ((EMBER, 2.2), (ORANGE, 3.2), (GOLD, 5.5))  # dim to hot; the hottest sits near the display's ~1200-nit peak
STAIR, COLUMN = ((0, 0, 0), (1, 0, 0), (1, 0, 1)), ((0, 0, 0), (0, 0, 1), (0, 0, 2))
VOXELS = [(cell, *RAMP[i]) for i, cell in enumerate(STAIR)]
CUBE, GAP, AT, HEIGHT, TURN = 0.43, 0.2, (0.43, -0.3), 1.2, (45, -54.7, 0)
# Glow, three levers: 'under' is the light the voxels throw on the code, 'edge' the hot rims, BLOOM the halo.
FACES = {'top': 1.0, 'left': 0.66, 'right': 0.42, 'edge': 1.2, 'under': 0.12}  # of a voxel's light
HEAT = 1.0  # how far bright faces pale and dim faces deepen; lower keeps a hue saturated
BLOOM_FROM, BLOOM = 2.6, ((4, 0.12), (14, 0.05))  # light above this blooms: (sigma in output px, weight)
SPILL = 8  # px the glow may leave the squircle before it has faded out
SHADOW = (7, 4, 0.5)  # the icon's shadow: blur px, drop px, opacity
CAST = (12, 12, 0.75)  # the voxels' shadow on the code, the same three
KEY = 0.5  # without a panel, light this bright (in WHITEs) and brighter is opaque

# The alternates kept from the studies; `all` renders them beside the icon.
ink = lambda kind: tuple((int(INK[kind][i:i + 2], 16) / 255) ** 2.2 for i in (1, 3, 5))
bright = lambda color: tuple(c / max(color) for c in color)
recolor = lambda *colors: [(cell, color, light) for (cell, _, light), color in zip(VOXELS, colors)]
column = [(cell, *RAMP[i]) for i, cell in enumerate(COLUMN)]
relit = lambda *ramp: [(cell, *ramp[i]) for i, cell in enumerate(STAIR)]
PEAK = 1600 / WHITE  # the brightest an iPhone or iPad panel shows, in WHITEs: the key voxels' top faces sit here
DEEP = relit(((0.0, 0.03, 1.0), 0.73 * PEAK), ((0.07, 0.05, 1.0), PEAK), ((0.24, 0.26, 1.0), PEAK))  # deep blue, royal, periwinkle
NIGHT = dict(PAPER=(0.02, 0.03, 0.3), GROUND=2, SKY=0.3)
VIOLET = relit(((0.08, 0.2, 1.0), 3.6), ((0.4, 0.1, 1.0), 4.6), ((0.62, 0.34, 1.0), 5.5))  # blue, violet, lavender
LOOKS = {
    'stair': {},
    'column': dict(VOXELS=column, CUBE=0.4, GAP=0.18, AT=(0.56, -0.12)),
    'lean': dict(VOXELS=column, CUBE=0.4, GAP=0.18, AT=(0.42, -0.2), TURN=(45, -54.7, -28)),  # the Pages pen's diagonal
    'pair': dict(VOXELS=column[1:], CUBE=0.48, AT=(0.5, -0.24)),
    'syntax': dict(VOXELS=recolor(*(bright(ink(kind)) for kind in ('def_name', 'keyword', 'decorator')))),
    'ice': dict(VOXELS=recolor((0.02, 0.2, 1.0), (0.05, 0.75, 1.0), (0.7, 0.95, 1.0))),
    'violet': dict(VOXELS=VIOLET, HEAT=0.4, PAPER=(0.07, 0.05, 0.3), GROUND=9),  # the first blue-purple, in sRGB
    # Blue-purple past sRGB: pure primaries at the display's peak, on a dark blue ground.
    'blue-p3': dict(VOXELS=DEEP, HEAT=0.25, GAMUT='p3', PANEL=False, TEXT_LIGHT=2.0),  # the installed icon: no panel, dim code
    'blue-p3-panel': dict(VOXELS=DEEP, HEAT=0.25, GAMUT='p3', **{**NIGHT, 'PAPER': (0.045, 0.055, 0.25)}, TEXT_LIGHT=3.3),  # the same on its panel: greyer ground, dimmer code
    'blue-2020': dict(VOXELS=DEEP, HEAT=0.25, GAMUT='bt2020', **NIGHT),
    'blue-p3-black': dict(VOXELS=DEEP, HEAT=0.25, GAMUT='p3', **{**NIGHT, 'GROUND': 0.5, 'SKY': 0.2}),
    'blue-p3-lit': dict(VOXELS=DEEP, HEAT=0.25, GAMUT='p3', **{**NIGHT, 'GROUND': 6}),
    'black': dict(PAPER=(0.09, 0.09, 0.101), GROUND=0),  # the real editor's background
    'hot': dict(VOXELS=[(cell, color, light * 2) for cell, color, light in VOXELS], BLOOM=((5, 0.3), (18, 0.15)),
                FACES={**FACES, 'under': 0.4, 'edge': 1.5}),  # glowier, past the display's peak
}


def turn():
    """The voxels' rotation: yaw about z, then tilt about x, then roll about the view axis."""
    def about(axis, degrees):
        c, s = np.cos(np.radians(degrees)), np.sin(np.radians(degrees))
        return np.array([[1, 0, 0], [0, c, -s], [0, s, c]] if axis == 'x' else [[c, -s, 0], [s, c, 0], [0, 0, 1]])
    return about('z', TURN[2]) @ about('x', TURN[1]) @ about('z', TURN[0])


def centres():
    """Each voxel's centre before the turn, the cluster's middle at the origin."""
    cells = np.array([cell for cell, _, _ in VOXELS], float)
    return (cells - (cells.min(axis=0) + cells.max(axis=0)) / 2) * CUBE * (1 + GAP)


def footprint(px, over=2):
    """The voxels' outline as the camera sees it: a 0..1 mask, px square."""
    from itertools import product
    from PIL import Image, ImageDraw
    mask = Image.new('L', (px * over, px * over))
    unit = np.array(list(product((-0.5, 0.5), repeat=3)))
    for centre in centres():
        xy = (((unit * CUBE + centre) @ turn().T)[:, :2] + AT) * (FILL / 2, -FILL / 2) * px * over + px * over / 2
        for axis, side in product(range(3), (-0.5, 0.5)):  # each face is a convex quad
            quad = xy[unit[:, axis] == side]
            quad = quad[np.argsort(np.arctan2(*(quad - quad.mean(axis=0)).T))]
            ImageDraw.Draw(mask).polygon([tuple(point) for point in quad], fill=255)
    return np.asarray(mask.reduce(over), float) / 255


def texture(path, px=2048):
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new('RGB', (px, px), 'black')
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(str(FONT), round(FONT_PX * px))
    step, char = FONT_PX * LINE * px, font.getlength('0')
    for row, tokens in enumerate(CODE):
        y = TOP * px + row * step
        draw.text((LEFT * px + 2 * char, y), str(row + 1), font=font, fill=INK['line_no'], anchor='ra')
        x = LEFT * px + 3.4 * char
        for text, kind in tokens:
            draw.text((x, y), text, font=font, fill=INK[kind])
            x += font.getlength(text)
    # Blender reads the PNG as sRGB: store the code's colors in the working primaries, so they keep their hue.
    v = np.asarray(image) / 255
    v = np.clip(np.where(v <= 0.04045, v / 12.92, ((v + 0.055) / 1.055) ** 2.4) @ gamut()[1].T, 0, 1)
    Image.fromarray(np.rint(np.where(v <= 0.0031308, v * 12.92, 1.055 * v ** (1 / 2.4) - 0.055) * 255).astype(np.uint8)).save(path)


def scene(texture_path, out_path):
    import bmesh
    from math import cos, pi, sin
    from mathutils import Matrix, Vector

    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    sc.render.engine = 'CYCLES'
    prefs = bpy.context.preferences.addons['cycles'].preferences
    for kind in ('OPTIX', 'CUDA'):
        try:
            prefs.compute_device_type = kind
            prefs.get_devices()
            for device in prefs.devices:
                device.use = device.type == kind
            sc.cycles.device = 'GPU'
            break
        except TypeError:
            pass
    sc.cycles.samples, sc.cycles.use_denoising = 512, True
    sc.render.resolution_x = sc.render.resolution_y = SIZE * SUPER
    sc.render.film_transparent = True
    sc.render.image_settings.file_format = 'OPEN_EXR'
    sc.render.image_settings.color_depth = '32'
    sc.render.filepath = out_path + '.exr'

    def material(name, **inputs):
        mat = bpy.data.materials.new(name)
        mat.use_nodes = True
        bsdf = mat.node_tree.nodes['Principled BSDF']
        for key, value in inputs.items():
            bsdf.inputs[key].default_value = value
        return mat, bsdf

    def add(name, bm, *mats):
        mesh = bpy.data.meshes.new(name)
        bm.to_mesh(mesh)
        obj = bpy.data.objects.new(name, mesh)
        for mat in mats:
            mesh.materials.append(mat)
        sc.collection.objects.link(obj)
        return obj

    # The editor panel: a superellipse slab whose top face shows the code.
    bm = bmesh.new()
    ring = [bm.verts.new((abs(cos(t)) ** 0.4 * (1 if cos(t) >= 0 else -1), abs(sin(t)) ** 0.4 * (1 if sin(t) >= 0 else -1), 0))
            for t in (2 * pi * i / 480 for i in range(480))]
    top = bm.faces.new(ring)
    uv = bm.loops.layers.uv.new()
    for loop in top.loops:
        loop[uv].uv = (loop.vert.co.x / 2 + 0.5, loop.vert.co.y / 2 + 0.5)
    sides = bmesh.ops.extrude_face_region(bm, geom=[top])['geom']
    bmesh.ops.translate(bm, verts=[v for v in sides if isinstance(v, bmesh.types.BMVert)], vec=(0, 0, -0.12))
    bmesh.ops.bevel(bm, geom=top.edges[:], offset=0.035, segments=4, affect='EDGES', profile=0.5)  # the rim that catches light
    panel, bsdf = material('panel', **{'Base Color': (0.02, 0.022, 0.03, 1), 'Roughness': 0.42})
    tree = panel.node_tree
    image = tree.nodes.new('ShaderNodeTexImage')
    image.image, image.extension = bpy.data.images.load(texture_path), 'CLIP'
    mix = tree.nodes.new('ShaderNodeMixRGB')
    mix.blend_type = 'ADD'
    mix.inputs['Fac'].default_value = 1
    to_bt2020, from_srgb = gamut()
    tint = from_srgb @ PAPER
    tint = tint / ((0.2627, 0.678, 0.0593) @ to_bt2020 @ tint)  # unit luminance, so GROUND is nits
    mix.inputs['Color1'].default_value = (*(tint * GROUND * PANEL / WHITE / TEXT_LIGHT), 1)
    tree.links.new(image.outputs['Color'], mix.inputs['Color2'])
    tree.links.new(mix.outputs['Color'], bsdf.inputs['Emission Color'])
    bsdf.inputs['Emission Strength'].default_value = TEXT_LIGHT
    add('panel', bm, panel)

    # The voxels: beveled so the edges are their own (hotter) faces; the faces the camera sees are
    # named by where they point on screen, the highest one 'top'.
    spin = Matrix(turn().tolist())
    order = list(FACES)
    for index, ((_, color, light), centre) in enumerate(zip(VOXELS, centres())):
        bm = bmesh.new()
        bmesh.ops.create_cube(bm, size=CUBE)
        bmesh.ops.bevel(bm, geom=bm.edges[:], offset=CUBE * 0.045, segments=3, affect='EDGES', profile=0.5)
        flat = [f for f in bm.faces if f.calc_area() > CUBE * CUBE * 0.5]
        seen = sorted((f for f in flat if (spin @ f.normal).z > 1e-3), key=lambda f: -(spin @ f.normal).y)
        slot = dict(zip([seen[0], *sorted(seen[1:], key=lambda f: (spin @ f.normal).x)], order))
        for face in bm.faces:
            face.material_index = order.index(slot.get(face, 'under') if face in flat else 'edge')
        heat = lambda k: tuple(c ** (1 + HEAT * (0.6 - FACES[k] if k != 'edge' else -0.65)) for c in color)
        cube = add(f'voxel{index}', bm, *(material(f'{k}{index}', **{
            'Base Color': (0, 0, 0, 1), 'Emission Color': (*heat(k), 1), 'Emission Strength': light * FACES[k] * (PANEL or k != 'under')})[0] for k in order))
        cube.matrix_world = Matrix.Translation((*AT, HEIGHT)) @ spin.to_4x4() @ Matrix.Translation(Vector(centre))

    world = bpy.data.worlds.new('world')
    world.use_nodes = True
    world.node_tree.nodes['Background'].inputs['Color'].default_value = (0.55, 0.65, 1, 1)
    world.node_tree.nodes['Background'].inputs['Strength'].default_value = SKY * PANEL
    sc.world = world
    camera = bpy.data.objects.new('camera', bpy.data.cameras.new('camera'))
    camera.data.type, camera.data.ortho_scale, camera.location = 'ORTHO', 2 / FILL, (0, 0, 10)
    sc.collection.objects.link(camera)
    sc.camera = camera

    bpy.ops.render.render(write_still=True)
    result = bpy.data.images.load(out_path + '.exr')
    pixels = np.empty(len(result.pixels), np.float32)
    result.pixels.foreach_get(pixels)
    np.save(out_path, pixels.reshape(result.size[1], result.size[0], 4)[::-1])


def blur(a, sigma):
    pad = int(3 * sigma) + 1
    a = np.pad(a, ((pad, pad), (pad, pad), (0, 0)))
    fy, fx = np.fft.fftfreq(a.shape[0])[:, None], np.fft.rfftfreq(a.shape[1])[None]
    gauss = np.exp(-2 * np.pi ** 2 * sigma ** 2 * (fx ** 2 + fy ** 2))[..., None]
    return np.fft.irfft2(np.fft.rfft2(a, axes=(0, 1)) * gauss, s=a.shape[:2], axes=(0, 1))[pad:-pad, pad:-pad]


def hdr():
    """The dock's HDR helpers (hyprland-hdr's desktop/hdr_icons.py, hdr_colors.py)."""
    sys.path.insert(0, str(Path.home() / '.local/opt/hyprland-hdr/desktop'))
    import hdr_colors
    import hdr_icons
    return hdr_icons, hdr_colors


def gamut():
    """The working primaries as matrices: to BT.2020, and from sRGB (what the code and PAPER are given in)."""
    icons, colors = hdr()
    out = colors.to_bt2020(np.array([*PRIMARIES[GAMUT], (0.3127, 0.3290)]))
    return out, np.linalg.inv(out) @ icons.SRGB_TO_BT2020


def save(nits, alpha, path):
    """BT.2020 nits + alpha -> the dock's HDR PNG, with the ICC twin of its cICP chunk for ICC viewers."""
    icons, _ = hdr()
    icons.write_hdr_png(np.clip(nits, 0, 10000), alpha, path)
    data, profile = Path(path).read_bytes(), (Path.home() / 'Desktop/icondesigned-melty/rec2020-pq.icc').read_bytes()
    Path(path).write_bytes(data[:33] + icons.png_chunk(b'iCCP', b'BT.2020 PQ\0\0' + zlib.compress(profile)) + data[33:])


def encode(render, path):
    """Premultiplied linear Rec.709 floats -> bloom, shadow, 16-bit BT.2020 PQ PNG."""
    px = render.astype(float)
    px = px.reshape(SIZE, SUPER, SIZE, SUPER, 4).mean(axis=(1, 3))
    rgb, alpha = px[..., :3], px[..., 3:]
    blur_px, drop, opacity = CAST
    voxels = footprint(SIZE)[..., None]
    cast = opacity * np.roll(blur(voxels, blur_px), drop, axis=0) * (1 - voxels)
    rgb = rgb * (1 - cast)
    glow = np.clip(sum(weight * blur(np.clip(rgb - BLOOM_FROM, 0, None), sigma) for sigma, weight in BLOOM), 0, None)
    rgb = rgb + glow * np.clip(blur(alpha, SPILL / 2) * 2, 0, 1) ** 2
    if PANEL:
        # Light that leaves the silhouette needs coverage of its own: enough alpha to carry it at 2x white.
        alpha = np.maximum(alpha, np.clip(rgb.max(axis=2, keepdims=True) / 2, 0, 1))
        blur_px, drop, opacity = SHADOW
        shade = np.roll(blur(px[..., 3:], blur_px), drop, axis=0) * opacity
        alpha = alpha + shade * (1 - alpha)
    else:
        # The render is light over black: its brightness is its coverage (KEY whites and up are opaque),
        # and the voxels' shadow darkens whatever shows through.
        alpha = np.maximum(np.clip(rgb.max(axis=2, keepdims=True) / KEY, 0, 1), cast * 0.6)
    straight = np.divide(rgb, alpha, out=np.zeros_like(rgb), where=alpha > 1e-6)
    save((straight @ gamut()[0].T) * WHITE, alpha[..., 0], path)


def sheet(paths, out, columns=4, ground=1.5, label=160.0):
    """The looks side by side on a dock-dark ground, as one HDR PNG: their real brightness against each other."""
    from PIL import Image, ImageDraw, ImageFont
    icons, colors = hdr()
    cell, rows = SIZE + 44, -(-len(paths) // columns)
    nits = np.full((rows * cell, columns * SIZE, 3), ground)
    names = Image.new('L', nits.shape[1::-1])
    font = ImageFont.truetype(str(FONT), 24)
    for index, path in enumerate(paths):
        x, y = index % columns * SIZE, index // columns * cell
        rgb, alpha, _ = icons.read_png16(path)
        nits[y:y + SIZE, x:x + SIZE] = colors.pq_decode(rgb) * alpha[..., None] + ground * (1 - alpha[..., None])
        ImageDraw.Draw(names).text((x + SIZE // 2, y + SIZE + 4), Path(path).stem, font=font, fill=255, anchor='ma')
    save(nits + np.asarray(names)[..., None] / 255 * label, np.ones(nits.shape[:2]), out)


if bpy:
    args = sys.argv[sys.argv.index('--') + 1:]
    globals().update(LOOKS[args[0]])
    scene(*args[1:])
elif __name__ == '__main__':
    look = sys.argv[1] if len(sys.argv) > 1 else 'blue-p3'
    here = Path(__file__).parent
    if look == 'all':  # each look in its own interpreter: a look rewrites this module's constants
        from concurrent.futures import ThreadPoolExecutor
        (here / 'variants').mkdir(exist_ok=True)
        for stale in (here / 'variants').glob('*.png'):
            stale.unlink()
        paths = [here / 'variants' / f'{name}.png' for name in LOOKS]
        with ThreadPoolExecutor(2) as pool:  # more at once runs a shared GPU out of memory
            list(pool.map(lambda name, path: subprocess.run([sys.executable, __file__, name, path], check=True), LOOKS, paths))
        sheet(paths, here / 'variants' / 'sheet.png')
    else:
        globals().update(LOOKS[look])
        out = sys.argv[2] if len(sys.argv) > 2 else here / 'melty-code-editor.png'
        with tempfile.TemporaryDirectory() as tmp:
            texture(f'{tmp}/code.png')
            done = subprocess.run(['blender', '-b', '--factory-startup', '-P', __file__, '--', look, f'{tmp}/code.png',
                                   f'{tmp}/render.npy'], capture_output=True, text=True)
            if done.returncode:
                sys.exit(f'{look}: blender failed\n{done.stdout[-600:]}{done.stderr[-600:]}')
            encode(np.load(f'{tmp}/render.npy'), out)
        print(out)
