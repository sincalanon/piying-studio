"""SAM3D 动画 GLB → 影棚白模：无脸蛋头、白色哑光人体、无缝浅灰背景、柔光、软阴影（EEVEE）。
用法: blender -b --python studio_scene.py -- in.glb out_dir [W H] [frames=all|a-b] [cam_az=-25] [hud=0|1]
hud=1 时在地面叠一层很淡的网格和脚下圆环，保留一点「软件视口」的味道。
"""
import bpy, bmesh, sys, math
from mathutils import Vector

argv = sys.argv[sys.argv.index('--') + 1:]
GLB, OUT = argv[0], argv[1]
W = int(argv[2]) if len(argv) > 2 else 864
H = int(argv[3]) if len(argv) > 3 else 1152
FR = argv[4] if len(argv) > 4 else 'all'
CAM_AZ = float(argv[5]) if len(argv) > 5 else -25.0
HUD = (argv[6] == '1') if len(argv) > 6 else False
CURVES = float(argv[7]) if len(argv) > 7 else 0.0
HEAD = argv[8] if len(argv) > 8 else 'egg'      # egg = 无脸蛋头；face = 保留中性五官
SHOT = argv[9] if len(argv) > 9 else 'full'     # full = 全身固定机位；close = 膝上跟拍   # 胸臀加丰满的强度，0 = 原始身形，1 = 默认幅度

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=GLB)
sc = bpy.context.scene
body = bpy.data.objects['Mesh_0']
bpy.data.objects['Icosphere'].hide_render = True
act = bpy.data.actions[0]
f0, f1 = int(act.frame_range[0]), int(act.frame_range[1])   # 跟随 GLB 动画长度

# ---------- 逐帧贴地（同 viewport_scene.py） ----------
def eval_verts(f):
    sc.frame_set(f)
    e = body.evaluated_get(bpy.context.evaluated_depsgraph_get())
    return [e.matrix_world @ v.co for v in e.data.vertices]

track, zmins = [], []
for f in range(f0, f1 + 1):
    vs = eval_verts(f)
    zmins.append(min(v.z for v in vs))
    c = sum(vs, Vector()) / len(vs)
    track.append([c.x, c.y])
zs = [sum(zmins[max(0, i - 2):i + 3]) / len(zmins[max(0, i - 2):i + 3]) for i in range(len(zmins))]
fix = bpy.data.objects.new('ground_fix', None); sc.collection.objects.link(fix)
bpy.data.objects['track00'].parent = fix
for i, z in enumerate(zs):
    fix.location = (0, 0, -z); fix.keyframe_insert('location', frame=f0 + i)
cx = sum(t[0] for t in track) / len(track); cy = sum(t[1] for t in track) / len(track)

# ---------- 头部 ----------
co = [v.co for v in body.data.vertices]          # 绑定姿势下的局部坐标（实测 +Z 朝上）
ztop = max(c.z for c in co); zbot = min(c.z for c in co); hgt = ztop - zbot
cut = ztop - 0.105 * hgt                          # 下巴附近，保留脖子
head_idx = [v.index for v in body.data.vertices if v.co.z > cut]
vg = body.vertex_groups.new(name='head'); vg.add(head_idx, 1.0, 'REPLACE')
if HEAD == 'face':
    # 中性脸：表情形态键清零并去掉其动画；脸部轻度平滑，抹掉原舞者的细节特征但保留五官位置
    if body.data.shape_keys:
        body.data.shape_keys.animation_data_clear()
        for kb in body.data.shape_keys.key_blocks[1:]: kb.value = 0.0
    sm = body.modifiers.new('neutral', 'SMOOTH'); sm.vertex_group = 'head'; sm.factor = 0.6; sm.iterations = 6
    body.modifiers.new('sub', 'SUBSURF').levels = 1
else:
    mk = body.modifiers.new('nohead', 'MASK'); mk.vertex_group = 'head'; mk.invert_vertex_group = True
    body.modifiers.new('sub', 'SUBSURF').levels = 1

egg = None
if HEAD == 'egg':
    # 头骨 = 头部顶点上权重总和最大的骨骼顶点组
    arm = bpy.data.objects['track00']
    score = {}
    for i in head_idx:
        for g in body.data.vertices[i].groups:
            n = body.vertex_groups[g.group].name
            if n != 'head': score[n] = score.get(n, 0) + g.weight
    head_bone = max(score, key=score.get)

    sc.frame_set(f0); bpy.context.view_layer.update()
    eb = body.evaluated_get(bpy.context.evaluated_depsgraph_get())
    # 遮罩后求值网格没有头部顶点，所以用绑定姿势坐标 + 头骨当前变换来估计头部位置
    pb = arm.pose.bones[head_bone]
    bone_w = arm.matrix_world @ pb.matrix
    bone_rest = arm.matrix_world @ arm.data.bones[head_bone].matrix_local
    hc_rest = sum((body.matrix_world @ body.data.vertices[i].co for i in head_idx), Vector()) / len(head_idx)
    head_h = (ztop - cut)
    bpy.ops.mesh.primitive_uv_sphere_add(segments=48, ring_count=32, radius=1)
    egg = bpy.context.object
    for p in egg.data.polygons: p.use_smooth = True
    egg.data.materials.append(None)
    # 蛋形：上宽下窄，略向前倾
    for v in egg.data.vertices:
        t = v.co.z
        v.co.x *= 0.083 * (1.0 + 0.10 * t); v.co.y *= 0.098 * (1.0 + 0.06 * t); v.co.z *= 0.125
    # 先放在绑定姿势下的头部位置，再挂到头骨上：子物体会跟随骨骼从绑定姿势到当前姿势的变化
    egg.matrix_world = (bone_w @ bone_rest.inverted()).to_4x4() @ __import__('mathutils').Matrix.Translation(hc_rest + Vector((0, 0, 0.012)))
    mw = egg.matrix_world.copy()
    egg.parent = arm; egg.parent_type = 'BONE'; egg.parent_bone = head_bone
    bpy.context.view_layer.update(); egg.matrix_world = mw
    egg.modifiers.new('sub', 'SUBSURF').levels = 1
    print('HEAD bone', head_bone, 'verts', len(head_idx))

# ---------- 可选：胸部、臀部适度加丰满（仅白模展示用，不用于换人源视频） ----------
if CURVES > 0:
    import mathutils
    vs = body.data.vertices
    # 朝向：脚尖指向前方。比较脚底一层和脚踝一层的 Y 均值
    sole = [v.co for v in vs if v.co.z < zbot + 0.03 * hgt]
    ankle = [v.co for v in vs if zbot + 0.06 * hgt < v.co.z < zbot + 0.09 * hgt]
    fwd = 1 if sum(c.y for c in sole) / len(sole) > sum(c.y for c in ankle) / len(ankle) else -1
    xc = sum(c.x for c in co) / len(co)
    def sect(z0, z1):
        return [c for c in co if z0 < c.z < z1]
    chest = sect(zbot + 0.70 * hgt, zbot + 0.75 * hgt); hipz = sect(zbot + 0.48 * hgt, zbot + 0.53 * hgt)
    yc_chest = sum(c.y for c in chest) / len(chest); yc_hip = sum(c.y for c in hipz) / len(hipz)
    front = max(fwd * c.y for c in chest) * fwd; back = max(-fwd * c.y for c in hipz) * -fwd
    # (中心, 半径, 外推距离)：两侧胸部、两侧臀部，再加一点胯宽
    blobs = [((xc + sx * 0.085, (yc_chest + front) / 2, zbot + 0.725 * hgt), 0.075, 0.020) for sx in (-1, 1)]
    blobs += [((xc + sx * 0.075, (yc_hip + back) / 2, zbot + 0.50 * hgt), 0.095, 0.022) for sx in (-1, 1)]
    blobs += [((xc + sx * 0.15, yc_hip, zbot + 0.52 * hgt), 0.10, 0.010) for sx in (-1, 1)]
    if not body.data.shape_keys:
        body.shape_key_add(name='Basis', from_mix=False)
    sk = body.shape_key_add(name='curves', from_mix=False); sk.value = 1.0
    basis = body.data.shape_keys.reference_key.data
    for i, v in enumerate(vs):
        if not (zbot + 0.42 * hgt < v.co.z < zbot + 0.80 * hgt): continue
        d = 0.0
        for (cx_, cy_, cz_), r, amt in blobs:
            q = (v.co - mathutils.Vector((cx_, cy_, cz_))).length
            d += amt * math.exp(-(q / r) ** 2)
        if d > 1e-5:
            sk.data[i].co = basis[i].co + v.normal * d * CURVES
    print('CURVES', CURVES, 'fwd', fwd)

# ---------- 材质 ----------
def pmat(name, rgb, rough=0.6):
    m = bpy.data.materials.new(name); m.use_nodes = True
    b = m.node_tree.nodes['Principled BSDF']
    b.inputs['Base Color'].default_value = (*rgb, 1); b.inputs['Roughness'].default_value = rough
    return m
mq = pmat('mannequin', (0.82, 0.82, 0.83), 0.55)
body.data.materials.clear(); body.data.materials.append(mq)
if egg: egg.data.materials[0] = mq

# ---------- 无缝背景（地面 + 后墙，交界处倒圆角） ----------
bm = bmesh.new()
s = 30.0; back = cy + 4.0
v = [bm.verts.new(p) for p in [(cx - s, cy - s, 0), (cx + s, cy - s, 0), (cx + s, back, 0), (cx - s, back, 0),
                                  (cx + s, back, s), (cx - s, back, s)]]
bm.faces.new((v[0], v[1], v[2], v[3])); bm.faces.new((v[3], v[2], v[4], v[5]))
me = bpy.data.meshes.new('cyc'); bm.to_mesh(me); bm.free()
cyc = bpy.data.objects.new('cyc', me); sc.collection.objects.link(cyc)
bv = cyc.modifiers.new('bevel', 'BEVEL'); bv.width = 2.5; bv.segments = 24; bv.limit_method = 'ANGLE'
cyc.modifiers.new('sub', 'SUBSURF').levels = 1
for p in me.polygons: p.use_smooth = True
cyc.data.materials.append(pmat('backdrop', (0.30, 0.30, 0.31), 0.95))

if HUD:
    bpy.ops.mesh.primitive_grid_add(x_subdivisions=33, y_subdivisions=33, size=8, location=(cx, cy, 0.002))
    g = bpy.context.object; wf = g.modifiers.new('wf', 'WIREFRAME'); wf.thickness = 0.005
    g.data.materials.append(pmat('grid', (0.2, 0.2, 0.21), 0.9))
    bpy.ops.mesh.primitive_torus_add(major_radius=0.3, minor_radius=0.008, location=(0, 0, 0.006))
    ring = bpy.context.object; ring.data.materials.append(pmat('ring', (0.25, 0.7, 0.62), 0.5))
    for i, (x, y) in enumerate(track):
        ring.location = (x, y, 0.006); ring.keyframe_insert('location', frame=f0 + i)

# ---------- 灯光：大面积柔光主光 + 补光 + 轮廓光 ----------
def area(name, loc, power, size, color=(1, 1, 1)):
    l = bpy.data.lights.new(name, 'AREA'); l.energy = power; l.size = size; l.color = color
    o = bpy.data.objects.new(name, l); sc.collection.objects.link(o); o.location = loc
    t = o.constraints.new('TRACK_TO'); t.target = tgt; t.track_axis = 'TRACK_NEGATIVE_Z'; t.up_axis = 'UP_Y'
    return o
tgt = bpy.data.objects.new('tgt', None); sc.collection.objects.link(tgt); tgt.location = (cx, cy, 0.9)
area('key', (cx - 2.6, cy - 2.4, 3.4), 320, 3.5)
area('fill', (cx + 3.0, cy - 1.5, 1.8), 70, 4.0)
area('rim', (cx + 1.0, cy + 2.6, 3.0), 160, 2.0)
sc.world = bpy.data.worlds.new('w'); sc.world.use_nodes = True
sc.world.node_tree.nodes['Background'].inputs['Color'].default_value = (0.05, 0.05, 0.055, 1)

# ---------- 相机 ----------
bpy.ops.object.camera_add(); cam = bpy.context.object; sc.camera = cam
az = math.radians(CAM_AZ)
if SHOT == 'close':
    # 膝上跟拍：目标点跟随身体质心（重度平滑），相机跟着平移，脸占画面更大
    cam.data.lens = 50; dist = 2.0
    k = 9
    sm_t = [(sum(t[0] for t in track[max(0,i-k):i+k+1]) / len(track[max(0,i-k):i+k+1]),
             sum(t[1] for t in track[max(0,i-k):i+k+1]) / len(track[max(0,i-k):i+k+1])) for i in range(len(track))]
    ctgt = bpy.data.objects.new('ctgt', None); sc.collection.objects.link(ctgt)
    for i, (x, y) in enumerate(sm_t):
        ctgt.location = (x, y, 1.22); ctgt.keyframe_insert('location', frame=f0 + i)
        cam.location = (x + dist * math.sin(az), y - dist * math.cos(az), 1.42); cam.keyframe_insert('location', frame=f0 + i)
    c = cam.constraints.new('TRACK_TO'); c.target = ctgt; c.track_axis = 'TRACK_NEGATIVE_Z'; c.up_axis = 'UP_Y'
else:
    cam.data.lens = 50; dist = 3.6
    cam.location = (cx + dist * math.sin(az), cy - dist * math.cos(az), 1.35)
    c = cam.constraints.new('TRACK_TO'); c.target = tgt; c.track_axis = 'TRACK_NEGATIVE_Z'; c.up_axis = 'UP_Y'

# ---------- 渲染：EEVEE ----------
sc.render.engine = 'BLENDER_EEVEE'
ee = sc.eevee
for k, val in (('taa_render_samples', 64), ('use_shadows', True), ('use_raytracing', True)):
    if hasattr(ee, k): setattr(ee, k, val)
# 无显示器的 GPU 服务器上 EEVEE 可能拿不到 OpenGL/EGL 上下文：SHISEI_ENGINE=CYCLES 改用 Cycles + CUDA/OptiX
import os
if os.environ.get('SHISEI_ENGINE', '').upper() == 'CYCLES':
    sc.render.engine = 'CYCLES'
    prefs = bpy.context.preferences.addons['cycles'].preferences
    for dev in ('OPTIX', 'CUDA'):
        try:
            prefs.compute_device_type = dev; prefs.get_devices()
            if any(d.type == dev for d in prefs.devices):
                for d in prefs.devices: d.use = d.type == dev
                sc.cycles.device = 'GPU'; break
        except TypeError:
            continue
    sc.cycles.samples = 64; sc.cycles.use_denoising = True
sc.view_settings.view_transform = 'AgX'; sc.view_settings.look = 'AgX - Base Contrast' if 'AgX - Base Contrast' in [l.name for l in []] else 'None'
sc.render.resolution_x, sc.render.resolution_y = W, H
sc.render.fps = 24
sc.render.image_settings.file_format = 'PNG'
sc.render.filepath = OUT.rstrip('/') + '/f_'
if FR == 'all':
    sc.frame_start, sc.frame_end = f0, f1
else:
    a, b = FR.split('-'); sc.frame_start, sc.frame_end = int(a), int(b)
bpy.ops.render.render(animation=True)
