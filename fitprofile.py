"""FIT 协议配置（本地 profile 子集）。

只登记本采集器能解释的消息与字段；未登记的字段一律视为未知字段，
由解码器保留字段号与原始字节，绝不按位置猜测含义（例如猜成心率）。
字段元组为 ``(名称, 缩放, 偏移, 单位, 枚举表)``，缩放/偏移遵循 FIT 公式
``实际值 = 原始值 / 缩放 - 偏移``。
"""

# 全局消息号 -> 消息名
MESG_NAMES = {
    0: 'file_id',
    18: 'session',
    19: 'lap',
    20: 'record',
    21: 'event',
    206: 'field_description',
    207: 'developer_data_id',
}

# 枚举表（只收录演练夹具用到的取值，其余保留数字）
ENUMS = {
    'file_type': {0: 'device', 1: 'settings', 2: 'sport', 3: 'activity', 4: 'activity'},
    'event': {0: 'timer', 3: 'workout', 36: 'course_point'},
    'event_type': {0: 'start', 1: 'stop', 2: 'consecutive_start', 3: 'consecutive_stop', 4: 'marker'},
    'sport': {0: 'generic', 1: 'running', 2: 'cycling'},
}

# 全局消息号 -> {字段定义号: (名称, 缩放, 偏移, 单位, 枚举表或 None)}
FIELDS = {
    0: {  # file_id
        0: ('type', None, None, None, 'file_type'),
        1: ('manufacturer', None, None, None, None),
        2: ('product', None, None, None, None),
        3: ('serial_number', None, None, None, None),
        4: ('time_created', None, None, 's', None),
        5: ('number', None, None, None, None),
        7: ('product_name', None, None, None, None),
    },
    18: {  # session
        253: ('timestamp', None, None, 's', None),
        2: ('start_time', None, None, 's', None),
        5: ('sport', None, None, None, 'sport'),
        7: ('total_elapsed_time', 1000, None, 's', None),
        8: ('total_timer_time', 1000, None, 's', None),
        9: ('total_distance', 100, None, 'm', None),
        11: ('total_calories', None, None, 'kcal', None),
        13: ('avg_heart_rate', None, None, 'bpm', None),
        16: ('avg_heart_rate', None, None, 'bpm', None),
    },
    19: {  # lap
        253: ('timestamp', None, None, 's', None),
        2: ('start_time', None, None, 's', None),
        7: ('total_elapsed_time', 1000, None, 's', None),
        8: ('total_timer_time', 1000, None, 's', None),
        9: ('total_distance', 100, None, 'm', None),
        13: ('avg_heart_rate', None, None, 'bpm', None),
    },
    20: {  # record
        253: ('timestamp', None, None, 's', None),
        0: ('position_lat', 11930465, None, 'deg', None),
        1: ('position_long', 11930465, None, 'deg', None),
        2: ('altitude', 5, 500, 'm', None),
        3: ('heart_rate', None, None, 'bpm', None),
        4: ('cadence', None, None, 'rpm', None),
        5: ('distance', 100, None, 'm', None),
        6: ('speed', 1000, None, 'm/s', None),
        7: ('power', None, None, 'W', None),
        13: ('temperature', None, None, 'degC', None),
    },
    21: {  # event
        253: ('timestamp', None, None, 's', None),
        0: ('event', None, None, None, 'event'),
        1: ('event_type', None, None, None, 'event_type'),
        3: ('data', None, None, None, None),
        4: ('event_group', None, None, None, None),
    },
    206: {  # field_description（开发者字段自述）
        0: ('developer_data_index', None, None, None, None),
        1: ('field_definition_number', None, None, None, None),
        2: ('fit_base_type_id', None, None, None, None),
        3: ('field_name', None, None, None, None),
        6: ('scale', None, None, None, None),
        7: ('offset', None, None, None, None),
        8: ('units', None, None, None, None),
        14: ('native_mesg_num', None, None, None, None),
        15: ('native_field_num', None, None, None, None),
    },
    207: {  # developer_data_id
        0: ('developer_data_index', None, None, None, None),
        1: ('application_id', None, None, None, None),
        2: ('manufacturer_id', None, None, None, None),
        3: ('application_version', None, None, None, None),
    },
}


def field_spec(global_mesg, field_number):
    """返回字段配置元组；未登记字段返回 None，调用方必须按未知字段处理。"""
    return FIELDS.get(global_mesg, {}).get(field_number)


def enum_name(table, value):
    if table is None or not isinstance(value, int):
        return None
    return ENUMS.get(table, {}).get(value)
