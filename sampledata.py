import struct
from source import crc16

def fit_file(payload):
    header = struct.pack('<BBHI4s', 14, 0x20, 2100, len(payload), b'.FIT')
    header += struct.pack('<H', crc16(header))
    document = header + payload
    return document + struct.pack('<H', crc16(document))

def samples():
    definition = bytes([0x40, 0, 0]) + struct.pack('<H', 20) + bytes([2, 253, 4, 0x86, 3, 1, 0x02])
    record = bytes([0]) + struct.pack('<I', 1000000030) + bytes([72])
    compressed = bytes([0x80 | 1, 74])
    redefinition = bytes([0x40, 0, 0]) + struct.pack('<H', 20) + bytes([1, 253, 4, 0x86])
    document = fit_file(definition + record + compressed + redefinition + bytes([0]) + struct.pack('<I', 1000000066))
    return {'sample.fit': document, 'invalid.fit': document[:-2],
            'corrupt.fit': document[:-3] + bytes([document[-3] ^ 1]) + document[-2:]}
