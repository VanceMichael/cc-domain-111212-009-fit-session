import struct

def crc16(data):
    value = 0
    for byte in data:
        value ^= byte
        for bit in range(8):
            value = (value >> 1) ^ (0xA001 if value & 1 else 0)
    return value

def inspect_bytes(data):
    if len(data) < 14 or data[8:12] != b'.FIT':
        raise ValueError('FIT 文件头无效')
    header_size = data[0]
    if header_size not in (12, 14):
        raise ValueError('不支持的 FIT 头长度')
    payload_size = struct.unpack_from('<I', data, 4)[0]
    if len(data) != header_size + payload_size + 2:
        raise ValueError('FIT 数据长度不完整')
    if header_size == 14 and crc16(data[:14]) != 0:
        raise ValueError('FIT 头校验失败')
    if crc16(data) != 0:
        raise ValueError('FIT 文件校验失败')
    return [{'header_size': header_size, 'payload_size': payload_size,
             'protocol_version': data[1], 'profile_version': struct.unpack_from('<H', data, 2)[0]}]
