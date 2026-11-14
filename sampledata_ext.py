"""本地虚构设备 FIT 文件生成器（不连接真实手表或在线账号）。

``device_files()`` 构造一份覆盖以下情形的完整文件和两份对照文件：
- 同一本地消息号在文件中途被重新定义为另一种全局消息（旧布局不得沿用）；
- 混合小端/大端架构；
- 压缩时间戳在 5 位偏移上回绕（含跨 2^32 附近的回绕演示）；
- 数组型开发者字段、未知开发者字段与未知标准字段（保留原始字节，不猜心率）；
- 无效测量（uint8 0xFF / uint16 0xFFFF / uint32 0xFFFFFFFF）；
- 相邻轨迹点时间缺口，用于生成缺失区间事件；
- 截断文件与 CRC 损坏文件对照。

所有数值均为虚构，时间戳使用任意基准，生成过程纯标准库且可重复。
"""

import struct

from fitcodec import BASE_TYPES, crc16

DEVELOPER_BIT = 0x20

# 基础类型号别名，构造器按名称引用，避免散落魔法数字。
ENUM, SINT8, UINT8 = 0x00, 0x01, 0x02
SINT16, UINT16, SINT32, UINT32 = 0x83, 0x84, 0x85, 0x86
STRING = 0x07
BYTE = 0x0D

INVALID = {base: spec[2] for base, spec in BASE_TYPES.items()
           if not isinstance(spec[2], float)}

BASE_TS = 999999996  # 低 5 位为 28，前几条压缩消息即可演示回绕


def fit_file(payload):
    header = struct.pack('<BBHI4s', 14, 0x20, 2100, len(payload), b'.FIT')
    header += struct.pack('<H', crc16(header))
    document = header + payload
    return document + struct.pack('<H', crc16(document))


class DeviceBuilder:
    """按 FIT 记录头规则逐段构造文档，并记录每段字节偏移作为定位证据。"""

    def __init__(self):
        self.chunks = []
        self.segments = []  # (kind, 起始偏移, 长度)

    def _append(self, kind, blob):
        offset = sum(len(chunk) for chunk in self.chunks)
        self.chunks.append(blob)
        self.segments.append((kind, offset, len(blob)))
        return offset

    def definition(self, local_mesg, global_mesg, fields, dev_fields=(),
                   architecture=0):
        endian = '<' if architecture == 0 else '>'
        head = 0x40 | (DEVELOPER_BIT if dev_fields else 0) | (local_mesg & 0x0F)
        body = bytes([head, 0, architecture]) + struct.pack(endian + 'H', global_mesg)
        body += bytes([len(fields)])
        for number, size, base_type in fields:
            body += bytes([number, size, base_type])
        if dev_fields:
            body += bytes([len(dev_fields)])
            for number, size, dev_index in dev_fields:
                body += bytes([number, size, dev_index])
        return self._append('definition', body)

    def data(self, local_mesg, blob):
        return self._append('data', bytes([local_mesg & 0x0F]) + blob)

    def compressed(self, local_mesg, time_offset, blob):
        head = 0x80 | ((local_mesg & 0x03) << 5) | (time_offset & 0x1F)
        return self._append('data', bytes([head]) + blob)

    def payload(self):
        return b''.join(self.chunks)

    def document(self):
        return fit_file(self.payload())


def _pack(value, base_type, endian='<'):
    type_size, fmt_char, _ = BASE_TYPES[base_type]
    if fmt_char == 's':
        if isinstance(value, str):
            value = value.encode('utf-8')
        return value
    return struct.pack(endian + fmt_char, value)


def _build_activity():
    builder = DeviceBuilder()
    endian = '<'

    # ---- file_id（本地 0，小端）----
    builder.definition(0, 0, [
        (0, 1, ENUM), (1, 1, UINT8), (2, 2, UINT16),
        (3, 4, UINT32), (4, 4, UINT32), (7, 12, STRING)])
    blob = _pack(4, ENUM) + _pack(89, UINT8) + _pack(31337, UINT16) \
        + _pack(0xCAFEBABE, UINT32) + _pack(BASE_TS, UINT32) \
        + b'camp-gadget\x00'
    file_id_offset = builder.data(0, blob)

    # ---- developer_data_id 与两条 field_description（本地 3）----
    builder.definition(3, 207, [(0, 1, UINT8), (1, 16, BYTE)])
    builder.data(3, _pack(0, UINT8) + bytes(range(16)))
    builder.definition(3, 206, [
        (0, 1, UINT8), (1, 1, UINT8), (2, 1, UINT8), (3, 8, STRING),
        (6, 1, UINT8), (7, 1, SINT8), (8, 6, STRING)])
    desc1 = (_pack(0, UINT8) + _pack(0, UINT8) + _pack(UINT16, UINT8)
             + b'muscle\0\0' + _pack(2, UINT8) + _pack(0, SINT8) + b'pct\0\0\0')
    desc1_offset = builder.data(3, desc1)
    desc2 = (_pack(0, UINT8) + _pack(1, UINT8) + _pack(SINT16, UINT8)
             + b'imbalanc' + _pack(1, UINT8)
             + _pack(0, SINT8) + b'pct\0\0\0')
    builder.data(3, desc2)

    # ---- record 定义（本地 1，小端，含两个开发者字段）----
    record_defs = [
        (253, 4, UINT32), (0, 4, SINT32), (1, 4, SINT32), (2, 2, UINT16),
        (3, 1, UINT8), (4, 1, UINT8), (5, 4, UINT32), (6, 2, UINT16),
        (70, 1, UINT8),  # 未知标准字段：必须保留原始字节，不能猜成心率
    ]
    builder.definition(1, 20, record_defs, dev_fields=[
        (0, 4, 0), (1, 2, 0), (3, 1, 0)])  # 字段 0=uint16 数组、1=sint16；
        # 字段 3 在定义中声明，但不给 field_description 自述（未知开发者字段）。

    lat0, lng0 = 312700000, 1164000000
    # (时间戳, 心率, 踏频, 高度原始值, 速度原始值, 距离原始值, 标记)
    # 标记：invalid=注入无效测量；unknown_dev=追加未自述开发者字段；
    #       gap=与上一条拉开时间缺口。
    rows = [
        (BASE_TS + 0,  120, 80, 12500, 2780, 0,      set()),
        (BASE_TS + 1,  121, 81, 12510, 2790, 28,     {'unknown_dev'}),
        (BASE_TS + 2,  None, 82, 12520, 2800, 56,    {'invalid'}),
        (BASE_TS + 3,  123, 83, None, 2810, 84,      {'invalid'}),
        (BASE_TS + 4,  124, 84, 12540, 2820, 112,    set()),
        (BASE_TS + 5,  125, 85, 12550, None, 140,    {'invalid'}),
        (BASE_TS + 11, 126, 86, 12560, 2840, 308,    {'gap'}),
        (BASE_TS + 12, 127, 87, 12570, 2850, 336,    set()),
        (BASE_TS + 13, 128, 88, 12580, 2860, 364,    set()),
        (BASE_TS + 14, 129, 89, 12590, 2870, 392,    set()),
        (BASE_TS + 15, 130, 90, 12600, 2880, 420,    set()),
        (BASE_TS + 16, 131, 91, 12610, 2890, 448,    set()),
    ]
    record_offsets = []
    first = True
    for index, (ts, hr, cad, alt, speed, distance, flags) in enumerate(rows):
        # 开发者字段 0：uint16 双元素数组，按自述缩放 2；字段 1：sint16；
        # 字段 3：已声明但无自述，解码器只能保留原始字节，不能猜成心率。
        unknown_dev = 0x5A if 'unknown_dev' in flags else 0x30 + index
        dev_blob = struct.pack('<HH', 1000 + index, 1010 + index) \
            + struct.pack('<h', -5 + index) + _pack(unknown_dev, UINT8)
        standard = b''
        if first:
            standard += _pack(ts, UINT32)  # 首条普通头，带完整时间戳并建立参考戳
        standard += struct.pack('<ii', lat0 + index * 100, lng0 + index * 200)
        standard += _pack(alt if alt is not None else INVALID[UINT16], UINT16)
        standard += _pack(hr if hr is not None else INVALID[UINT8], UINT8)
        standard += _pack(cad, UINT8)
        standard += _pack(distance, UINT32)
        standard += _pack(speed if speed is not None else INVALID[UINT16], UINT16)
        standard += _pack(0x40 + index, UINT8)  # 未知标准字段 70 的原始字节
        if first:
            offset = builder.data(1, standard + dev_blob)
            first = False
        else:
            # 压缩时间头：时间戳压成 5 位，回绕由解码器按参考戳恢复。
            offset = builder.compressed(1, ts & 0x1F, standard + dev_blob)
        record_offsets.append((offset, ts))

    # ---- lap 重定义到本地 1（大端）：本地消息号 1 的 record 布局被覆盖 ----
    lap_defs_big = [(253, 4, UINT32), (2, 4, UINT32), (7, 4, UINT32),
                    (8, 4, UINT32), (9, 4, UINT32), (13, 1, UINT8)]
    lap1_def_offset = builder.definition(1, 19, lap_defs_big, architecture=1)
    lap1_blob = struct.pack('>IIIII', BASE_TS + 6, BASE_TS, 7000, 7000, 20000) \
        + _pack(124, UINT8)
    lap1_offset = builder.data(1, lap1_blob)
    lap2_blob = struct.pack('>IIIII', BASE_TS + 16, BASE_TS + 7, 10000, 10000, 24800) \
        + _pack(129, UINT8)
    lap2_offset = builder.data(1, lap2_blob)

    # ---- session 占用本地 2（小端）----
    session_defs = [(253, 4, UINT32), (2, 4, UINT32), (5, 1, ENUM),
                    (7, 4, UINT32), (8, 4, UINT32), (9, 4, UINT32),
                    (11, 2, UINT16), (13, 1, UINT8)]
    builder.definition(2, 18, session_defs)
    session_blob = struct.pack('<IIBIIIHB', BASE_TS + 16, BASE_TS, 1,
                               16000, 16000, 44800, 180, 127)
    session_offset = builder.data(2, session_blob)

    # ---- 本地消息号 1 再次重定义为 event（小端），验证大端 lap 布局不再沿用 ----
    event_defs = [(253, 4, UINT32), (0, 1, ENUM), (1, 1, ENUM), (3, 2, UINT16)]
    event_def_offset = builder.definition(1, 21, event_defs)
    event_blob = _pack(BASE_TS + 12, UINT32) + _pack(0, ENUM) + _pack(4, ENUM) \
        + _pack(1, UINT16)
    event_offset = builder.data(1, event_blob)

    return builder, dict(file_id_offset=file_id_offset, desc1_offset=desc1_offset,
                         record_offsets=record_offsets, lap1_def_offset=lap1_def_offset,
                         lap1_offset=lap1_offset, lap2_offset=lap2_offset,
                         session_offset=session_offset,
                         event_def_offset=event_def_offset, event_offset=event_offset)


def device_files():
    """返回 name -> (文档字节, 构造元数据)。"""
    builder, meta = _build_activity()
    document = builder.document()

    # 截断：丢掉最后一条完整数据消息（含其记录头），前面的消息全部可解析，
    # 预览能给出真实轨迹，同时明确标注不完整且不进入完整训练列表。
    last_kind, last_offset, last_length = builder.segments[-1]
    assert last_kind == 'data'
    truncated = document[:14 + last_offset]
    # CRC 损坏：翻转 record 数据区（非定义消息）的一个字节，保持结构可解析、
    # 仅文件 CRC 失败，以区分“结构截断”与“内容损坏”两类完整性问题。
    corrupt = bytearray(document)
    first_record_data = next(seg for seg in builder.segments
                             if seg[0] == 'data' and seg[1] > 150)
    corrupt[14 + first_record_data[1] + 4] ^= 0x01
    return {
        'device-activity.fit': (document, meta),
        'device-truncated.fit': (bytes(truncated), meta),
        'device-corrupt.fit': (bytes(corrupt), meta),
    }
