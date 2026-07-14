#!/usr/bin/env python3
"""
animpack_all_in_one.py — 光遇 .animpack 全功能独立转换脚本 (修正版)
================================================================
基于 libBootloader.so 完整逆向 + SkyModelViewer 验证。

核心格式修正:
  骨骼记录 = name(64) + matrix(64, 4x4行主序) + parent_index(4, 1-based)
  动画段无 tag/separator, SQT 直接跟在骨骼记录后
  头部: boneDefsFlag(0x48) + refSqtFlag(0x4C) + compression(0x50) + nameTableSize(0x51)

功能:
  交互模式:  python animpack_all_in_one.py
  摘要:      python animpack_all_in_one.py <file.animpack>
  JSON导出:  python animpack_all_in_one.py <file> --json out.json
  批量CSV:   python animpack_all_in_one.py batch <目录> <输出目录>
  glTF导出:  python animpack_all_in_one.py gltf <file.animpack> <out.gltf>
  glTF导入:  python animpack_all_in_one.py gltf_import <file.gltf> <out.animpack>
  骨骼树:    python animpack_all_in_one.py tree <file.animpack> <out.html>
  对比分析:  python animpack_all_in_one.py compare <目录> <out.json>
  全部导出:  python animpack_all_in_one.py all <目录> <输出目录>

依赖:
  pip install lz4
"""

import struct, json, sys, os, math, csv, base64
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any, Tuple

# ═══════════════════════════════════════════════════════════════
#  LZ4 依赖
# ═══════════════════════════════════════════════════════════════
try:
    import lz4.block as _lz4_block
    HAS_LZ4 = True
except ImportError:
    HAS_LZ4 = False
    _lz4_block = None

# ═══════════════════════════════════════════════════════════════
#  常量 (来自 libBootloader.so 逆向)
# ═══════════════════════════════════════════════════════════════
HEADER_SIZE = 80          # 文件头固定 80 字节 (0x00-0x4F)
NAME_FIELD_SIZE = 64      # 名称字段固定 64 字节
BONE_RECORD_SIZE = 132    # name(64) + matrix(64) + parent_index(4) = 132
SQT_DISK_SIZE = 40        # Scale(12) + Quat(16) + Translation(12)
VECTOR3_SIZE = 12
QUAT_SIZE = 16

# clip header: version>=10 = 6 u32 (24B), version<10 = 5 u32 (20B)
def _clip_header_u32_count(version):
    return 6 if version >= 10 else 5

def _clip_header_size(version):
    return _clip_header_u32_count(version) * 4

# FNV-1a 64-bit
FNV1A_OFFSET_BASIS = 0xcbf29ce484222325
FNV1A_PRIME = 0x100000001b3

# 关键帧通道位映射 (来自 .so 逆向 0xfd4f54)
#   bit3 = Scale (主缩放, float)       bit0 = Scale2 (副缩放, float)
#   bit4 = Quat  (主旋转, int16/float)  bit1 = Quat2  (副旋转, int16/float)
#   bit5 = Trans (主平移, int16/float)  bit2 = Trans2 (副平移, int16/float)
KF_FLAG_NAMES = {
    3: "Scale",  4: "Quat",  5: "Trans",
    0: "Scale2", 1: "Quat2", 2: "Trans2",
}

# ═══════════════════════════════════════════════════════════════
#  FNV-1a 哈希
# ═══════════════════════════════════════════════════════════════
def fnv1a_64(data: bytes) -> int:
    h = FNV1A_OFFSET_BASIS
    for b in data:
        h ^= b
        h = (h * FNV1A_PRIME) & 0xFFFFFFFFFFFFFFFF
    return h

# ═══════════════════════════════════════════════════════════════
#  数据结构
# ═══════════════════════════════════════════════════════════════
@dataclass
class SQT:
    scale: List[float]
    rotation: List[float]
    translation: List[float]
    @property
    def quaternion_length(self): return math.sqrt(sum(x*x for x in self.rotation))

@dataclass
class AnimSQT:
    scale: List[float]
    rotation: List[float]
    translation: List[float]
    @property
    def quaternion_length(self): return math.sqrt(sum(x*x for x in self.rotation))

@dataclass
class Bone:
    """骨骼记录 (132 字节: name(64) + matrix(64) + parent_index(4))"""
    index: int
    name: str
    matrix: List[float]          # 16 floats, 4x4 行主序 (InverseBindMatrix)
    parent_index: int            # 0-based, -1=根骨骼
    fnv1a_hash: int = 0
    @property
    def is_root(self): return self.parent_index < 0
    @property
    def translation(self): return [self.matrix[12], self.matrix[13], self.matrix[14]]

@dataclass
class KeyframeHeader:
    """关键帧头 (由 keyframe reader 0x00fd5c70 读取)"""
    field1: int                    # u32 — 起始帧索引
    field2: int                    # u32 — 结束帧索引
    flags: int                     # u32 — bit0=int16平移通道开关
    bbox_min: List[float]
    bbox_max: List[float]
    extra1: List[float]
    extra2: List[float]
    per_bone_flags: List[int]
    remaining_data: bytes = b""
    @property
    def frame_count(self): return self.field2 - self.field1 + 1 if self.field2 >= self.field1 else 0
    @property
    def has_keyframes(self): return any(f != 0 for f in self.per_bone_flags)
    @property
    def flag_summary(self):
        s = {}
        for bit, name in KF_FLAG_NAMES.items():
            c = sum(1 for f in self.per_bone_flags if f & (1 << bit))
            if c > 0: s[name] = c
        return s

@dataclass
class ClipData:
    header: List[int]
    sqt_list: List[AnimSQT]
    keyframe_header: Optional[KeyframeHeader] = None
    raw_bytes: bytes = b""

@dataclass
class AnimSegment:
    """动画段 (参考SQT + 压缩clip数据, 无 tag/separator)"""
    index: int
    sqt_list: List[AnimSQT]
    compressed_size: int
    decompressed_size: int
    compressed_data: bytes = b""
    clip_data: Optional[ClipData] = None
    decompression_error: str = ""

@dataclass
class AnimPack:
    """完整的 .animpack 解析结果"""
    version: int
    name: str
    bone_count: int
    bone_defs_flag: int = 0       # boneDefsFlag (>0=读取矩阵)
    ref_sqt_flag: int = 0         # refSqtFlag (>0=读取参考SQT)
    compression: int = 0          # 0=无, 1=LZ4+float, 2=LZ4+int16
    name_table_size: int = 0      # 名称表大小 (仅内存分配用)
    bones: List[Bone] = field(default_factory=list)
    segments: List[AnimSegment] = field(default_factory=list)
    anim_offset: int = 0
    file_size: int = 0
    file_path: str = ""
    format_version: str = ""

# ═══════════════════════════════════════════════════════════════
#  底层读取函数
# ═══════════════════════════════════════════════════════════════
def _read_u32(data, off): return struct.unpack_from('<I', data, off)[0]
def _read_f32(data, off): return struct.unpack_from('<f', data, off)[0]
def _read_u8(data, off): return data[off]
def _read_name(data, off, max_len=NAME_FIELD_SIZE):
    raw = data[off:off+max_len]
    nul = raw.find(b'\x00')
    if nul >= 0: raw = raw[:nul]
    return raw.decode('utf-8', errors='replace')
def _read_vector3(data, off):
    return [_read_f32(data,off), _read_f32(data,off+4), _read_f32(data,off+8)]
def _read_quat(data, off):
    return [_read_f32(data,off), _read_f32(data,off+4), _read_f32(data,off+8), _read_f32(data,off+12)]

# ═══════════════════════════════════════════════════════════════
#  解析函数
# ═══════════════════════════════════════════════════════════════
def _parse_anim_sqt(data, offset):
    return AnimSQT(
        scale=_read_vector3(data, offset),
        rotation=_read_quat(data, offset+12),
        translation=_read_vector3(data, offset+28),
    )

def _decompress_clip(compressed, decomp_size):
    if not HAS_LZ4:
        return b"", "lz4 模块未安装 (pip install lz4)"
    try:
        return _lz4_block.decompress(compressed, uncompressed_size=decomp_size), ""
    except Exception as e:
        return b"", str(e)

def _parse_keyframe_header(data, offset, bone_count, version):
    pos = offset
    f1 = _read_u32(data, pos); pos += 4
    f2 = _read_u32(data, pos); pos += 4
    fl = _read_u32(data, pos); pos += 4
    bbox_min = [0,0,0]; bbox_max = [0,0,0]; extra1 = [0,0,0]; extra2 = [0,0,0]
    if version > 8:
        bbox_min = _read_vector3(data, pos); pos += 12
        bbox_max = _read_vector3(data, pos); pos += 12
    if version >= 11:
        extra1 = _read_vector3(data, pos); pos += 12
        extra2 = _read_vector3(data, pos); pos += 12
    flags = list(data[pos:pos+bone_count]); pos += bone_count
    return KeyframeHeader(f1, f2, fl, bbox_min, bbox_max, extra1, extra2, flags, data[pos:])

def _parse_clip_data(decomp, bone_count, version):
    pos = 0
    u32_count = _clip_header_u32_count(version)
    header = [_read_u32(decomp, pos+i*4) for i in range(u32_count)]
    pos += u32_count * 4
    sqt_list = [_parse_anim_sqt(decomp, pos+i*SQT_DISK_SIZE) for i in range(bone_count)]
    pos += bone_count * SQT_DISK_SIZE
    kf = None
    if pos < len(decomp):
        kf = _parse_keyframe_header(decomp, pos, bone_count, version)
    return ClipData(header, sqt_list, kf, decomp)

def _parse_anim_segments(data, bone_count, version, ref_sqt_flag, bone_defs_flag, compression):
    """解析动画段 (无 tag/separator)。

    每段结构:
      if refSqtFlag > 0 and boneDefsFlag > 0:
        SQT(bone_count×40)  — 参考 SQT
      if compression > 0:
        u32 compressedSize + u32 decompressedSize + LZ4 data
      else:
        clip data 直接内嵌
    """
    segments = []
    offset = 0; seg_idx = 0
    while offset < len(data):
        sqt_list = []
        # 1. 参考 SQT (条件: refSqtFlag>0 且 boneDefsFlag>0)
        if ref_sqt_flag > 0 and bone_defs_flag > 0:
            sqt_end = offset + bone_count * SQT_DISK_SIZE
            if sqt_end > len(data): break
            sqt_list = [_parse_anim_sqt(data, offset + i*SQT_DISK_SIZE) for i in range(bone_count)]
            offset = sqt_end
        # 2. 压缩块
        if compression > 0:
            if offset + 8 > len(data): break
            total_size = _read_u32(data, offset)
            decomp_size = _read_u32(data, offset + 4)
            comp_start = offset + 8
            if total_size == 0 or comp_start + total_size > len(data): break
            comp_data = data[comp_start:comp_start + total_size]
            decomp, err = _decompress_clip(comp_data, decomp_size)
            cd = _parse_clip_data(decomp, bone_count, version) if not err else None
            segments.append(AnimSegment(seg_idx, sqt_list, total_size,
                                         decomp_size, comp_data, cd, err))
            seg_idx += 1
            offset = comp_start + total_size
        else:
            remaining = len(data) - offset
            if remaining < _clip_header_size(version) + bone_count * SQT_DISK_SIZE: break
            cd = _parse_clip_data(data[offset:], bone_count, version)
            segments.append(AnimSegment(seg_idx, sqt_list, len(data)-offset,
                                         len(data)-offset, b"", cd))
            break
    return segments

def parse_animpack(file_path):
    """解析 .animpack 文件, 返回 AnimPack 结构。"""
    with open(file_path, 'rb') as f:
        data = f.read()
    if len(data) < HEADER_SIZE:
        raise ValueError(f"文件过小 ({len(data)} < {HEADER_SIZE})")

    # ── 文件头 ──
    version = _read_u32(data, 0x00)
    name = _read_name(data, 0x04)
    bone_count = _read_u32(data, 0x44)
    bone_defs_flag = _read_u32(data, 0x48)   # boneDefsFlag
    ref_sqt_flag = _read_u32(data, 0x4c)     # refSqtFlag

    # ── 压缩信息 ──
    compression = _read_u8(data, 0x50)       # u8: 0/1/2
    name_table_size = 0
    if version >= 10:
        name_table_size = _read_u32(data, 0x51)

    # 骨骼区起始偏移
    bone_area_offset = 0x55 if version >= 10 else 0x50

    # ── 骨骼表: name(64) + matrix(16 floats) + parent_index(4) = 132 字节 ──
    bones = []
    for i in range(bone_count):
        base = bone_area_offset + i * BONE_RECORD_SIZE
        bname = _read_name(data, base)
        matrix = [_read_f32(data, base + 64 + j*4) for j in range(16)]
        parent_1based = _read_u32(data, base + 128)
        parent_idx = parent_1based - 1 if parent_1based > 0 else -1
        bones.append(Bone(i, bname, matrix, parent_idx,
                          fnv1a_64(bname.encode('utf-8'))))

    # ── 动画段 ──
    anim_off = bone_area_offset + bone_count * BONE_RECORD_SIZE
    segs = _parse_anim_segments(data[anim_off:], bone_count, version,
                                 ref_sqt_flag, bone_defs_flag, compression)
    fv = f"animPack{version}" if version in range(1,12) else "unknown"
    return AnimPack(version, name, bone_count, bone_defs_flag, ref_sqt_flag,
                    compression, name_table_size, bones, segs, anim_off,
                    len(data), file_path, fv)

# ═══════════════════════════════════════════════════════════════
#  摘要 / JSON 输出
# ═══════════════════════════════════════════════════════════════
def to_summary(ap):
    L = []; s = "─"*60
    L.append(s)
    L.append(f"  文件: {os.path.basename(ap.file_path)}")
    L.append(f"  大小: {ap.file_size:,} 字节 ({ap.file_size/1024:.1f} KB)")
    L.append(s)
    L.append(f"  名称: {ap.name}")
    L.append(f"  版本: {ap.version}")
    L.append(f"  骨骼数: {ap.bone_count}")
    L.append(f"  boneDefsFlag: {ap.bone_defs_flag}")
    L.append(f"  refSqtFlag: {ap.ref_sqt_flag}")
    L.append(f"  压缩: {ap.compression} ({'LZ4+int16' if ap.compression==2 else 'LZ4+float' if ap.compression==1 else '无'})")
    L.append(s)
    for seg in ap.segments:
        L.append(f"  ── 动画段 {seg.index} ──")
        L.append(f"    压缩前: {seg.decompressed_size:,} 字节 → 压缩后: {seg.compressed_size:,} 字节")
        if seg.decompression_error:
            L.append(f"    [错误] {seg.decompression_error}")
        elif seg.clip_data:
            cd = seg.clip_data
            L.append(f"    clip header: {cd.header}")
            L.append(f"    关键帧集数: {cd.header[0]}")
            if cd.keyframe_header:
                kf = cd.keyframe_header
                L.append(f"    起始帧: {kf.field1}")
                L.append(f"    结束帧: {kf.field2}")
                L.append(f"    总帧数: {kf.frame_count}")
                if kf.has_keyframes:
                    L.append(f"    动态关键帧: 有")
                    for n, c in kf.flag_summary.items():
                        L.append(f"      {n}: {c} 个骨骼")
                else:
                    L.append(f"    动态关键帧: 无 (静态绑定姿态)")
        L.append(s)
    L.append(f"  骨骼列表 (前10):")
    L.append(f"  {'#':>4}  {'骨骼名':<30}  {'Parent':>6}  {'位置 (X, Y, Z)'}")
    for idx in range(min(10, len(ap.bones))):
        b = ap.bones[idx]
        t = b.translation
        p = str(b.parent_index) if b.parent_index >= 0 else "ROOT"
        root = " *" if b.is_root else ""
        L.append(f"  {idx:4d}  {b.name:<30}  {p:>6}  ({t[0]:7.2f}, {t[1]:7.2f}, {t[2]:7.2f}){root}")
    L.append("  * = 根骨骼")
    L.append("")
    return "\n".join(L)

def to_json(ap, include_raw_hex=False):
    def _clean_sqt(sqt):
        return {"scale":[round(f,6) for f in sqt.scale],
                "rotation":[round(f,6) for f in sqt.rotation],
                "translation":[round(f,6) for f in sqt.translation]}
    bones_data = []
    for b in ap.bones:
        bd = {"index":b.index, "name":b.name, "is_root":b.is_root,
              "matrix":[round(f,6) for f in b.matrix],
              "parent_index":b.parent_index,
              "translation":[round(f,6) for f in b.translation]}
        bones_data.append(bd)
    segs_data = []
    for seg in ap.segments:
        sd = {"index":seg.index,
              "compressed_size":seg.compressed_size,
              "decompressed_size":seg.decompressed_size}
        if seg.decompression_error:
            sd["error"] = seg.decompression_error
        elif seg.clip_data:
            cd = seg.clip_data
            sd["clip_header"] = cd.header
            sd["keyframe_set_count"] = cd.header[0] if cd.header else 0
            sd["sqt_list"] = [_clean_sqt(s) for s in cd.sqt_list]
            if cd.keyframe_header:
                kf = cd.keyframe_header
                sd["keyframe"] = {
                    "start_frame":kf.field1, "end_frame":kf.field2, "flags":kf.flags,
                    "frame_count":kf.frame_count,
                    "has_keyframes":kf.has_keyframes,
                    "bbox_min":[round(f,6) for f in kf.bbox_min],
                    "bbox_max":[round(f,6) for f in kf.bbox_max],
                    "extra1":[round(f,6) for f in kf.extra1],
                    "extra2":[round(f,6) for f in kf.extra2],
                    "per_bone_flags":kf.per_bone_flags,
                    "flag_summary":kf.flag_summary if kf.has_keyframes else None,
                    "data_base64":base64.b64encode(kf.remaining_data).decode('ascii') if kf.remaining_data else None,
                }
        segs_data.append(sd)
    result = {"file":{"name":os.path.basename(ap.file_path),"size":ap.file_size},
              "header":{"version":ap.version,"name":ap.name,"bone_count":ap.bone_count,
                         "bone_defs_flag":ap.bone_defs_flag,"ref_sqt_flag":ap.ref_sqt_flag,
                         "compression":ap.compression,"name_table_size":ap.name_table_size},
              "bones":bones_data,
              "segments":segs_data}
    return json.dumps(result, indent=2, ensure_ascii=False)

# ═══════════════════════════════════════════════════════════════
#  矩阵运算
# ═══════════════════════════════════════════════════════════════
def quat_to_mat3(q):
    x,y,z,w = q; xx,yy,zz = x*x,y*y,z*z; xy,xz,yz = x*y,x*z,y*z
    wx,wy,wz = w*x,w*y,w*z
    return [[1-2*(yy+zz),2*(xy-wz),2*(xz+wy)],
            [2*(xy+wz),1-2*(xx+zz),2*(yz-wx)],
            [2*(xz-wy),2*(yz+wx),1-2*(xx+yy)]]

def compose_mat4(t,r,s):
    R = quat_to_mat3(r); sx,sy,sz = s
    return [[R[0][0]*sx,R[0][1]*sy,R[0][2]*sz,t[0]],
            [R[1][0]*sx,R[1][1]*sy,R[1][2]*sz,t[1]],
            [R[2][0]*sx,R[2][1]*sy,R[2][2]*sz,t[2]],
            [0,0,0,1]]

def multiply_mat4(a,b):
    return [[sum(a[i][k]*b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]

def invert_mat4(m):
    a,b,c,_=m[0]; d,e,f,_=m[1]; g,h,i,_=m[2]; tx,ty,tz=m[0][3],m[1][3],m[2][3]
    det = a*(e*i-f*h)-b*(d*i-f*g)+c*(d*h-e*g)
    if abs(det)<1e-12: return [[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]]
    inv=1.0/det
    ia=(e*i-f*h)*inv; ib=(c*h-b*i)*inv; ic=(b*f-c*e)*inv
    id_=(f*g-d*i)*inv; ie=(a*i-c*g)*inv; if_=(c*d-a*f)*inv
    ig=(d*h-e*g)*inv; ih=(b*g-a*h)*inv; ii=(a*e-b*d)*inv
    return [[ia,ib,ic,-(ia*tx+ib*ty+ic*tz)],
            [id_,ie,if_,-(id_*tx+ie*ty+if_*tz)],
            [ig,ih,ii,-(ig*tx+ih*ty+ii*tz)],
            [0,0,0,1]]

def mat4_to_bytes_col_major(m):
    """行优先 4x4 → glTF 列优先 bytes (16 floats)"""
    return struct.pack('<16f', *[m[j][i] for i in range(4) for j in range(4)])

def _matrix_to_gltf_ibm(matrix_flat):
    """行主序 16 floats → glTF 列优先 bytes (转置)"""
    m = matrix_flat
    col_major = [m[j*4+i] for i in range(4) for j in range(4)]
    return struct.pack('<16f', *col_major)

# ═══════════════════════════════════════════════════════════════
#  层级 (使用 bone.parent_index)
# ═══════════════════════════════════════════════════════════════
def get_parents(bones):
    """从骨骼记录获取父索引列表"""
    return [b.parent_index for b in bones]

def get_root_idx(bones):
    """获取根骨骼索引"""
    for i, b in enumerate(bones):
        if b.is_root:
            return i
    return 0

def build_children_map(parents):
    ch = {}
    for i, p in enumerate(parents):
        if p >= 0: ch.setdefault(p, []).append(i)
    return ch

def compute_world_transforms(bones, sqt_list, parents, root_idx):
    n = len(bones)
    local = [None]*n; world = [None]*n
    for i in range(n):
        if i < len(sqt_list) and sqt_list[i]:
            s = sqt_list[i]
            local[i] = compose_mat4(s.translation, s.rotation, s.scale)
        else:
            local[i] = [[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]]
    done = [False]*n; done[root_idx]=True; world[root_idx]=local[root_idx]
    for _ in range(n):
        for i in range(n):
            if not done[i] and parents[i]>=0 and done[parents[i]]:
                world[i]=multiply_mat4(world[parents[i]],local[i]); done[i]=True
    return world

def _get_bone_sqt_list(ap):
    """获取骨骼 SQT 列表 (参考SQT 或 clip中的SQT)"""
    if ap.segments and ap.segments[0].sqt_list:
        return ap.segments[0].sqt_list
    if ap.segments and ap.segments[0].clip_data and ap.segments[0].clip_data.sqt_list:
        return ap.segments[0].clip_data.sqt_list
    return [AnimSQT([1,1,1],[0,0,0,1],[0,0,0]) for _ in ap.bones]

# ═══════════════════════════════════════════════════════════════
#  1. 批量导出
# ═══════════════════════════════════════════════════════════════
def cmd_batch(input_dir, output_dir):
    os.makedirs(output_dir, exist_ok=True)
    csv_dir = os.path.join(output_dir,"csv"); json_dir = os.path.join(output_dir,"json")
    os.makedirs(csv_dir, exist_ok=True); os.makedirs(json_dir, exist_ok=True)
    files = sorted(f for f in os.listdir(input_dir) if f.endswith('.animpack'))
    if not files: print(f"  [错误] {input_dir} 下无 .animpack 文件"); return
    print(f"  找到 {len(files)} 个文件, 开始批量导出...")

    with open(os.path.join(csv_dir,"bones.csv"),'w',newline='',encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(["file","bone_index","name","is_root","parent_index",
                     "trans_x","trans_y","trans_z",
                     "sqt_scale_x","sqt_scale_y","sqt_scale_z",
                     "sqt_quat_x","sqt_quat_y","sqt_quat_z","sqt_quat_w",
                     "sqt_trans_x","sqt_trans_y","sqt_trans_z"])
        for fname in files:
            try: ap = parse_animpack(os.path.join(input_dir, fname))
            except Exception as e: print(f"    [跳过] {fname}: {e}"); continue
            sqt_list = _get_bone_sqt_list(ap)
            for b in ap.bones:
                sqt = sqt_list[b.index] if b.index < len(sqt_list) else None
                t = b.translation
                row = [fname,b.index,b.name,b.is_root,b.parent_index,
                       f"{t[0]:.6f}",f"{t[1]:.6f}",f"{t[2]:.6f}"]
                if sqt:
                    row += [f"{sqt.scale[0]:.6f}",f"{sqt.scale[1]:.6f}",f"{sqt.scale[2]:.6f}",
                            f"{sqt.rotation[0]:.6f}",f"{sqt.rotation[1]:.6f}",f"{sqt.rotation[2]:.6f}",f"{sqt.rotation[3]:.6f}",
                            f"{sqt.translation[0]:.6f}",f"{sqt.translation[1]:.6f}",f"{sqt.translation[2]:.6f}"]
                else: row += [""]*10
                w.writerow(row)
            print(f"    [OK] {fname}: {ap.bone_count} 骨骼")
    print(f"  骨骼数据 → {csv_dir}/bones.csv")

    with open(os.path.join(csv_dir,"segments.csv"),'w',newline='',encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(["file","seg_index","compressed_size","decompressed_size","compression_ratio",
                     "keyframe_set_count","start_frame","end_frame","frame_count","has_keyframes",
                     "flag_Scale","flag_Quat","flag_Trans","flag_Scale2","flag_Quat2","flag_Trans2"])
        for fname in files:
            try: ap = parse_animpack(os.path.join(input_dir, fname))
            except: continue
            for seg in ap.segments:
                cd = seg.clip_data; kf = cd.keyframe_header if cd else None
                ratio = seg.compressed_size/seg.decompressed_size*100 if seg.decompressed_size else 0
                flags = kf.flag_summary if (kf and kf.has_keyframes) else {}
                w.writerow([fname,seg.index,seg.compressed_size,seg.decompressed_size,f"{ratio:.1f}%",
                            cd.header[0] if cd else "",
                            kf.field1 if kf else "",kf.field2 if kf else "",kf.frame_count if kf else "",
                            kf.has_keyframes if kf else "",
                            flags.get("Scale",""),flags.get("Quat",""),flags.get("Trans",""),
                            flags.get("Scale2",""),flags.get("Quat2",""),flags.get("Trans2","")])
    print(f"  动画段数据 → {csv_dir}/segments.csv")

    with open(os.path.join(csv_dir,"summary.csv"),'w',newline='',encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(["file","file_size","name","bone_count","bone_defs_flag","ref_sqt_flag",
                     "compression","segment_count","total_compressed","total_decompressed"])
        for fname in files:
            try: ap = parse_animpack(os.path.join(input_dir, fname))
            except: continue
            tc = sum(s.compressed_size for s in ap.segments); td = sum(s.decompressed_size for s in ap.segments)
            w.writerow([fname,ap.file_size,ap.name,ap.bone_count,ap.bone_defs_flag,
                        ap.ref_sqt_flag,ap.compression,len(ap.segments),tc,td])
    print(f"  文件摘要 → {csv_dir}/summary.csv")

    for fname in files:
        try:
            ap = parse_animpack(os.path.join(input_dir, fname))
            jp = os.path.join(json_dir, fname.replace('.animpack','.json'))
            with open(jp,'w',encoding='utf-8') as f: f.write(to_json(ap))
        except Exception as e: print(f"    [JSON失败] {fname}: {e}")
    print(f"  JSON → {json_dir}/ ({len(files)} 个)")
    print(f"  批量导出完成!")

# ═══════════════════════════════════════════════════════════════
#  关键帧值解码
# ═══════════════════════════════════════════════════════════════
def _decode_i16_quat(data, pos):
    """int16四元数 (4×u16) → [x, y, z, w]"""
    r = struct.unpack_from('<4H', data, pos)
    return [(r[i] - 32768) / 32767.0 for i in range(4)]

def _decode_i16_trans(data, pos, extra1, extra2):
    """int16平移 (3×u16) 使用 extra1/extra2 解码 → [x, y, z]
    正确公式: extra1[i] + (u16[i] / 65535.0) * extra2[i]
    (extra2 是缩放因子, 不是范围上限)
    """
    r = struct.unpack_from('<3H', data, pos)
    return [extra1[i] + (r[i] / 65535.0) * extra2[i] for i in range(3)]

def _decode_kf_sets(ap):
    """解码所有关键帧集。

    每个关键帧集结构 (来自 .so 逆向 0xfd5c70):
    - 头部: field1(起始帧), field2(结束帧), flags, AABB, per_bone_flags
    - 主通道 (init: bit3/4/5): 读一次 → field1 帧的初始姿态
    - 副通道 (per-seg: bit0/1/2): 读 frame_count 次 → field1~field2 逐帧动画数据

    frame_count = field2 - field1 + 1

    通道读取顺序:
      主通道 (读一次): Scale(bit3) → Quat(bit4) → Trans(bit5)
      副通道 (每帧重复): Scale2(bit0) → Quat2(bit1) → Trans2(bit2)

    返回 list[dict], 每个含: field1, field2, frame_count, flags, extra1, extra2,
    per_bone_flags, values(各骨通道值), has_data
    values[bone_idx] = {
        'scale': [x,y,z],           # 主通道, frame=field1
        'quat': [x,y,z,w],          # 主通道, frame=field1
        'trans': [x,y,z],           # 主通道, frame=field1
        'scale2': [[x,y,z], ...],   # 副通道, frame_count 个值
        'quat2': [[x,y,z,w], ...],  # 副通道, frame_count 个值
        'trans2': [[x,y,z], ...],   # 副通道, frame_count 个值
    }
    """
    if not ap.segments or not ap.segments[0].clip_data:
        return []
    cd = ap.segments[0].clip_data
    decomp = cd.raw_bytes
    bc = ap.bone_count
    comp = ap.compression
    version = ap.version

    pos = _clip_header_size(version) + bc * SQT_DISK_SIZE
    remaining = decomp[pos:]
    num_sets = cd.header[0] if cd.header else 0
    if num_sets == 0:
        return []

    sets = []
    off = 0
    for _ in range(num_sets):
        # ── 读关键帧头 ──
        if off + 12 > len(remaining):
            break
        field1 = struct.unpack_from('<I', remaining, off)[0]; off += 4
        field2 = struct.unpack_from('<I', remaining, off)[0]; off += 4
        flags  = struct.unpack_from('<I', remaining, off)[0]; off += 4

        bbox_min = [0,0,0]; bbox_max = [0,0,0]; extra1 = [0,0,0]; extra2 = [0,0,0]
        if version > 8:
            if off + 24 > len(remaining): break
            bbox_min = list(struct.unpack_from('<3f', remaining, off)); off += 12
            bbox_max = list(struct.unpack_from('<3f', remaining, off)); off += 12
        if version >= 11:
            if off + 24 > len(remaining): break
            extra1 = list(struct.unpack_from('<3f', remaining, off)); off += 12
            extra2 = list(struct.unpack_from('<3f', remaining, off)); off += 12

        if off + bc > len(remaining):
            break
        per_bone_flags = list(remaining[off:off+bc]); off += bc

        frame_count = field2 - field1 + 1 if field2 >= field1 else 0

        quat_sz = 8 if comp == 2 else 16
        trans_sz = 6 if (comp == 2 and (flags & 1)) else 12
        scale_sz = 12

        scale_bones  = [i for i, f in enumerate(per_bone_flags) if f & (1 << 3)]
        quat_bones   = [i for i, f in enumerate(per_bone_flags) if f & (1 << 4)]
        trans_bones  = [i for i, f in enumerate(per_bone_flags) if f & (1 << 5)]
        scale2_bones = [i for i, f in enumerate(per_bone_flags) if f & (1 << 0)]
        quat2_bones  = [i for i, f in enumerate(per_bone_flags) if f & (1 << 1)]
        trans2_bones = [i for i, f in enumerate(per_bone_flags) if f & (1 << 2)]

        values = {}

        # ── 主通道 (init): 读一次 ──
        # Scale (bit3) — 始终 float
        for bi in scale_bones:
            if off + scale_sz > len(remaining): break
            values.setdefault(bi, {})['scale'] = list(struct.unpack_from('<3f', remaining, off)); off += scale_sz
        # Quat (bit4)
        for bi in quat_bones:
            if off + quat_sz > len(remaining): break
            if comp == 2:
                q = _decode_i16_quat(remaining, off)
            else:
                q = list(struct.unpack_from('<4f', remaining, off))
            mag = sum(c*c for c in q)
            if mag > 1e-10:
                inv = 1.0 / math.sqrt(mag); q = [c*inv for c in q]
            values.setdefault(bi, {})['quat'] = q; off += quat_sz
        # Trans (bit5)
        for bi in trans_bones:
            if off + trans_sz > len(remaining): break
            if comp == 2 and (flags & 1):
                values.setdefault(bi, {})['trans'] = _decode_i16_trans(remaining, off, extra1, extra2)
            else:
                values.setdefault(bi, {})['trans'] = list(struct.unpack_from('<3f', remaining, off))
            off += trans_sz

        # ── 副通道 (per-seg): 读 frame_count 次 ──
        for _fi in range(frame_count):
            # Scale2 (bit0) — 始终 float
            for bi in scale2_bones:
                if off + scale_sz > len(remaining): break
                val = list(struct.unpack_from('<3f', remaining, off)); off += scale_sz
                values.setdefault(bi, {}).setdefault('scale2', []).append(val)
            # Quat2 (bit1)
            for bi in quat2_bones:
                if off + quat_sz > len(remaining): break
                if comp == 2:
                    q = _decode_i16_quat(remaining, off)
                else:
                    q = list(struct.unpack_from('<4f', remaining, off))
                mag = sum(c*c for c in q)
                if mag > 1e-10:
                    inv = 1.0 / math.sqrt(mag); q = [c*inv for c in q]
                values.setdefault(bi, {}).setdefault('quat2', []).append(q); off += quat_sz
            # Trans2 (bit2)
            for bi in trans2_bones:
                if off + trans_sz > len(remaining): break
                if comp == 2 and (flags & 1):
                    val = _decode_i16_trans(remaining, off, extra1, extra2)
                else:
                    val = list(struct.unpack_from('<3f', remaining, off))
                values.setdefault(bi, {}).setdefault('trans2', []).append(val); off += trans_sz

        sets.append({
            'field1': field1, 'field2': field2, 'frame_count': frame_count,
            'flags': flags,
            'extra1': extra1, 'extra2': extra2,
            'per_bone_flags': per_bone_flags,
            'values': values,
            'has_data': bool(values),
            'scale_bones': scale_bones, 'quat_bones': quat_bones, 'trans_bones': trans_bones,
            'scale2_bones': scale2_bones, 'quat2_bones': quat2_bones, 'trans2_bones': trans2_bones,
        })
    return sets

def _build_gltf_animation(ap, kf_sets):
    """从关键帧集构建 glTF 动画数据。

    主通道 (init): 单个关键帧, 位于 field1 帧
    副通道 (per-seg): frame_count 个关键帧, 位于 field1, field1+1, ..., field2

    优化: 共享相同时间轴的通道使用同一个 input accessor。
    """
    fps = 30  # 默认帧率
    sqt_list = _get_bone_sqt_list(ap)

    # 收集有动画数据的骨骼
    animated_bones = set()
    for sd in kf_sets:
        animated_bones.update(sd['values'].keys())

    # 添加基础 SQT 作为初始关键帧 (frame 0)
    tracks = {}  # (bone_idx, prop) -> {frame: value}
    for bi in animated_bones:
        if bi < len(sqt_list):
            sqt = sqt_list[bi]
            tracks.setdefault((bi, 'rotation'), {})[0] = sqt.rotation
            tracks.setdefault((bi, 'translation'), {})[0] = sqt.translation
            tracks.setdefault((bi, 'scale'), {})[0] = sqt.scale

    # 添加关键帧集数据
    for sd in kf_sets:
        f1 = sd['field1']
        for bi, props in sd['values'].items():
            # 主通道 (init): 单帧, 位于 field1
            if 'quat' in props:
                tracks.setdefault((bi, 'rotation'), {})[f1] = props['quat']
            if 'trans' in props:
                tracks.setdefault((bi, 'translation'), {})[f1] = props['trans']
            if 'scale' in props:
                tracks.setdefault((bi, 'scale'), {})[f1] = props['scale']
            # 副通道 (per-seg): 逐帧, 位于 field1, field1+1, ..., field2
            if 'quat2' in props:
                for fi, val in enumerate(props['quat2']):
                    tracks.setdefault((bi, 'rotation'), {})[f1 + fi] = val
            if 'trans2' in props:
                for fi, val in enumerate(props['trans2']):
                    tracks.setdefault((bi, 'translation'), {})[f1 + fi] = val
            if 'scale2' in props:
                for fi, val in enumerate(props['scale2']):
                    tracks.setdefault((bi, 'scale'), {})[f1 + fi] = val

    if not tracks:
        return None

    anim_buf = bytearray()
    bvs = []; accs = []; samplers = []; channels = []
    acc_idx = 0

    # 缓存时间轴 accessor (frames_tuple -> accessor_idx)
    time_cache = {}

    for (bi, prop), frame_vals in tracks.items():
        frames = sorted(frame_vals.keys())
        n = len(frames)
        if n == 0: continue
        times = [f / fps for f in frames]
        t_min, t_max = times[0], times[-1]

        # 归一化四元数
        vals = [frame_vals[f] for f in frames]
        if prop == 'rotation':
            for qi, q in enumerate(vals):
                mag = sum(c*c for c in q)
                if mag > 1e-10:
                    inv = 1.0 / math.sqrt(mag)
                    vals[qi] = [c*inv for c in q]
                else:
                    vals[qi] = [0.0, 0.0, 0.0, 1.0]

        # 共享时间轴 accessor
        frames_key = tuple(frames)
        if frames_key in time_cache:
            t_acc = time_cache[frames_key]
        else:
            tb = struct.pack(f'{n}f', *times)
            tbv = len(bvs)
            bvs.append({"buffer": 1, "byteOffset": len(anim_buf), "byteLength": len(tb)})
            anim_buf.extend(tb)
            t_acc = acc_idx
            accs.append({"bufferView": tbv, "componentType": 5126, "count": n,
                          "type": "SCALAR", "min": [t_min], "max": [t_max]})
            acc_idx += 1
            time_cache[frames_key] = t_acc

        # 输出 accessor
        if prop == 'rotation':
            vb = struct.pack(f'{n*4}f', *[c for v in vals for c in v])
            vtype = "VEC4"
            flat = [c for v in vals for c in v]
            vmin = [min(flat[i::4]) for i in range(4)]
            vmax = [max(flat[i::4]) for i in range(4)]
        else:
            vb = struct.pack(f'{n*3}f', *[c for v in vals for c in v])
            vtype = "VEC3"
            flat = [c for v in vals for c in v]
            vmin = [min(flat[i::3]) for i in range(3)]
            vmax = [max(flat[i::3]) for i in range(3)]
        vbv = len(bvs)
        bvs.append({"buffer": 1, "byteOffset": len(anim_buf), "byteLength": len(vb)})
        anim_buf.extend(vb)
        v_acc = acc_idx
        accs.append({"bufferView": vbv, "componentType": 5126, "count": n,
                      "type": vtype, "min": vmin, "max": vmax})
        acc_idx += 1
        si = len(samplers)
        samplers.append({"input": t_acc, "output": v_acc, "interpolation": "LINEAR"})
        channels.append({"sampler": si, "target": {"node": bi, "path": prop}})

    anim_b64 = base64.b64encode(bytes(anim_buf)).decode('ascii')
    return {
        "accessors": accs,
        "bufferViews": bvs,
        "buffer": {"uri": f"data:application/octet-stream;base64,{anim_b64}",
                    "byteLength": len(anim_buf)},
        "animation": {"name": f"{ap.name}_anim", "samplers": samplers, "channels": channels},
    }

def _add_visual_mesh(gltf, nodes, ap, parents, root_idx, world_mats):
    """添加骨骼连线可视化 (不修改骨骼节点, 创建独立 mesh 节点)。

    关键: 不给骨骼节点添加 mesh 属性, 否则建模软件会将骨骼当作网格对象,
    导致动画无法正确导入。
    """
    root_inv = invert_mat4(world_mats[root_idx])
    bone_pos = []
    for i in range(len(ap.bones)):
        wp = [world_mats[i][0][3], world_mats[i][1][3], world_mats[i][2][3]]
        lp = [
            root_inv[0][0]*wp[0]+root_inv[0][1]*wp[1]+root_inv[0][2]*wp[2]+root_inv[0][3],
            root_inv[1][0]*wp[0]+root_inv[1][1]*wp[1]+root_inv[1][2]*wp[2]+root_inv[1][3],
            root_inv[2][0]*wp[0]+root_inv[2][1]*wp[1]+root_inv[2][2]*wp[2]+root_inv[2][3],
        ]
        bone_pos.append(lp)
    line_v = []
    for i in range(len(ap.bones)):
        if i == root_idx or parents[i] < 0: continue
        p = parents[i]
        line_v.extend(bone_pos[i])
        line_v.extend(bone_pos[p])
    if not line_v:
        return
    line_vb = struct.pack(f'{len(line_v)}f', *line_v)
    mesh_b64 = base64.b64encode(line_vb).decode('ascii')
    buf_idx = len(gltf["buffers"])
    bv0 = len(gltf["bufferViews"])
    acc0 = len(gltf["accessors"])
    gltf["bufferViews"].append({"buffer": buf_idx, "byteOffset": 0, "byteLength": len(line_vb)})
    nv = len(line_v) // 3
    xs = line_v[0::3]; ys = line_v[1::3]; zs = line_v[2::3]
    gltf["accessors"].append({"bufferView": bv0, "componentType": 5126, "count": nv,
        "type": "VEC3", "min": [min(xs),min(ys),min(zs)], "max": [max(xs),max(ys),max(zs)]})
    gltf["materials"] = [
        {"name":"bone_line","pbrMetallicRoughness":{"baseColorFactor":[0.6,0.7,0.9,1.0],"metallicFactor":0.0,"roughnessFactor":0.5}},
    ]
    gltf["meshes"] = [{"name":"bone_lines","primitives":[
        {"attributes":{"POSITION":acc0},"material":0,"mode":1}]}]
    # 创建独立的可视化节点 (不是骨骼节点, 不参与 skin)
    vis_node = len(nodes)
    nodes.append({"name":"_bone_visual","mesh":0})
    if "children" not in nodes[root_idx]:
        nodes[root_idx]["children"] = []
    nodes[root_idx]["children"].append(vis_node)
    gltf["buffers"].append({"uri":f"data:application/octet-stream;base64,{mesh_b64}","byteLength":len(line_vb)})

# ═══════════════════════════════════════════════════════════════
#  2. glTF 导出
# ═══════════════════════════════════════════════════════════════
def cmd_gltf(file_path, output_path):
    ap = parse_animpack(file_path)
    sqt_list = _get_bone_sqt_list(ap)
    parents = get_parents(ap.bones)
    root_idx = get_root_idx(ap.bones)
    children_map = build_children_map(parents)
    world_mats = compute_world_transforms(ap.bones, sqt_list, parents, root_idx)

    # IBM = inverse(world_from_SQT)，确保与节点变换一致
    # 不使用 bone.matrix (那是绑定姿态 IBM，与 SQT 动画基础姿态不同)
    ibm_bytes = b""
    for i in range(len(ap.bones)):
        inv_world = invert_mat4(world_mats[i])
        ibm_bytes += mat4_to_bytes_col_major(inv_world)
    ibm_b64 = base64.b64encode(ibm_bytes).decode('ascii')

    # 构建节点 (使用 SQT 作为局部变换, parent_index 构建层级)
    nodes = []
    for i, b in enumerate(ap.bones):
        sqt = sqt_list[i] if i < len(sqt_list) else None
        node = {"name": b.name}
        if sqt:
            if sqt.translation != [0,0,0]: node["translation"] = [round(v,8) for v in sqt.translation]
            if sqt.rotation != [0,0,0,1]: node["rotation"] = [round(v,8) for v in sqt.rotation]
            if sqt.scale != [1,1,1]: node["scale"] = [round(v,8) for v in sqt.scale]
        if i in children_map: node["children"] = sorted(children_map[i])
        nodes.append(node)

    bc = len(ap.bones)
    gltf = {
        "asset":{"version":"2.0","generator":"animpack_all_in_one.py","copyright":f"Sky: CotL — {ap.name}"},
        "scene":0,"scenes":[{"nodes":[root_idx],"name":ap.name}],
        "nodes":nodes,
        "skins":[{"name":f"{ap.name}_skeleton","joints":list(range(bc)),"inverseBindMatrices":0,"skeleton":root_idx}],
        "accessors":[{"bufferView":0,"componentType":5126,"count":bc,"type":"MAT4"}],
        "bufferViews":[{"buffer":0,"byteOffset":0,"byteLength":bc*64}],
        "buffers":[{"uri":f"data:application/octet-stream;base64,{ibm_b64}","byteLength":bc*64}],
    }
    kf_sets = _decode_kf_sets(ap)
    anim_data = _build_gltf_animation(ap, kf_sets)
    if anim_data:
        next_acc = len(gltf["accessors"])
        next_bv = len(gltf["bufferViews"])
        anim_buf_idx = len(gltf["buffers"])
        for bv in anim_data["bufferViews"]:
            bv["buffer"] = anim_buf_idx
        for acc in anim_data["accessors"]:
            acc["bufferView"] += next_bv
        gltf["accessors"].extend(anim_data["accessors"])
        gltf["bufferViews"].extend(anim_data["bufferViews"])
        gltf["buffers"].append(anim_data["buffer"])
        for s in anim_data["animation"]["samplers"]:
            s["input"] += next_acc
            s["output"] += next_acc
        gltf["animations"] = [anim_data["animation"]]
        n_ch = len(anim_data["animation"]["channels"])
        n_sets = len(kf_sets)
        print(f"    动画: {n_ch} 通道, {n_sets} 关键帧集, {anim_data['buffer']['byteLength']} 字节")
    else:
        print(f"    动画: 无关键帧 (纯静态)")
    # 不添加静态骨骼可视化 mesh — 静态 mesh 不跟随动画，会导致"线不动"问题
    # 建模软件会原生显示骨骼层级 (armature)
    with open(output_path,'w',encoding='utf-8') as f: json.dump(gltf,f,indent=2,ensure_ascii=False)
    print(f"  glTF 已导出: {output_path}")
    print(f"    骨骼数: {bc}  根骨骼: {ap.bones[root_idx].name}")
    print(f"    逆绑定矩阵: {bc*64} 字节 (base64 内嵌)")

# ═══════════════════════════════════════════════════════════════
#  2b. glTF 导入 (glTF → .animpack)
# ═══════════════════════════════════════════════════════════════
def _gltf_ibm_to_matrix(ibm_floats):
    """glTF 列优先 16 floats → 行主序 list"""
    cm = ibm_floats
    return [cm[i*4+j] for j in range(4) for i in range(4)]

def _mat4_to_sqt(m):
    """从 4x4 局部变换矩阵提取 Scale, Quaternion, Translation"""
    tx, ty, tz = m[0][3], m[1][3], m[2][3]
    sx = math.sqrt(m[0][0]**2 + m[1][0]**2 + m[2][0]**2)
    sy = math.sqrt(m[0][1]**2 + m[1][1]**2 + m[2][1]**2)
    sz = math.sqrt(m[0][2]**2 + m[1][2]**2 + m[2][2]**2)
    if sx == 0: sx = 1e-8
    if sy == 0: sy = 1e-8
    if sz == 0: sz = 1e-8
    r00 = m[0][0]/sx; r10 = m[1][0]/sx; r20 = m[2][0]/sx
    r01 = m[0][1]/sy; r11 = m[1][1]/sy; r21 = m[2][1]/sy
    r02 = m[0][2]/sz; r12 = m[1][2]/sz; r22 = m[2][2]/sz
    trace = r00 + r11 + r22
    if trace > 0:
        s = math.sqrt(trace + 1.0) * 2
        qw = 0.25 * s; qx = (r21 - r12) / s; qy = (r02 - r20) / s; qz = (r10 - r01) / s
    elif r00 > r11 and r00 > r22:
        s = math.sqrt(1.0 + r00 - r11 - r22) * 2
        qw = (r21 - r12) / s; qx = 0.25 * s; qy = (r01 + r10) / s; qz = (r02 + r20) / s
    elif r11 > r22:
        s = math.sqrt(1.0 + r11 - r00 - r22) * 2
        qw = (r02 - r20) / s; qx = (r01 + r10) / s; qy = 0.25 * s; qz = (r12 + r21) / s
    else:
        s = math.sqrt(1.0 + r22 - r00 - r11) * 2
        qw = (r10 - r01) / s; qx = (r02 + r20) / s; qy = (r12 + r21) / s; qz = 0.25 * s
    ql = math.sqrt(qx*qx + qy*qy + qz*qz + qw*qw)
    if ql > 0: qx /= ql; qy /= ql; qz /= ql; qw /= ql
    return [sx, sy, sz], [qx, qy, qz, qw], [tx, ty, tz]

def cmd_gltf_import(gltf_path, output_path):
    """从 glTF 2.0 文件重建 .animpack"""
    if not HAS_LZ4:
        print("  [错误] pip install lz4"); return
    with open(gltf_path, 'r', encoding='utf-8') as f:
        gltf = json.load(f)

    nodes = gltf.get("nodes", [])
    if not nodes:
        print("  [错误] glTF 中无节点"); return

    scene_idx = gltf.get("scene", 0)
    scenes = gltf.get("scenes", [])
    root_idx = scenes[scene_idx]["nodes"][0] if scenes else 0

    # 构建父索引
    bone_parents = [-1] * len(nodes)
    for i, node in enumerate(nodes):
        for child in node.get("children", []):
            if child < len(bone_parents):
                bone_parents[child] = i

    # 获取 IBM
    ibm_data = None
    if gltf.get("skins"):
        skin = gltf["skins"][0]
        acc_idx = skin.get("inverseBindMatrices")
        if acc_idx is not None and acc_idx < len(gltf.get("accessors", [])):
            acc = gltf["accessors"][acc_idx]
            bv_idx = acc["bufferView"]
            bv = gltf["bufferViews"][bv_idx]
            buf = gltf["buffers"][bv["buffer"]]
            uri = buf.get("uri", "")
            if uri.startswith("data:"):
                b64 = uri.split(",", 1)[1]
                raw = base64.b64decode(b64)
                offset = bv.get("byteOffset", 0)
                length = bv["byteLength"]
                ibm_data = raw[offset:offset+length]

    # 解析节点局部变换 → SQT, 构建 Bone
    bone_count = len(nodes)
    sqt_list = []
    bones = []
    for i, node in enumerate(nodes):
        name = node.get("name", f"bone_{i}")
        if "translation" in node or "rotation" in node or "scale" in node:
            t = node.get("translation", [0, 0, 0])
            r = node.get("rotation", [0, 0, 0, 1])
            s = node.get("scale", [1, 1, 1])
        elif "matrix" in node:
            m_flat = node["matrix"]
            m = [[m_flat[c*4+r] for c in range(4)] for r in range(4)]
            s, r, t = _mat4_to_sqt(m)
        else:
            t = [0, 0, 0]; r = [0, 0, 0, 1]; s = [1, 1, 1]
        sqt_list.append(AnimSQT(scale=list(s), rotation=list(r), translation=list(t)))

        # 从 IBM 获取矩阵, 或使用单位矩阵
        if ibm_data and i * 64 + 64 <= len(ibm_data):
            ibm_floats = list(struct.unpack_from('<16f', ibm_data, i * 64))
            matrix = _gltf_ibm_to_matrix(ibm_floats)
        else:
            matrix = [1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1]

        bones.append(Bone(index=i, name=name, matrix=matrix, parent_index=bone_parents[i]))

    asset = gltf.get("asset", {})
    name = scenes[scene_idx].get("name", asset.get("copyright", "imported").replace("Sky: CotL — ", "")) if scenes else "imported"

    ap = AnimPack(
        version=11, name=name,
        bone_count=bone_count,
        bone_defs_flag=1, ref_sqt_flag=1,
        compression=2,
    )
    ap.bones = bones

    # 构建动画段
    file_sqt_list = [AnimSQT(scale=s.scale, rotation=s.rotation, translation=s.translation) for s in sqt_list]
    clip_header = [1, 0, 0, 0, 0, 0]
    kf_header = KeyframeHeader(
        field1=0, field2=0, flags=0,
        bbox_min=[0, 0, 0], bbox_max=[0, 0, 0],
        extra1=[-15, -15, -15], extra2=[30, 30, 30],
        per_bone_flags=[0] * bone_count,
        remaining_data=b"",
    )
    cd = ClipData(header=clip_header, sqt_list=sqt_list, keyframe_header=kf_header)
    seg = AnimSegment(index=0, sqt_list=file_sqt_list,
                      compressed_size=0, decompressed_size=0, clip_data=cd)
    ap.segments = [seg]

    size = pack_animpack(ap, output_path, use_raw=False)

    # 验证
    ap2 = parse_animpack(output_path)
    ok = ap2.bone_count == bone_count
    names_ok = all(b1.name == b2.name for b1, b2 in zip(bones, ap2.bones)) if ok else False

    print(f"  [glTF → animpack] 已转换: {output_path} ({size:,} 字节)")
    print(f"    名称: {name}  骨骼: {bone_count}  根骨骼: {bones[root_idx].name}")
    print(f"    验证: {'通过' if ok and names_ok else '失败'}")

# ═══════════════════════════════════════════════════════════════
#  3. 骨骼树 HTML
# ═══════════════════════════════════════════════════════════════
def _bone_color(name):
    if name == "M_hip" or name.endswith(":M_hip"): return "root"
    if "spine" in name or "chest" in name or "neck" in name or name.endswith("M_head"): return "spine"
    if "L_" in name: return "limb-l"
    if "R_" in name: return "limb-r"
    if "AUX" in name: return "aux"
    if "Wing" in name: return "wing"
    if "hair" in name.lower(): return "hair"
    return "other"

def cmd_tree(file_path, output_path):
    ap = parse_animpack(file_path)
    sqt_list = _get_bone_sqt_list(ap)
    parents = get_parents(ap.bones)
    root_idx = get_root_idx(ap.bones)
    children_map = build_children_map(parents)

    def render_node(idx):
        b = ap.bones[idx]; sqt = sqt_list[idx] if idx < len(sqt_list) else None
        cc = _bone_color(b.name); ch = children_map.get(idx, [])
        if sqt: s,q,t = sqt.scale, sqt.rotation, sqt.translation; ql = sqt.quaternion_length
        else: s,q,t = [1,1,1],[0,0,0,1],[0,0,0]; ql = 1.0
        p_str = str(b.parent_index) if b.parent_index >= 0 else "ROOT"
        detail = (f"<table class='dt'><tr><th>索引</th><td>{idx}</td><th>类型</th><td>{'根骨骼' if b.is_root else '普通骨骼'}</td></tr>"
                  f"<tr><th>名称</th><td colspan='3'>{b.name}</td></tr>"
                  f"<tr><th>父骨骼</th><td colspan='3'>{p_str}</td></tr>"
                  f"<tr><th>缩放</th><td colspan='3'>[{s[0]:.4f}, {s[1]:.4f}, {s[2]:.4f}]</td></tr>"
                  f"<tr><th>旋转</th><td colspan='3'>[{q[0]:.4f}, {q[1]:.4f}, {q[2]:.4f}, {q[3]:.4f}]  |Q|={ql:.4f}</td></tr>"
                  f"<tr><th>位移</th><td colspan='3'>[{t[0]:.4f}, {t[1]:.4f}, {t[2]:.4f}]</td></tr></table>")
        h = f"<div class='tn' data-idx='{idx}' data-detail='{detail}' onclick='sd({idx})'>"
        h += f"<span class='bt {cc}'>{'▼' if ch else '•'} {b.name} <span class='bi'>#{idx}</span></span>"
        if ch:
            h += "<div class='tc'>" + "".join(render_node(c) for c in sorted(ch)) + "</div>"
        return h + "</div>"

    counts = {}
    for b in ap.bones:
        c = _bone_color(b.name); counts[c] = counts.get(c,0)+1
    legend = " ".join(f"<span class='li {c}'>■ {c}({n})</span>" for c,n in sorted(counts.items()))

    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{ap.name} — 骨骼层级</title><style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:'Segoe UI','Noto Sans CJK SC',sans-serif;background:#0d1117;color:#c9d1d9}}
.hdr{{background:#161b22;padding:20px 30px;border-bottom:1px solid #30363d}}
.hdr h1{{font-size:20px;color:#58a6ff;margin-bottom:8px}}
.hdr .i{{font-size:13px;color:#8b949e}}.hdr .i span{{margin-right:20px}}
.lg{{padding:10px 30px;background:#161b22;border-bottom:1px solid #30363d;font-size:12px}}.li{{margin-right:15px}}
.ctn{{display:flex;height:calc(100vh - 120px)}}
.tp{{flex:1;overflow:auto;padding:20px 30px}}
.dp{{width:400px;background:#161b22;border-left:1px solid #30363d;padding:20px;overflow:auto}}
.dp h2{{font-size:16px;color:#58a6ff;margin-bottom:15px}}
.tn{{cursor:pointer;padding:3px 0}}.tn:hover .bt{{background:#21262d}}
.bt{{display:inline-block;padding:2px 8px;border-radius:4px;font-size:13px;white-space:nowrap}}
.bi{{font-size:11px;color:#6e7681}}.tc{{margin-left:20px;border-left:1px solid #30363d;padding-left:8px}}
.root{{color:#f85149}}.spine{{color:#3fb950}}.limb-l{{color:#58a6ff}}.limb-r{{color:#bc8cff}}
.aux{{color:#6e7681}}.wing{{color:#d29922}}.hair{{color:#f778ba}}.other{{color:#c9d1d9}}
.li.root{{color:#f85149}}.li.spine{{color:#3fb950}}.li.limb-l{{color:#58a6ff}}.li.limb-r{{color:#bc8cff}}
.li.aux{{color:#6e7681}}.li.wing{{color:#d29922}}.li.hair{{color:#f778ba}}
.dt{{width:100%;border-collapse:collapse;font-size:13px}}
.dt th{{text-align:right;color:#8b949e;padding:4px 8px;border-bottom:1px solid #21262d;width:70px}}
.dt td{{padding:4px 8px;border-bottom:1px solid #21262d;color:#c9d1d9}}.dt td[colspan]{{color:#79c0ff}}
#dc{{color:#c9d1d9}}#dc:empty::before{{content:'点击骨骼节点查看详情';color:#6e7681}}
</style></head><body>
<div class="hdr"><h1>{ap.name}</h1><div class="i">
<span>骨骼: {ap.bone_count}</span><span>动画段: {len(ap.segments)}</span>
<span>压缩: type {ap.compression}</span><span>版本: {ap.version}</span>
<span>文件: {os.path.basename(ap.file_path)} ({ap.file_size:,}B)</span></div></div>
<div class="lg">{legend}</div>
<div class="ctn"><div class="tp">{render_node(root_idx)}</div>
<div class="dp"><h2>骨骼详情</h2><div id="dc"></div></div></div>
<script>const D={{}};
document.querySelectorAll('.tn').forEach(n=>{{D[n.dataset.idx]=n.dataset.detail}});
function sd(idx){{document.getElementById('dc').innerHTML=D[idx]||'无数据'}}
</script></body></html>"""
    with open(output_path,'w',encoding='utf-8') as f: f.write(html)
    print(f"  HTML 骨骼树已导出: {output_path}")
    print(f"    骨骼数: {ap.bone_count}  根骨骼: {ap.bones[root_idx].name}")

# ═══════════════════════════════════════════════════════════════
#  4. 对比分析
# ═══════════════════════════════════════════════════════════════
def cmd_compare(input_dir, output_path):
    files = sorted(f for f in os.listdir(input_dir) if f.endswith('.animpack'))
    if not files: print(f"  [错误] 无 .animpack 文件"); return
    results = []
    for fname in files:
        try: ap = parse_animpack(os.path.join(input_dir, fname))
        except: continue
        bone_names = {b.name for b in ap.bones}
        sqt_list = _get_bone_sqt_list(ap)
        seg = ap.segments[0] if ap.segments else None
        cd = seg.clip_data if seg else None
        kf = cd.keyframe_header if cd else None
        results.append({"file":fname,"file_size":ap.file_size,"name":ap.name,
            "bone_count":ap.bone_count,"compression":ap.compression,
            "bone_defs_flag":ap.bone_defs_flag,"ref_sqt_flag":ap.ref_sqt_flag,
            "segment_count":len(ap.segments),
            "compressed_size":seg.compressed_size if seg else 0,
            "decompressed_size":seg.decompressed_size if seg else 0,
            "keyframe_set_count":cd.header[0] if cd else 0,
            "has_keyframes":kf.has_keyframes if kf else False,
            "start_frame":kf.field1 if kf else 0,
            "end_frame":kf.field2 if kf else 0,
            "frame_count":kf.frame_count if kf else 0,
            "bone_names":sorted(bone_names),
            "sqt_sample":[{"name":ap.bones[i].name,"scale":[round(v,6) for v in sqt_list[i].scale],
                           "rotation":[round(v,6) for v in sqt_list[i].rotation],
                           "translation":[round(v,6) for v in sqt_list[i].translation]}
                          for i in range(min(3,len(sqt_list)))] if sqt_list else []})
    bone_comp = {}
    if len(results) >= 2:
        all_names = set()
        for r in results: all_names.update(r["bone_names"])
        shared = all_names.copy()
        for r in results: shared &= set(r["bone_names"])
        bone_comp = {"total_unique_bones":len(all_names),"shared_bones":len(shared),
            "per_file":{r["file"]:{"bone_count":len(r["bone_names"]),
                         "unique_bones":sorted(set(r["bone_names"])-shared)[:10]} for r in results}}
    report = {"summary":{"file_count":len(results),"files":[r["file"] for r in results]},
              "file_comparison":results,"bone_comparison":bone_comp}
    with open(output_path,'w',encoding='utf-8') as f: json.dump(report,f,indent=2,ensure_ascii=False)
    print(f"  对比报告已导出: {output_path}")
    print(f"  {'─'*60}")
    print(f"  {'文件':<40} {'骨骼':>5} {'段数':>4} {'大小':>8}")
    print(f"  {'─'*60}")
    for r in results:
        print(f"  {r['file']:<40} {r['bone_count']:>5} {r['segment_count']:>4} {r['file_size']:>7}B")
    if bone_comp:
        print(f"  {'─'*60}")
        print(f"  唯一骨骼: {bone_comp['total_unique_bones']}  共享: {bone_comp['shared_bones']}")

# ═══════════════════════════════════════════════════════════════
#  5. 反向打包 (.animpack 重建)
# ═══════════════════════════════════════════════════════════════
def pack_animpack(ap, output_path, use_raw=True):
    """将 AnimPack 对象打包回 .animpack 二进制文件。"""
    if not HAS_LZ4:
        raise RuntimeError("lz4 模块未安装 (pip install lz4)")

    data = bytearray()

    # ── 文件头 ──
    data += struct.pack('<I', ap.version)                         # 0x00: version
    name_b = ap.name.encode('utf-8')[:64]
    data += name_b + b'\x00' * (64 - len(name_b))                # 0x04: name[64]
    data += struct.pack('<I', ap.bone_count)                      # 0x44: boneCount
    data += struct.pack('<I', ap.bone_defs_flag)                  # 0x48: boneDefsFlag
    data += struct.pack('<I', ap.ref_sqt_flag)                    # 0x4C: refSqtFlag
    assert len(data) == HEADER_SIZE, f"头大小错误: {len(data)} != {HEADER_SIZE}"

    # ── 压缩信息 (offset 0x50) ──
    data += struct.pack('B', ap.compression)                      # 0x50: u8 compression
    if ap.version >= 10:
        data += struct.pack('<I', ap.name_table_size)             # 0x51: u32 nameTableSize

    # ── 骨骼表: name(64) + matrix(64) + parent_index(4) = 132 字节 ──
    for bone in ap.bones:
        bn = bone.name.encode('utf-8')[:64]
        data += bn + b'\x00' * (64 - len(bn))                     # name[64]
        data += struct.pack('<16f', *bone.matrix)                 # matrix[16 floats]
        parent_1based = bone.parent_index + 1 if bone.parent_index >= 0 else 0
        data += struct.pack('<I', parent_1based)                  # parent_index (1-based)

    # ── 动画段 (无 tag/separator) ──
    for seg in ap.segments:
        # 参考 SQT (如果 refSqtFlag > 0)
        if ap.ref_sqt_flag > 0 and ap.bone_defs_flag > 0:
            for sqt in seg.sqt_list:
                for f in sqt.scale:       data += struct.pack('<f', f)
                for f in sqt.rotation:    data += struct.pack('<f', f)
                for f in sqt.translation: data += struct.pack('<f', f)

        # clip 数据
        if seg.clip_data:
            cd = seg.clip_data
            if use_raw and cd.raw_bytes:
                clip_bytes = cd.raw_bytes
            else:
                clip_bytes = _rebuild_clip_data(cd, ap.version)

            if ap.compression > 0:
                compressed = _lz4_block.compress(clip_bytes, store_size=False)
                data += struct.pack('<I', len(compressed))     # compressedSize
                data += struct.pack('<I', len(clip_bytes))     # decompressedSize
                data += compressed
            else:
                data += clip_bytes
        elif seg.compressed_data:
            data += struct.pack('<I', seg.compressed_size)
            data += struct.pack('<I', seg.decompressed_size)
            data += seg.compressed_data

    with open(output_path, 'wb') as f:
        f.write(bytes(data))
    return len(data)

def _rebuild_clip_data(cd, version):
    """从结构化字段重建解压后的 clip 数据。"""
    buf = bytearray()
    u32_count = _clip_header_u32_count(version)
    for v in cd.header[:u32_count]:
        buf += struct.pack('<I', v)
    for sqt in cd.sqt_list:
        for f in sqt.scale:       buf += struct.pack('<f', f)
        for f in sqt.rotation:    buf += struct.pack('<f', f)
        for f in sqt.translation: buf += struct.pack('<f', f)
    if cd.keyframe_header:
        kf = cd.keyframe_header
        buf += struct.pack('<I', kf.field1)
        buf += struct.pack('<I', kf.field2)
        buf += struct.pack('<I', kf.flags)
        if version > 8:
            for f in kf.bbox_min: buf += struct.pack('<f', f)
            for f in kf.bbox_max: buf += struct.pack('<f', f)
        if version >= 11:
            for f in kf.extra1:   buf += struct.pack('<f', f)
            for f in kf.extra2:   buf += struct.pack('<f', f)
        buf += bytes(kf.per_bone_flags)
        buf += kf.remaining_data
    return bytes(buf)

def _json_to_animpack(j):
    """从 JSON 字典完整重建 AnimPack 对象。"""
    h = j['header']
    ap = AnimPack(
        version=h['version'], name=h['name'],
        bone_count=h['bone_count'],
        bone_defs_flag=h.get('bone_defs_flag', 1),
        ref_sqt_flag=h.get('ref_sqt_flag', 1),
        compression=h.get('compression', 2),
        name_table_size=h.get('name_table_size', 0),
    )
    for bd in j['bones']:
        matrix = bd.get('matrix', [1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1])
        ap.bones.append(Bone(
            index=bd['index'],
            name=bd['name'],
            matrix=list(matrix),
            parent_index=bd.get('parent_index', -1),
        ))
    for sd in j['segments']:
        sqt_list = []
        for sq in sd.get('sqt_list', []):
            sqt_list.append(AnimSQT(
                scale=sq.get('scale', [1,1,1]),
                rotation=sq.get('rotation', [0,0,0,1]),
                translation=sq.get('translation', [0,0,0]),
            ))
        seg = AnimSegment(
            index=sd['index'],
            sqt_list=sqt_list,
            compressed_size=sd.get('compressed_size', 0),
            decompressed_size=sd.get('decompressed_size', 0),
        )
        if 'clip_header' in sd:
            cd = ClipData(header=sd['clip_header'], sqt_list=sqt_list)
            kf_data = sd.get('keyframe')
            if kf_data:
                remaining = b""
                if kf_data.get('data_base64'):
                    remaining = base64.b64decode(kf_data['data_base64'])
                cd.keyframe_header = KeyframeHeader(
                    field1=kf_data.get('start_frame', 0),
                    field2=kf_data.get('end_frame', 0),
                    flags=kf_data.get('flags', 0),
                    bbox_min=kf_data.get('bbox_min', [0,0,0]),
                    bbox_max=kf_data.get('bbox_max', [0,0,0]),
                    extra1=kf_data.get('extra1', [0,0,0]),
                    extra2=kf_data.get('extra2', [0,0,0]),
                    per_bone_flags=kf_data.get('per_bone_flags', [0]*ap.bone_count),
                    remaining_data=remaining,
                )
            # 重建 raw_bytes 以便 _decode_kf_sets 等函数使用
            cd.raw_bytes = _rebuild_clip_data(cd, ap.version)
            seg.clip_data = cd
        ap.segments.append(seg)
    return ap

def cmd_pack(input_path, output_path, verify=True):
    """反向打包命令。"""
    if input_path.endswith('.json'):
        with open(input_path, 'r', encoding='utf-8') as f:
            j = json.load(f)
        ap = _json_to_animpack(j)
        size = pack_animpack(ap, output_path, use_raw=False)
        print(f"  [JSON → animpack] 已打包: {output_path} ({size:,} 字节)")
        print(f"    名称: {ap.name}  骨骼: {ap.bone_count}  段: {len(ap.segments)}")

        if verify:
            ap2 = parse_animpack(output_path)
            ok = True
            if ap2.bone_count != ap.bone_count:
                print(f"    [验证] 骨骼数不一致: {ap.bone_count} → {ap2.bone_count}"); ok = False
            else:
                for b1, b2 in zip(ap.bones, ap2.bones):
                    if b1.name != b2.name:
                        print(f"    [验证] 骨骼名不一致: {b1.name} → {b2.name}"); ok = False; break
                    if b1.parent_index != b2.parent_index:
                        print(f"    [验证] 父索引不一致: {b1.parent_index} → {b2.parent_index}"); ok = False; break
            if ap.segments and ap2.segments:
                s1, s2 = ap.segments[0], ap2.segments[0]
                if s1.clip_data and s2.clip_data:
                    if s1.clip_data.header != s2.clip_data.header:
                        print(f"    [验证] clip_header 不一致"); ok = False
                    kf1 = s1.clip_data.keyframe_header
                    kf2 = s2.clip_data.keyframe_header
                    if kf1 and kf2:
                        if kf1.field1 != kf2.field1 or kf1.field2 != kf2.field2:
                            print(f"    [验证] 关键帧帧范围不一致"); ok = False
                        if kf1.per_bone_flags != kf2.per_bone_flags:
                            print(f"    [验证] per_bone_flags 不一致"); ok = False
                        if kf1.remaining_data != kf2.remaining_data:
                            print(f"    [验证] 关键帧数据不一致"); ok = False
            if ok:
                print(f"    [验证] 全部一致 — JSON 往返转换成功")
    else:
        ap = parse_animpack(input_path)
        size = pack_animpack(ap, output_path, use_raw=True)
        print(f"  [animpack → animpack] 已打包: {output_path} ({size:,} 字节)")
        if verify:
            with open(input_path, 'rb') as f: orig = f.read()
            with open(output_path, 'rb') as f: packed = f.read()
            if orig == packed:
                print(f"  [验证] 字节级完全一致 ({len(orig):,} 字节)")
            else:
                ap2 = parse_animpack(output_path)
                if ap2.bone_count == ap.bone_count and ap.segments and ap2.segments:
                    s1, s2 = ap.segments[0], ap2.segments[0]
                    if s1.clip_data and s2.clip_data and s1.clip_data.raw_bytes == s2.clip_data.raw_bytes:
                        print(f"  [验证] 解压数据字节级一致 (仅 LZ4 压缩字节不同)")

def cmd_pack_dir(input_dir, output_dir, verify=True):
    """批量打包"""
    os.makedirs(output_dir, exist_ok=True)
    files = sorted(f for f in os.listdir(input_dir) if f.endswith('.animpack'))
    if not files:
        print(f"  [错误] {input_dir} 下无 .animpack 文件"); return
    ok = 0; fail = 0; byte_match = 0
    for fname in files:
        try:
            src = os.path.join(input_dir, fname)
            dst = os.path.join(output_dir, fname)
            ap = parse_animpack(src)
            size = pack_animpack(ap, dst, use_raw=True)
            with open(src, 'rb') as f: orig = f.read()
            with open(dst, 'rb') as f: packed = f.read()
            if orig == packed:
                byte_match += 1; status = "字节一致"
            elif len(orig) == len(packed):
                status = "大小一致(内容不同)"
            else:
                status = f"大小不同({len(orig)}→{len(packed)})"
            print(f"    [OK] {fname}: {size:,}B — {status}")
            ok += 1
        except Exception as e:
            print(f"    [失败] {fname}: {e}"); fail += 1
    print(f"\n  完成: {ok}/{len(files)} 成功, {fail} 失败, {byte_match} 字节级一致")

# ═══════════════════════════════════════════════════════════════
#  6. 交互式菜单
# ═══════════════════════════════════════════════════════════════
def interactive_menu():
    print("\n  ╔══════════════════════════════════════════════════════╗")
    print("  ║   Sky: Children of the Light .animpack 转换工具     ║")
    print("  ║   (修正版: name+matrix+parent_index 格式)            ║")
    print("  ╚══════════════════════════════════════════════════════╝")
    if not HAS_LZ4:
        print("  [警告] pip install lz4")
    while True:
        print("\n  " + "─"*52)
        print("   1. 扫描目录     — 批量查看所有文件信息")
        print("   2. animpack→JSON — 解析为可读JSON")
        print("   3. JSON→animpack — 从JSON重建animpack")
        print("   4. animpack→glTF — 转换为glTF 2.0")
        print("   5. glTF→animpack — 从glTF重建animpack")
        print("   0. 退出")
        ch = input("  选择 [0-5]: ").strip()
        if ch == '0': break

        elif ch == '1':
            d = input("  目录路径(回车=.): ").strip() or "."
            if not os.path.isdir(d):
                print("  [错误] 目录不存在"); continue
            files = sorted(f for f in os.listdir(d) if f.endswith('.animpack'))
            if not files:
                print("  [提示] 该目录下无 .animpack 文件"); continue
            print(f"\n  发现 {len(files)} 个动画文件:\n")
            print(f"  {'#':>3}  {'文件名':<45} {'骨骼':>5} {'压缩':>4} {'大小':>8} {'关键帧'}")
            print("  " + "─"*80)
            for i, fname in enumerate(files):
                try:
                    ap = parse_animpack(os.path.join(d, fname))
                    has_kf = "有" if (ap.segments and ap.segments[0].clip_data and
                               ap.segments[0].clip_data.keyframe_header and
                               ap.segments[0].clip_data.keyframe_header.has_keyframes) else "无"
                    print(f"  {i:3d}  {fname:<45} {ap.bone_count:>5} {ap.compression:>3}  {ap.file_size/1024:>6.1f}KB  {has_kf}")
                except Exception as e:
                    print(f"  {i:3d}  {fname:<45} [错误: {e}]")

        elif ch == '2':
            p = input("  animpack 文件路径: ").strip().strip('"\'')
            if not os.path.isfile(p):
                print("  [错误] 文件不存在"); continue
            out = input(f"  JSON 输出路径(回车={os.path.splitext(p)[0]}.json): ").strip()
            out = out or os.path.splitext(p)[0] + ".json"
            try:
                ap = parse_animpack(p)
                with open(out, 'w', encoding='utf-8') as f: f.write(to_json(ap))
                has_kf = "有动态关键帧" if (ap.segments and ap.segments[0].clip_data and
                    ap.segments[0].clip_data.keyframe_header and
                    ap.segments[0].clip_data.keyframe_header.has_keyframes) else "静态姿态"
                print(f"  [OK] {out}")
                print(f"    名称: {ap.name}  骨骼: {ap.bone_count}  压缩: {ap.compression}  {has_kf}")
            except Exception as e:
                print(f"  [错误] {e}")

        elif ch == '3':
            p = input("  JSON 文件路径: ").strip().strip('"\'')
            if not os.path.isfile(p):
                print("  [错误] 文件不存在"); continue
            out = input(f"  animpack 输出路径(回车={os.path.splitext(p)[0]}.animpack): ").strip()
            out = out or os.path.splitext(p)[0] + ".animpack"
            try:
                cmd_pack(p, out)
            except Exception as e:
                print(f"  [错误] {e}")

        elif ch == '4':
            p = input("  animpack 文件路径: ").strip().strip('"\'')
            if not os.path.isfile(p):
                print("  [错误] 文件不存在"); continue
            out = input(f"  glTF 输出路径(回车={os.path.splitext(p)[0]}.gltf): ").strip()
            out = out or os.path.splitext(p)[0] + ".gltf"
            try:
                cmd_gltf(p, out)
            except Exception as e:
                print(f"  [错误] {e}")

        elif ch == '5':
            p = input("  glTF 文件路径: ").strip().strip('"\'')
            if not os.path.isfile(p):
                print("  [错误] 文件不存在"); continue
            out = input(f"  animpack 输出路径(回车={os.path.splitext(p)[0]}.animpack): ").strip()
            out = out or os.path.splitext(p)[0] + ".animpack"
            try:
                cmd_gltf_import(p, out)
            except Exception as e:
                print(f"  [错误] {e}")

# ═══════════════════════════════════════════════════════════════
#  CLI 主函数
# ═══════════════════════════════════════════════════════════════
def _usage():
    print("""
  animpack_all_in_one.py — 光遇 .animpack 全功能独立脚本 (修正版)

  用法:
    python animpack_all_in_one.py                              交互式菜单
    python animpack_all_in_one.py <file.animpack>              打印摘要
    python animpack_all_in_one.py <file> --json out.json       导出JSON
    python animpack_all_in_one.py batch <目录> <输出目录>        批量CSV+JSON
    python animpack_all_in_one.py gltf <file> <out.gltf>       animpack→glTF
    python animpack_all_in_one.py gltf_import <file.gltf> <out.animpack>  glTF→animpack
    python animpack_all_in_one.py tree <file> <out.html>       骨骼树可视化
    python animpack_all_in_one.py compare <目录> <out.json>     对比分析
    python animpack_all_in_one.py pack <file> <out.animpack>   JSON→animpack / animpack→animpack
    python animpack_all_in_one.py all <目录> <输出目录>          一键全部

  依赖:
    pip install lz4
""")

def main():
    if len(sys.argv) < 2:
        interactive_menu(); return

    cmd = sys.argv[1].lower()

    if cmd in ("batch","gltf","gltf_import","tree","compare","pack","all"):
        if cmd == "batch" and len(sys.argv) >= 4:
            cmd_batch(sys.argv[2], sys.argv[3])
        elif cmd == "gltf" and len(sys.argv) >= 4:
            cmd_gltf(sys.argv[2], sys.argv[3])
        elif cmd == "gltf_import" and len(sys.argv) >= 4:
            cmd_gltf_import(sys.argv[2], sys.argv[3])
        elif cmd == "tree" and len(sys.argv) >= 4:
            cmd_tree(sys.argv[2], sys.argv[3])
        elif cmd == "compare" and len(sys.argv) >= 4:
            cmd_compare(sys.argv[2], sys.argv[3])
        elif cmd == "pack" and len(sys.argv) >= 4:
            inp = sys.argv[2]; outp = sys.argv[3]
            if os.path.isdir(inp):
                cmd_pack_dir(inp, outp)
            else:
                cmd_pack(inp, outp)
        elif cmd == "all" and len(sys.argv) >= 4:
            input_dir = sys.argv[2]; output_dir = sys.argv[3]
            print("\n  ═══ 1. 批量导出 ═══")
            cmd_batch(input_dir, output_dir)
            files = sorted(f for f in os.listdir(input_dir) if f.endswith('.animpack'))
            gltf_dir = os.path.join(output_dir,"gltf")
            tree_dir = os.path.join(output_dir,"trees")
            os.makedirs(gltf_dir, exist_ok=True)
            os.makedirs(tree_dir, exist_ok=True)
            print("\n  ═══ 2. glTF 导出 ═══")
            for f in files:
                try: cmd_gltf(os.path.join(input_dir,f), os.path.join(gltf_dir, f.replace('.animpack','.gltf')))
                except Exception as e: print(f"    [失败] {f}: {e}")
            print("\n  ═══ 3. 骨骼树 ═══")
            for f in files:
                try: cmd_tree(os.path.join(input_dir,f), os.path.join(tree_dir, f.replace('.animpack','.html')))
                except Exception as e: print(f"    [失败] {f}: {e}")
            print("\n  ═══ 4. 对比分析 ═══")
            cmd_compare(input_dir, os.path.join(output_dir,"comparison.json"))
            print(f"\n  ═══ 全部完成! 输出: {output_dir} ═══")
        else:
            _usage()
        return

    if cmd in ("--help","-h"):
        _usage(); return

    file_path = sys.argv[1]
    if not os.path.isfile(file_path):
        print(f"  [错误] 文件不存在: {file_path}")
        _usage(); return

    json_path = None
    for i, arg in enumerate(sys.argv[2:], 2):
        if arg == "--json" and i+1 < len(sys.argv):
            json_path = sys.argv[i+1]

    try:
        ap = parse_animpack(file_path)
    except Exception as e:
        print(f"  [错误] {e}"); return

    if json_path:
        with open(json_path,'w',encoding='utf-8') as f: f.write(to_json(ap))
        print(f"  JSON 已导出: {json_path}")
    else:
        print(to_summary(ap))

if __name__ == '__main__':
    main()
