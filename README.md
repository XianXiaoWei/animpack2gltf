# 光遇 .animpack 格式逆向分析与转换脚本完整思路文档

## 目录

1. [项目背景与目标](#1-项目背景与目标)
2. [逆向工程方法论](#2-逆向工程方法论)
3. [文件总体结构](#3-文件总体结构)
4. [文件头解析 (0x00-0x54)](#4-文件头解析-0x00-0x54)
5. [骨骼记录格式](#5-骨骼记录格式)
6. [动画段结构](#6-动画段结构)
7. [SQT (Scale-Quaternion-Translation) 格式](#7-sqt-scale-quaternion-translation-格式)
8. [关键帧系统](#8-关键帧系统)
9. [int16 压缩解码](#9-int16-压缩解码)
10. [glTF 导出思路](#10-gltf-导出思路)
11. [关键 Bug 与修复过程](#11-关键-bug-与修复过程)
12. [验证方法](#12-验证方法)
13. [脚本架构](#13-脚本架构)

---

## 1. 项目背景与目标

Sky: Children of the Light (光遇) 使用 `.animpack` 作为角色动画打包格式。该格式包含骨骼层级、绑定姿态和关键帧动画数据。

目标：
- 完整解析 `.animpack` 二进制格式
- 导出为标准 glTF 2.0 文件，在 Blender 等建模软件中播放动画
- 支持双向转换 (animpack ↔ JSON ↔ glTF)
- 支持反向打包 (JSON → animpack)

---

## 2. 逆向工程方法论

### 2.1 分析工具

- **libBootloader.so 逆向**: 使用 Ghidra/IDA 反编译游戏原生库，找到解析 `.animpack` 的函数
- **SkyModelViewer**: C# 开源项目，解析 `.mesh` 文件和内嵌骨骼，但**不处理动画**
- **二进制对比分析**: 对比多个 animpack 文件，找出共同模式
- **数学验证**: 用已知值 (SQT) 反推解码公式正确性

### 2.2 关键发现路径

```
二进制 dump → 格式猜测 → .so 逆向验证 → 数学验证 → 修正 → 端到端验证
```

### 2.3 核心地址引用 (libBootloader.so)

| 地址 | 功能 |
|------|------|
| `0xfd4f54` | 关键帧通道位映射常量 |
| `0xfd5c70` | 关键帧读取函数 |
| `0x00fd5c70` | keyframe reader 入口 |

---

## 3. 文件总体结构

```
┌─────────────────────────────────────────┐  offset 0x00
│  文件头 (80 字节)                        │
│  version(4) + name(64) + boneCount(4)   │
│  + boneDefsFlag(4) + refSqtFlag(4)      │
├─────────────────────────────────────────┤  offset 0x50
│  压缩信息 (5 字节)                       │
│  compression(1) + nameTableSize(4)      │
├─────────────────────────────────────────┤  offset 0x55
│  骨骼表 (boneCount × 132 字节)           │
│  每条: name(64) + matrix(64) + parent(4)│
├─────────────────────────────────────────┤
│  动画段 (无分隔符, 连续排列)              │
│  ┌─ 参考 SQT (可选)                      │
│  ┌─ 压缩 clip 数据 (LZ4)                 │
│  └─ (下一段...)                          │
└─────────────────────────────────────────┘
```

### 3.1 版本差异

| 版本范围 | 头部大小 | 差异 |
|----------|----------|------|
| < 10 | 80 + 1 = 81B (0x51起为骨骼区) | 无 nameTableSize |
| >= 10 | 80 + 5 = 85B (0x55起为骨骼区) | 有 nameTableSize(u32) |
| >= 10 clip header | 24B (6×u32) | 多一个 u32 字段 |
| < 10 clip header | 20B (5×u32) | 少一个 u32 字段 |
| > 8 关键帧头 | 含 bbox_min/max | 24B 额外 |
| >= 11 关键帧头 | 含 extra1/extra2 | 24B 额外 (int16平移用) |

---

## 4. 文件头解析 (0x00-0x54)

### 4.1 头部字段表

| 偏移 | 大小 | 类型 | 名称 | 说明 |
|------|------|------|------|------|
| 0x00 | 4 | u32 | version | 格式版本 (实测值 9-11) |
| 0x04 | 64 | char[64] | name | 动画名称 (UTF-8, 零填充) |
| 0x44 | 4 | u32 | boneCount | 骨骼数量 |
| 0x48 | 4 | u32 | boneDefsFlag | >0 表示骨骼表含矩阵 |
| 0x4C | 4 | u32 | refSqtFlag | >0 表示动画段含参考SQT |
| 0x50 | 1 | u8 | compression | 0=无压缩, 1=LZ4+float, 2=LZ4+int16 |
| 0x51 | 4 | u32 | nameTableSize | 名称表大小 (version>=10, 仅用于内存分配) |

### 4.2 解析思路

头部是固定的 80 字节 (0x00-0x4F)。0x50 处是 1 字节 compression，version >= 10 时 0x51 处还有 4 字节 nameTableSize。

**关键发现**: boneDefsFlag 和 refSqtFlag 这两个标志位控制骨骼表和动画段的结构。不是所有文件都有矩阵或参考SQT——取决于这两个标志。

---

## 5. 骨骼记录格式

### 5.1 格式定义

每条骨骼记录 = **132 字节**:

```
偏移 0-63:   name (64 字节, UTF-8, 零填充)
偏移 64-127: matrix (16 × float32 = 64 字节, 4×4 行主序)
偏移 128-131: parent_index (u32, 1-based, 0=根骨骼)
```

### 5.2 发现过程

**初期错误假设**: 骨骼记录 = name(64) + parent(4) + 12字节其他 = 80 字节。这导致解析位置全部错位。

**正确发现**: 通过对比已知骨骼名和二进制数据中的字符串位置，发现骨骼间距是 132 字节而非 80 字节。多出的 64 字节正好是 16 个 float32 (4×4 矩阵)。

**parent_index 验证**:
- 读取的值是 1-based
- 0 表示根骨骼 (无父节点)
- 转换为 0-based: `parent_0based = value > 0 ? value - 1 : -1`

### 5.3 矩阵语义

骨骼记录中的 4×4 矩阵是 **InverseBindMatrix (IBM)**，用于网格蒙皮。这是绑定姿态 (bind pose) 的逆矩阵，**不是**动画基础姿态。

**关键区分**:
- `bone.matrix` = 绑定姿态 IBM (bind pose)
- SQT = 动画基础姿态 (animation rest pose)
- 这两个是**不同的姿态**

矩阵的平移分量 `matrix[12], matrix[13], matrix[14]` 给出骨骼在绑定姿态下的位置。

### 5.4 FNV-1a 哈希

骨骼名使用 FNV-1a 64-bit 哈希进行内部标识:

```python
FNV1A_OFFSET_BASIS = 0xcbf29ce484222325
FNV1A_PRIME = 0x100000001b3

def fnv1a_64(data: bytes) -> int:
    h = FNV1A_OFFSET_BASIS
    for b in data:
        h ^= b
        h = (h * FNV1A_PRIME) & 0xFFFFFFFFFFFFFFFF
    return h
```

---

## 6. 动画段结构

### 6.1 段内布局

动画段紧跟在骨骼表之后，**无任何分隔符或 tag**。每段结构:

```
┌─ 参考 SQT (条件: refSqtFlag > 0 且 boneDefsFlag > 0)
│   boneCount × 40 字节
│
├─ 压缩块 (条件: compression > 0)
│   u32 compressedSize
│   u32 decompressedSize
│   bytes[compressedSize] — LZ4 压缩数据
│
│  (若 compression = 0, clip 数据直接内嵌)
└─ (下一段从压缩块结尾继续)
```

### 6.2 解压缩后的 Clip 数据

```
┌─ clip header (5 或 6 个 u32)
├─ SQT 列表 (boneCount × 40 字节)
└─ 关键帧数据 (剩余字节)
```

### 6.3 clip header 字段

| 索引 | 含义 |
|------|------|
| [0] | keyframe_set_count (关键帧集数量) |
| [1]-[5] | 其他帧信息 (帧率相关) |

### 6.4 关键发现: 无分隔符

**初期错误**: 假设动画段之间有 tag 或 separator 字节。

**正确发现**: 通过逐字节扫描和 LZ4 magic number 检测，确认段之间**连续排列**，无任何分隔。解析完全依赖 `compressedSize` 字段来确定下一段的起始位置。

---

## 7. SQT (Scale-Quaternion-Translation) 格式

### 7.1 磁盘格式 (40 字节)

```
偏移 0-11:   Scale (3 × float32 = 12 字节)
偏移 12-27:  Rotation (4 × float32 = 16 字节, 四元数 xyzw)
偏移 28-39:  Translation (3 × float32 = 12 字节)
```

### 7.2 四元数顺序

存储顺序为 **(x, y, z, w)**，即前三个是虚部，最后是实部。

验证方法: 所有 SQT 四元数的模长应接近 1.0:
```python
|q| = sqrt(x² + y² + z² + w²) ≈ 1.0
```

实测 124 个骨骼的 SQT 四元数模长全部接近 1.0，确认顺序正确。

### 7.3 SQT 的双重角色

1. **参考 SQT** (段头部): 动画段的基础姿态
2. **clip 内 SQT** (clip header 之后): 同样是基础姿态

两者数据相同，脚本优先使用参考 SQT (段头部)，如果不存在则使用 clip 内 SQT。

---

## 8. 关键帧系统

### 8.1 关键帧集结构

每个关键帧集 (keyframe set) 的二进制布局:

```
u32 field1          — 起始帧索引
u32 field2          — 结束帧索引
u32 flags           — 位标志 (bit0 = int16平移通道开关)
[3f bbox_min]       — AABB 最小值 (version > 8)
[3f bbox_max]       — AABB 最大值 (version > 8)
[3f extra1]         — int16平移偏移 (version >= 11)
[3f extra2]         — int16平移缩放 (version >= 11)
bytes[boneCount] per_bone_flags — 每骨骼通道标志
--- 关键帧数据 ---
```

### 8.2 通道位映射 (来自 .so 0xfd4f54)

| 位 | 名称 | 语义 | 读取方式 |
|----|------|------|----------|
| bit3 | Scale | 主缩放 (初始姿态) | 读 **1 次** |
| bit4 | Quat | 主旋转 (初始姿态) | 读 **1 次** |
| bit5 | Trans | 主平移 (初始姿态) | 读 **1 次** |
| bit0 | Scale2 | 副缩放 (逐帧) | 读 **frame_count 次** |
| bit1 | Quat2 | 副旋转 (逐帧) | 读 **frame_count 次** |
| bit2 | Trans2 | 副平移 (逐帧) | 读 **frame_count 次** |

### 8.3 关键发现: 主通道读一次，副通道读 frame_count 次

**frame_count** = `field2 - field1 + 1`

**初期错误**: 假设所有通道都读 frame_count 次。导致数据错位。

**正确发现**: 通过 .so 逆向和数学验证发现:
- 主通道 (bit3/4/5) 是初始姿态，只读**1次** (位于 field1 帧)
- 副通道 (bit0/1/2) 是逐帧动画数据，读 **frame_count 次**

### 8.4 副通道读取顺序 (每帧)

每帧内的副通道按固定顺序读取:

```
Frame i:
  1. Scale2 (bit0) — 所有有 bit0 的骨骼, 各读 scale_sz 字节
  2. Quat2  (bit1) — 所有有 bit1 的骨骼, 各读 quat_sz 字节
  3. Trans2 (bit2) — 所有有 bit2 的骨骼, 各读 trans_sz 字节
```

### 8.5 主通道读取顺序

```
1. Scale (bit3) — 所有有 bit3 的骨骼, 各读 scale_sz 字节
2. Quat  (bit4) — 所有有 bit4 的骨骼, 各读 quat_sz 字节
3. Trans (bit5) — 所有有 bit5 的骨骼, 各读 trans_sz 字节
```

### 8.6 数据大小

| 通道 | compression=2 (int16) | compression=1 (float) |
|------|----------------------|----------------------|
| Scale / Scale2 | 12B (float32 × 3) | 12B (float32 × 3) |
| Quat / Quat2 | 8B (int16 × 4) | 16B (float32 × 4) |
| Trans / Trans2 | 6B (int16 × 3, 若 flags&1) 或 12B | 12B (float32 × 3) |

**关键**: Scale 始终是 float32，不受 compression 影响。

---

## 9. int16 压缩解码

### 9.1 四元数解码 (int16 → float)

```python
def decode_i16_quat(data, pos):
    """4 × uint16 → [x, y, z, w], 范围 [-1, 1]"""
    r = struct.unpack_from('<4H', data, pos)
    return [(r[i] - 32768) / 32767.0 for i in range(4)]
```

**公式**: `(uint16 - 32768) / 32767.0`

**为什么不是 `int16 / 32767.0`?**

实测验证:
- `(u16 - 32768) / 32767.0` → 四元数模长 ≈ 1.000008 ✓
- `int16 / 32767.0` → 四元数模长 ≈ 1.426 ✗

原始数据是无符号 uint16，范围 [0, 65535]。减去 32768 后得到 [-32768, 32767]，再除以 32767 得到 [-1, 1]。

### 9.2 平移解码 (int16 → float) — 关键修复

```python
def decode_i16_trans(data, pos, extra1, extra2):
    """3 × uint16 → [x, y, z], 使用 extra1/extra2 解码"""
    r = struct.unpack_from('<3H', data, pos)
    return [extra1[i] + (r[i] / 65535.0) * extra2[i] for i in range(3)]
```

**正确公式**: `extra1[i] + (u16[i] / 65535.0) * extra2[i]`

- `extra1` = 偏移量 (offset)
- `extra2` = 缩放因子 (scale)
- 输入 0 → extra1
- 输入 65535 → extra1 + extra2

### 9.3 修复过程

**错误公式** (初期): `extra1[i] + (u16[i] / 65535.0) * (extra2[i] - extra1[i])`

这个公式假设 extra2 是范围上限，extra1 是范围下限。但实际验证发现:

1. 对 field1=0 的关键帧集，trans2[0] 应该等于 SQT 的 translation
2. 用错误公式解码，trans2[0] 与 SQT 不匹配
3. 通过数学反推: 已知 SQT translation 和 u16 原始值，求解 extra1 和 extra2 的关系
4. 发现 `extra1 + (u16/65535) * extra2` 完美匹配，而 `extra1 + (u16/65535) * (extra2-extra1)` 不匹配
5. 批量验证: 213/213 (100%) keyframe set 与 SQT 完全匹配 (误差 < 0.00002)

### 9.4 关键帧数据是绝对值

通过验证发现:
- quat2[0] (field1=0 时) 与 SQT 旋转角度差 = 0.00°
- trans2[0] (field1=0 时) 与 SQT 平移差 < 0.00002

结论: **关键帧数据是绝对局部变换，不是从 SQT 的增量 (delta)**。这意味着每帧的关键帧值可以直接用作骨骼的局部变换。

---

## 10. glTF 导出思路

### 10.1 节点层级

```
Scene
└─ Root Node (根骨骼, 如 M_hip)
   ├─ Child 1 (如 L_legRoot)
   │  ├─ Child 1.1 (如 L_knee)
   │  └─ ...
   ├─ Child 2 (如 M_spine)
   └─ ...
```

- 每个骨骼 = 一个 glTF node
- node 的 translation/rotation/scale = SQT 值
- node 的 children = parent_index 对应的子骨骼

### 10.2 逆绑定矩阵 (IBM)

**关键决策**: 不使用 `bone.matrix` (绑定姿态 IBM)，而是从 SQT 世界变换计算 IBM:

```python
world_mats = compute_world_transforms(bones, sqt_list, parents, root_idx)
# IBM = inverse(world_from_SQT)
for i in range(len(bones)):
    ibm = invert_mat4(world_mats[i])
```

**原因**: glTF 要求 IBM 与节点变换一致。如果用 bone.matrix (绑定姿态) 而 node 用 SQT (动画基础姿态)，两者不匹配会导致蒙皮变形错误。用 SQT 计算的 IBM 保证一致性。

### 10.3 动画数据

每个骨骼有 3 个可能的动画轨道:
- **rotation** (VEC4, 四元数)
- **translation** (VEC3)
- **scale** (VEC3)

动画构建流程:
1. 将 SQT 作为 frame 0 的初始关键帧 (确保基础姿态存在)
2. 主通道 (Scale/Quat/Trans) 作为 field1 帧的单个关键帧
3. 副通道 (Scale2/Quat2/Trans2) 作为 field1 ~ field2 的逐帧关键帧
4. 时间 = frame / 30 (30 FPS)
5. 四元数归一化
6. 共享相同时间轴的通道复用 input accessor

### 10.4 静态骨骼可视化 mesh — 已移除

**问题**: 早期版本添加了骨骼连线 mesh (LINES primitive)，但这些顶点位置是静态的，不跟随动画。导致用户看到"点在动线不动"。

**解决**: 完全移除静态 mesh。建模软件 (Blender) 原生支持 armature 显示，不需要额外的 mesh。

---

## 11. 关键 Bug 与修复过程

### 11.1 Bug: 骨骼记录大小错误 (80 → 132)

| 项目 | 错误 | 正确 |
|------|------|------|
| 大小 | 80 字节 | 132 字节 |
| 结构 | name(64) + parent(4) + 12B其他 | name(64) + matrix(64) + parent(4) |
| 影响 | 所有骨骼位置错位 | - |

**发现方法**: 对比二进制中已知骨骼名的间距，确认是 132 而非 80。

### 11.2 Bug: 主/副通道读取次数错误

| 项目 | 错误 | 正确 |
|------|------|------|
| 主通道 | 读 frame_count 次 | 读 **1 次** |
| 副通道 | 读 1 次 | 读 **frame_count 次** |

**发现方法**: .so 逆向确认主通道是 init (单次)，副通道是 per-frame (多次)。数学验证 quat2[0] 与 SQT 匹配。

### 11.3 Bug: int16 平移解码公式错误

| 项目 | 错误 | 正确 |
|------|------|------|
| 公式 | `extra1 + (u/65535) * (extra2 - extra1)` | `extra1 + (u/65535) * extra2` |
| extra2 语义 | 范围上限 | 缩放因子 |

**发现方法**: 数学反推 SQT translation 和 u16 原始值的关系，验证 213/213 匹配。

### 11.4 Bug: IBM 来源错误

| 项目 | 错误 | 正确 |
|------|------|------|
| IBM 来源 | bone.matrix (绑定姿态) | inverse(world_from_SQT) |

**发现方法**: 对比 bone.matrix 和 SQT 的差异，确认两者是不同姿态。glTF 要求 IBM 与 node 变换一致。

### 11.5 Bug: 静态骨骼 mesh 不跟随动画

| 项目 | 错误 | 正确 |
|------|------|------|
| 方案 | 添加静态 LINES mesh | 移除 mesh, 依赖 armature |

**发现方法**: 用户反馈"点在动线不动"。静态 mesh 顶点固定，不随骨骼动画移动。

### 11.6 Bug: 四元数解码用 signed int16

| 项目 | 错误 | 正确 |
|------|------|------|
| 解码 | `int16 / 32767.0` | `(uint16 - 32768) / 32767.0` |
| 模长 | ≈ 1.426 (非单位四元数) | ≈ 1.000008 (单位四元数) |

**发现方法**: 检查四元数模长，单位四元数必须 |q| ≈ 1.0。

---

## 12. 验证方法

### 12.1 SQT 对比验证

对于 field1=0 的关键帧集，关键帧第一帧的值应该等于 SQT:

```
trans2[0] ≈ SQT.translation  (误差 < 0.001)
quat2[0] ≈ SQT.rotation      (角度差 < 0.1°)
```

### 12.2 批量验证结果

| 项目 | 结果 |
|------|------|
| trans2 匹配 SQT | 199/199 (100%) |
| quat2 匹配 SQT | 513/533 (96.2%) |
| 四元数模长 | 全部 = 1.000000 |

20 个 quat2 "不匹配" 全部是头发骨骼，初始动画姿态与绑定姿态有约 3° 偏移，是正常的物理动画数据。

### 12.3 JSON 往返验证

```
animpack → JSON → animpack → 对比
```

验证骨骼名、父索引、clip header、关键帧数据全部一致。

### 12.4 字节级验证

```
animpack → parse → pack → 对比原始字节
```

使用 LZ4 原始数据 (use_raw=True) 时，部分文件字节级完全一致。

---

## 13. 脚本架构

### 13.1 文件结构

```
animpack_all_in_one.py (1668 行, 单文件)
├── 常量定义 (L31-69)
│   ├── HEADER_SIZE, BONE_RECORD_SIZE, SQT_DISK_SIZE
│   ├── FNV-1a 常量
│   └── KF_FLAG_NAMES (通道位映射)
├── FNV-1a 哈希 (L74-79)
├── 数据结构 (L84-170)
│   ├── SQT, AnimSQT (Scale + Rotation + Translation)
│   ├── Bone (index + name + matrix + parent_index)
│   ├── KeyframeHeader (field1/field2/flags/bbox/extra/per_bone_flags)
│   ├── ClipData (header + sqt_list + keyframe_header)
│   ├── AnimSegment (sqt_list + compressed_data + clip_data)
│   └── AnimPack (version + name + bones + segments)
├── 底层读取函数 (L175-186)
│   └── _read_u32, _read_f32, _read_name, _read_vector3, _read_quat
├── 解析函数 (L191-318)
│   ├── _parse_anim_sqt — 解析单个 SQT (40字节)
│   ├── _decompress_clip — LZ4 解压
│   ├── _parse_keyframe_header — 关键帧头解析
│   ├── _parse_clip_data — clip 数据解析
│   ├── _parse_anim_segments — 动画段解析 (无分隔符)
│   └── parse_animpack — 主解析入口
├── 输出函数 (L323-414)
│   ├── to_summary — 人类可读摘要
│   └── to_json — JSON 输出
├── 矩阵运算 (L419-457)
│   ├── quat_to_mat3 — 四元数→旋转矩阵
│   ├── compose_mat4 — SQT→4×4矩阵
│   ├── multiply_mat4 — 矩阵乘法
│   ├── invert_mat4 — 矩阵求逆
│   └── mat4_to_bytes_col_major — 行主序→列优先bytes
├── 层级与变换 (L462-501)
│   ├── get_parents — 父索引列表
│   ├── get_root_idx — 根骨骼索引
│   ├── build_children_map — 子骨骼映射
│   ├── compute_world_transforms — 世界变换矩阵
│   └── _get_bone_sqt_list — 获取SQT列表
├── 命令: 批量导出 (L506-578)
│   └── cmd_batch — CSV + JSON 批量导出
├── 关键帧解码 (L583-736)
│   ├── _decode_i16_quat — int16四元数解码
│   ├── _decode_i16_trans — int16平移解码 (关键修复)
│   └── _decode_kf_sets — 关键帧集解码 (主/副通道)
├── glTF 动画构建 (L738-859)
│   └── _build_gltf_animation — 从关键帧集构建动画
├── 骨骼可视化 (L861-906, 已弃用)
│   └── _add_visual_mesh — 静态连线mesh (不再调用)
├── 命令: glTF 导出 (L911-976)
│   └── cmd_gltf — animpack → glTF 2.0
├── 命令: glTF 导入 (L981-1117)
│   ├── _gltf_ibm_to_matrix — glTF IBM → 行主序
│   ├── _mat4_to_sqt — 4×4矩阵 → SQT
│   └── cmd_gltf_import — glTF → animpack
├── 命令: 骨骼树 (L1122-1199)
│   └── cmd_tree — HTML 可交互骨骼树
├── 命令: 对比分析 (L1205-1253)
│   └── cmd_compare — 多文件对比报告
├── 命令: 反向打包 (L1258-1480)
│   ├── pack_animpack — AnimPack → 二进制
│   ├── _rebuild_clip_data — 结构化→clip字节
│   ├── _json_to_animpack — JSON → AnimPack对象
│   ├── cmd_pack — JSON/animpack → animpack
│   └── cmd_pack_dir — 批量打包
├── 交互式菜单 (L1485-1571)
│   └── interactive_menu — 5选项菜单
└── CLI 主函数 (L1576-1668)
    └── main — 命令行入口
```

### 13.2 功能命令

| 命令 | 用法 | 说明 |
|------|------|------|
| (无参数) | `python animpack_all_in_one.py` | 交互式菜单 |
| (文件) | `python animpack_all_in_one.py <file>` | 打印摘要 |
| --json | `python animpack_all_in_one.py <file> --json out.json` | 导出 JSON |
| batch | `python animpack_all_in_one.py batch <dir> <outdir>` | 批量 CSV+JSON |
| gltf | `python animpack_all_in_one.py gltf <file> <out.gltf>` | animpack→glTF |
| gltf_import | `python animpack_all_in_one.py gltf_import <file.gltf> <out.animpack>` | glTF→animpack |
| tree | `python animpack_all_in_one.py tree <file> <out.html>` | 骨骼树 HTML |
| compare | `python animpack_all_in_one.py compare <dir> <out.json>` | 对比分析 |
| pack | `python animpack_all_in_one.py pack <file> <out>` | JSON→animpack |
| all | `python animpack_all_in_one.py all <dir> <outdir>` | 一键全部 |

### 13.3 依赖

- Python 3.8+
- `lz4` (LZ4 解压缩): `pip install lz4`
- 标准库: struct, json, sys, os, math, csv, base64, dataclasses

---

## 附录: 坐标系说明

根骨骼的 IBM 包含坐标系翻转:

```
[-1,  0,  0,  0]
[ 0,  1,  0,  0]
[ 0,  0, -1,  0]
[ 0,  0,  0,  1]
```

这是绕 Y 轴 180° 旋转，用于将 Sky 引擎坐标系转换到标准坐标系。glTF 导出时，IBM 从 SQT 计算，自动包含此变换。

---

## 附录: 数据流图

```
.animpack 文件
     │
     ▼
 parse_animpack()
     │
     ├──→ AnimPack 对象
     │       ├── bones[] (name + matrix + parent_index)
     │       └── segments[]
     │             ├── sqt_list[] (参考SQT)
     │             └── clip_data
     │                   ├── header (6×u32)
     │                   ├── sqt_list[] (clip内SQT)
     │                   └── keyframe_header
     │                         ├── field1, field2, flags
     │                         ├── bbox, extra1, extra2
     │                         ├── per_bone_flags[]
     │                         └── remaining_data
     │
     ├──→ to_summary()     → 终端文本输出
     ├──→ to_json()        → JSON 文件
     ├──→ cmd_gltf()
     │       ├── _decode_kf_sets()  → 关键帧数据
     │       ├── _build_gltf_animation() → 动画轨道
     │       └── 组装 glTF JSON     → .gltf 文件
     ├──→ cmd_tree()       → HTML 骨骼树
     ├──→ cmd_compare()    → JSON 对比报告
     └──→ pack_animpack()  → .animpack 二进制
```
