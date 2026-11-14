"""按文件内定义逐条解码的 FIT 采集解码器。

不依赖任何固定字段位置：每条数据消息都按其本地消息号当前生效的定义消息
（含架构端序、字段列表、开发者字段列表）读取。本地消息号一旦被新的定义
消息重新定义，旧布局立即失效，后续数据按新布局解码。

时间戳：
- 普通头数据消息按定义读取字段 253（uint32 秒）；
- 压缩时间头不含时间字段，以上一条同本地消息号的参考时间戳为基准，
  用头部 5 位偏移做模 2**32 回绕恢复，并更新该本地消息号的参考戳。

未知标准字段与未知开发者字段均保留字段号、基础类型与原始字节；
开发者字段若有 field_description 自述则保留名称/单位/缩放等描述，
没有自述的开发者字段不做任何标准字段猜测（例如不会被当成心率）。
"""

import struct

from fitprofile import MESG_NAMES, field_spec, enum_name

# 基础类型号 -> (尺寸, struct 格式字符, 无效值)
# FIT SDK 规定的固定无效值；浮点无效值是 NaN，需用不等自比较判定。
BASE_TYPES = {
    0x00: (1, 'B', 0xFF),        # enum
    0x01: (1, 'b', 0x7F),        # sint8
    0x02: (1, 'B', 0xFF),        # uint8
    0x83: (2, 'h', 0x7FFF),      # sint16
    0x84: (2, 'H', 0xFFFF),      # uint16
    0x85: (4, 'i', 0x7FFFFFFF),  # sint32
    0x86: (4, 'I', 0xFFFFFFFF),  # uint32
    0x07: (1, 's', None),        # string（无效判定为全 0x00）
    0x88: (4, 'f', float('nan')),
    0x89: (8, 'd', float('nan')),
    0x0A: (1, 'B', 0xFF),        # uint8z
    0x8B: (2, 'H', 0xFFFF),      # uint16z
    0x8C: (4, 'I', 0xFFFFFFFF),  # uint32z
    0x0D: (1, 'b', 0x7F),        # byte
    0x8E: (8, 'q', 0x7FFFFFFFFFFFFFFF),  # sint64
    0x8F: (8, 'Q', 0xFFFFFFFFFFFFFFFF),  # uint64
    0x90: (8, 'Q', 0xFFFFFFFFFFFFFFFF),  # uint64z
}

HEADER_SIZE_OK = (12, 14)
COMPRESSED_BIT = 0x80
DEVELOPER_BIT = 0x20
WRAP_MOD = 1 << 32

# field_description.fit_base_type_id 使用 SDK 枚举编号。注意多字节类型的
# 枚举号（如 uint16=132、sint16=131）与定义消息中的位编码（0x84/0x83）数值相同，
# 但 8/16/64 位类型编码不同，必须经此表显式翻译，不能直接当 BASE_TYPES 键用。
FIT_BASE_TYPE_ID = {
    0: 0x00,    # enum
    1: 0x01,    # sint8
    2: 0x02,    # uint8
    131: 0x83,  # sint16
    132: 0x84,  # uint16
    133: 0x85,  # sint32
    134: 0x86,  # uint32
    7: 0x07,    # string
    136: 0x88,  # float32
    137: 0x89,  # float64
    10: 0x0A,   # uint8z
    139: 0x8B,  # uint16z
    140: 0x8C,  # uint32z
    13: 0x0D,   # byte
    142: 0x8E,  # sint64
    143: 0x8F,  # uint64
    144: 0x90,  # uint64z
}


def crc16(data):
    value = 0
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ (0xA001 if value & 1 else 0)
    return value


def _is_invalid(raw_values, base_type):
    """按基础类型判定原始值是否等于协议无效值。"""
    kind = base_type & 0x0F
    if kind == 7:  # string：全 NUL 视为无效
        return all(item == b'\x00' for item in raw_values)
    invalid = BASE_TYPES[base_type][2]
    if isinstance(invalid, float):
        return all(isinstance(item, float) and item != item for item in raw_values)
    return all(item == invalid for item in raw_values)


def _parse_header(data):
    if len(data) < 12 or data[8:12] != b'.FIT':
        raise ValueError('FIT 文件头无效：缺少 .FIT 签名')
    header_size = data[0]
    if header_size not in HEADER_SIZE_OK:
        raise ValueError('不支持的 FIT 头长度：%r' % header_size)
    if len(data) < header_size:
        raise ValueError('FIT 文件头不完整')
    protocol_version = data[1]
    profile_version = struct.unpack_from('<H', data, 2)[0]
    payload_size = struct.unpack_from('<I', data, 4)[0]
    header_crc_ok = True
    if header_size == 14:
        header_crc_ok = crc16(data[:14]) == 0
    return dict(header_size=header_size, protocol_version=protocol_version,
                profile_version=profile_version, payload_size=payload_size,
                header_crc_ok=header_crc_ok)


def _parse_definition(data, abs_cursor, header):
    """abs_cursor 为文件绝对偏移；返回 (定义, 新的文件绝对偏移)。"""
    start = abs_cursor
    cursor = abs_cursor + 1  # reserved
    architecture = data[cursor]
    if architecture not in (0, 1):
        raise ValueError('不支持的字节架构：%r（偏移 %d）' % (architecture, start))
    endian = '<' if architecture == 0 else '>'
    cursor += 1
    global_mesg = struct.unpack_from(endian + 'H', data, cursor)[0]
    cursor += 2
    field_count = data[cursor]
    cursor += 1
    fields = []
    for _ in range(field_count):
        number, size, base_type = data[cursor], data[cursor + 1], data[cursor + 2]
        fields.append(dict(number=number, size=size, base_type=base_type))
        cursor += 3
    dev_fields = []
    if header & DEVELOPER_BIT:
        dev_count = data[cursor]
        cursor += 1
        for _ in range(dev_count):
            # 开发者字段定义第三字节是 developer_data_index，不是基础类型；
            # 基础类型由 field_description 消息的 fit_base_type_id 给出。
            field_number, size, dev_index = data[cursor], data[cursor + 1], data[cursor + 2]
            dev_fields.append(dict(number=field_number, size=size, dev_data_index=dev_index))
            cursor += 3
    definition = dict(local_mesg=header & 0x0F, architecture=architecture, endian=endian,
                      global_mesg=global_mesg, fields=fields, dev_fields=dev_fields,
                      defined_at=start)
    # 返回从记录头之后到定义结束的消耗字节数（不含调用方已读取的 head 字节）。
    return definition, cursor - abs_cursor


def _read_value(data, cursor, size, base_type, endian):
    """按架构端序解码一个字段；size 为类型尺寸倍数时按数组返回。"""
    if base_type not in BASE_TYPES:
        return None, data[cursor:cursor + size].hex(), cursor + size
    type_size, fmt_char, _ = BASE_TYPES[base_type]
    count = max(1, size // type_size)
    raw_values, raw_hex = [], data[cursor:cursor + size]
    for _ in range(count):
        if fmt_char == 's':
            raw_values.append(data[cursor:cursor + type_size])
        else:
            raw_values.append(struct.unpack_from(endian + fmt_char, data, cursor)[0])
        cursor += type_size
    return raw_values, raw_hex.hex(), cursor


def _decode_raw(raw_values, base_type):
    """按基础类型还原原始数值（字符串解码、数组保留为列表），不做业务缩放。"""
    kind = base_type & 0x0F
    if kind == 7:
        return b''.join(raw_values).split(b'\x00', 1)[0].decode('utf-8', 'replace')
    return raw_values[0] if len(raw_values) == 1 else list(raw_values)


def _apply_profile(global_mesg, field_number, raw_values, base_type):
    spec = field_spec(global_mesg, field_number)
    kind = base_type & 0x0F
    if kind == 7:  # string：截断到首个 NUL 再解码
        text = b''.join(raw_values).split(b'\x00', 1)[0]
        value = text.decode('utf-8', 'replace')
        return value, (spec[0] if spec else None)
    scalar = len(raw_values) == 1
    decoded = raw_values[0] if scalar else list(raw_values)
    if spec is None:
        return decoded, None
    name, scale, offset, unit, enum_table = spec

    def convert(item):
        if enum_table is not None:
            return enum_name(enum_table, item)
        result = item
        if scale is not None:
            result = result / scale
        if offset is not None:
            result = result - offset
        return result

    return (convert(decoded) if scalar else [convert(item) for item in decoded]), name


def parse(data):
    """解码整份 FIT 文档。

    返回 dict：header / messages / complete / errors / file_crc_ok / truncated。
    截断或 CRC 失败不抛异常，而是在 errors 标注并置 complete=False，
    以便生成“明确标为不完整的预览”；文件头本身无法识别才抛 ValueError。
    """
    header = _parse_header(data)
    payload_start = header['header_size']
    payload_size = header['payload_size']
    declared_end = payload_start + payload_size
    messages = []
    errors = []
    # 本地消息号 -> 当前生效定义；重定义即覆盖，旧布局不再沿用。
    definitions = {}
    # 本地消息号 -> 最近参考时间戳（压缩时间戳恢复用）。
    reference_ts = {}
    # (developer_data_index, field_definition_number) -> 基础类型与自述描述。
    dev_catalog = {}
    # 解析游标统一使用“负载内相对偏移”，输出证据时再加文件头长度换算成绝对偏移。
    cursor = 0
    truncated = False

    while cursor < payload_size:
        if cursor >= len(data) - payload_start:
            truncated = True
            errors.append(dict(reason='payload_incomplete', declared_end=declared_end,
                               actual_size=len(data)))
            break
        record_offset = cursor
        head = data[payload_start + cursor]
        cursor += 1

        if head & COMPRESSED_BIT:
            local_mesg = (head >> 5) & 0x03
            time_offset = head & 0x1F
            definition = definitions.get(local_mesg)
            if definition is None:
                truncated = True
                errors.append(dict(reason='missing_definition', local_mesg=local_mesg,
                                   offset=payload_start + record_offset))
                break
            try:
                message, cursor, fault = _decode_data(
                    data, payload_start, cursor, definition, record_offset,
                    time_offset, reference_ts, dev_catalog)
            except (IndexError, struct.error):
                truncated = True
                errors.append(dict(reason='truncated_data_field',
                                   offset=payload_start + record_offset))
                break
            if fault:
                truncated = True
                errors.append(dict(reason=fault,
                                   offset=payload_start + record_offset))
                break
            messages.append(message)
            continue

        local_mesg = head & 0x0F
        if head & 0x40:  # 定义消息
            try:
                # head 字节已计入 cursor；返回值是 head 之后的定义体长度，需累加。
                definition, consumed = _parse_definition(
                    data, payload_start + cursor, head)
                cursor += consumed
            except (IndexError, struct.error, ValueError):
                truncated = True
                errors.append(dict(reason='truncated_definition',
                                   offset=payload_start + record_offset))
                break
            definition['local_mesg'] = local_mesg
            # 重定义：覆盖旧布局；压缩时间戳参考值随布局重置，避免跨布局串用。
            definitions[local_mesg] = definition
            reference_ts.pop(local_mesg, None)
            messages.append(dict(kind='definition',
                                 offset=payload_start + record_offset,
                                 local_mesg=local_mesg, global_mesg=definition['global_mesg'],
                                 architecture=definition['architecture'],
                                 field_count=len(definition['fields']),
                                 dev_field_count=len(definition['dev_fields']),
                                 name=MESG_NAMES.get(definition['global_mesg'],
                                                     'unknown_%d' % definition['global_mesg'])))
            continue

        definition = definitions.get(local_mesg)
        if definition is None:
            truncated = True
            errors.append(dict(reason='missing_definition', local_mesg=local_mesg,
                               offset=payload_start + record_offset))
            break
        try:
            message, cursor, fault = _decode_data(
                data, payload_start, cursor, definition, record_offset, None,
                reference_ts, dev_catalog)
        except (IndexError, struct.error):
            truncated = True
            errors.append(dict(reason='truncated_data_field',
                               offset=payload_start + record_offset))
            break
        if fault:
            truncated = True
            errors.append(dict(reason=fault,
                               offset=payload_start + record_offset))
            break
        messages.append(message)
        if message['global_mesg'] == 206:  # field_description：登记开发者字段自述
            _register_dev_description(message, dev_catalog)

    if not truncated and payload_start + cursor < declared_end:
        truncated = True
        errors.append(dict(reason='payload_incomplete', declared_end=declared_end,
                           parsed_end=payload_start + cursor))

    # 文件 CRC：尾部两字节缺失则无法校验；截断文件给不出可信 CRC。
    file_crc_ok = None
    if len(data) >= declared_end + 2:
        stored_crc = struct.unpack_from('<H', data, declared_end)[0]
        file_crc_ok = crc16(data[:declared_end]) == stored_crc
        if not file_crc_ok:
            errors.append(dict(reason='file_crc_mismatch', offset=declared_end))
    else:
        errors.append(dict(reason='file_crc_missing', offset=min(declared_end, len(data))))
    if not header['header_crc_ok']:
        errors.append(dict(reason='header_crc_mismatch'))

    _attach_dev_descriptions(messages, dev_catalog)
    complete = (not truncated) and file_crc_ok is True and header['header_crc_ok']
    return dict(header=header, messages=messages, complete=complete, errors=errors,
                file_crc_ok=file_crc_ok, truncated=truncated)


def _decode_data(data, payload_start, rel_cursor, definition, record_offset,
                 compressed_offset, reference_ts, dev_catalog):
    """rel_cursor / record_offset 均为负载内相对偏移；消息证据偏移换算成绝对偏移。

    返回 (message, 新的负载内相对游标, fault)。
    """
    endian = definition['endian']
    fields_out = []
    dev_fields_out = []
    local_mesg = definition['local_mesg']
    global_mesg = definition['global_mesg']
    rel = rel_cursor
    payload_bytes = len(data) - payload_start

    def read(size, base_type):
        nonlocal rel
        if rel + size > payload_bytes:
            return None, data[payload_start + rel:].hex()
        raw_values, raw_hex, _ = _read_value(
            data, payload_start + rel, size, base_type, endian)
        rel += size
        return raw_values, raw_hex

    for item in definition['fields']:
        # 压缩时间头消息不含字段 253 的数据（FIT 规定），跳过读取后合成。
        if compressed_offset is not None and item['number'] == 253:
            continue
        raw_values, raw_hex = read(item['size'], item['base_type'])
        field = dict(number=item['number'], base_type=item['base_type'],
                     size=item['size'], raw_hex=raw_hex,
                     field_offset=payload_start + rel - item['size'])
        if raw_values is None or _is_invalid(raw_values, item['base_type']):
            field.update(invalid=True, value=None, name=None)
        else:
            value, name = _apply_profile(global_mesg, item['number'], raw_values,
                                         item['base_type'])
            field.update(invalid=False, value=value, name=name)
        fields_out.append(field)

    for item in definition['dev_fields']:
        key = (item['dev_data_index'], item['number'])
        catalog = dev_catalog.get(key)
        # catalog 存 SDK 枚举号，这里映射为内部基础类型键；无自述则为 None。
        base_type = FIT_BASE_TYPE_ID.get(catalog['base_type_id']) if catalog else None
        raw_values, raw_hex = read(
            item['size'], base_type if base_type is not None else 0x0D)
        field = dict(number=item['number'], dev_data_index=item['dev_data_index'],
                     base_type=base_type, size=item['size'], raw_hex=raw_hex,
                     field_offset=payload_start + rel - item['size'], description=None)
        if raw_values is None:
            return None, rel, 'truncated_dev_field'
        if base_type is not None and _is_invalid(raw_values, base_type):
            field.update(invalid=True, value=None, name=None)
        elif base_type is None:
            # 自述尚未出现（异常顺序）：只保留原始字节，后置 pass 再尝试解码。
            field.update(invalid=False, value=None, name=None, raw_only=True)
        else:
            # 开发者字段绝不套用标准消息 profile（不能猜成心率/位置等），
            # 仅按基础类型解码原始值；名称与缩放只来自 field_description 自述。
            field.update(invalid=False, value=_decode_raw(raw_values, base_type),
                         name=None)
        dev_fields_out.append(field)

    # 时间戳恢复：压缩头用 5 位偏移 + 参考戳做模 2^32 回绕；
    # 普通消息中的字段 253 成为该本地消息号的新参考戳。
    timestamp = None
    timestamp_evidence = None
    standard_ts = next((f for f in fields_out if f['number'] == 253 and not f['invalid']),
                       None)
    if standard_ts is not None:
        timestamp = standard_ts['value']
        reference_ts[local_mesg] = timestamp
        timestamp_evidence = 'field'
    if compressed_offset is not None:
        base = reference_ts.get(local_mesg)
        if base is None:
            return None, rel, 'compressed_timestamp_without_reference'
        # FIT SDK 语义：新戳高位沿用参考戳，低 5 位取头部偏移；当参考戳低 5 位
        # 大于偏移时，5 位计数器已回绕，整体先加 32 秒（即高 5 位进位）。
        rolled = (base & 0x1F) > compressed_offset
        timestamp = (base - (base & 0x1F) + compressed_offset
                     + (32 if rolled else 0)) % WRAP_MOD
        reference_ts[local_mesg] = timestamp
        timestamp_evidence = 'compressed_header'
        # 压缩头不含时间字段，补一条合成证据字段，下游统一按时间关联。
        fields_out.append(dict(number=253, base_type=0x86, size=4, raw_hex=None,
                               field_offset=None, invalid=False, value=timestamp,
                               name='timestamp', synthesized=True))
    else:
        # 普通头的时间字段来自实际字节，统一标记为非合成以便测试与审计。
        for field in fields_out:
            field.setdefault('synthesized', False)

    message = dict(kind='data', offset=payload_start + record_offset,
                   local_mesg=local_mesg,
                   global_mesg=global_mesg,
                   name=MESG_NAMES.get(global_mesg, 'unknown_%d' % global_mesg),
                   architecture=definition['architecture'], fields=fields_out,
                   dev_fields=dev_fields_out, timestamp=timestamp,
                   timestamp_evidence=timestamp_evidence)
    return message, rel, None


def _register_dev_description(message, dev_catalog):
    values = {f['number']: f['value'] for f in message['fields'] if not f['invalid']}
    dev_index = values.get(0)
    field_number = values.get(1)
    if dev_index is None or field_number is None:
        return
    type_id = values.get(2)
    dev_catalog[(dev_index, field_number)] = dict(
        # 保留 SDK 枚举号；消费方通过 FIT_BASE_TYPE_ID 映射成内部基础类型键。
        base_type_id=type_id, field_name=values.get(3), units=values.get(8),
        scale=values.get(6), offset=values.get(7),
        native_mesg_num=values.get(14), native_field_num=values.get(15),
        described_at=message['offset'])


def _attach_dev_descriptions(messages, dev_catalog):
    """把 field_description 自述挂到每条数据消息的开发者字段上。

    没有自述的开发者字段保持 description=None 与原始字节，绝不映射成
    任何标准字段（例如心率）。若解码时自述尚未出现，在此用原始字节补解码。
    """
    for message in messages:
        if message.get('kind') != 'data':
            continue
        for field in message['dev_fields']:
            description = dev_catalog.get((field['dev_data_index'], field['number']))
            if description is None:
                continue
            field['description'] = {k: v for k, v in description.items()
                                    if k != 'base_type_id'}
            # 名称只取自述，绝不回退到标准消息 profile。
            field['name'] = description.get('field_name')
            if field.get('raw_only'):
                base_type = FIT_BASE_TYPE_ID.get(description['base_type_id'])
                if base_type in BASE_TYPES:
                    raw = bytes.fromhex(field['raw_hex'])
                    type_size, fmt_char, _ = BASE_TYPES[base_type]
                    endian = '<' if (base_type & 0x80) else '>'
                    values = [struct.unpack_from(endian + fmt_char, raw, pos)[0]
                              for pos in range(0, len(raw), type_size)] if fmt_char != 's' \
                        else [raw]
                    field['base_type'] = base_type
                    if not _is_invalid(values, base_type):
                        field['value'] = _decode_raw(values, base_type)
                field.pop('raw_only', None)
            _apply_dev_scale(field, description)


def _apply_dev_scale(field, description):
    if field['invalid'] or field['value'] is None:
        return
    scale, offset = description.get('scale'), description.get('offset')
    if isinstance(scale, (int, float)) and scale not in (0, 1):
        if isinstance(field['value'], list):
            field['value'] = [item / scale for item in field['value']]
        else:
            field['value'] = field['value'] / scale
    if isinstance(offset, (int, float)) and offset != 0:
        if isinstance(field['value'], list):
            field['value'] = [item - offset for item in field['value']]
        else:
            field['value'] = field['value'] - offset
