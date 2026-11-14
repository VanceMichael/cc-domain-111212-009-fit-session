"""训练采集业务层：消息关联、事件证据、缺失区间与稳定标识。

关联不按文件顺序等分：record 按时间戳落入 lap 的 [start_time, timestamp]
时间窗，lap 按同一规则落入 session；落不进任何窗口的消息标为 orphan，
并产生带字节偏移证据的事件。同一文件重复接收时，所有标识仅由文件摘要与
消息定位偏移推导，因此事件可被稳定识别（幂等）。
"""

import hashlib

from fitcodec import parse

GLOBAL = dict(SESSION=18, LAP=19, RECORD=20, EVENT=21)


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _field_value(message, field_number, dev=False):
    pool = message['dev_fields'] if dev else message['fields']
    for field in pool:
        if field['number'] == field_number and not field['invalid']:
            return field['value']
    return None


def _window(message):
    """返回消息的 (开始时间, 结束时间)；缺 start_time 时用 timestamp。"""
    end = message.get('timestamp')
    start = _field_value(message, 2)
    if start is None:
        start = end
    return start, end


def _stable_id(digest, kind, *parts):
    material = '|'.join([digest, kind] + [str(part) for part in parts])
    return hashlib.sha256(material.encode('utf-8')).hexdigest()[:16]


def _evidence(message):
    """事件的字节定位证据：消息偏移、本地/全局消息号与时间戳来源。"""
    return dict(message_offset=message['offset'], local_mesg=message['local_mesg'],
                global_mesg=message['global_mesg'], mesg_name=message['name'],
                timestamp_evidence=message.get('timestamp_evidence'))


def collect(data):
    """解码一份 FIT 文档并构建训练视图。

    返回 dict：complete / digest / header / errors / trainings / preview /
    orphan_messages。complete=False 时只生成 preview，不进入完整训练列表。
    """
    decoded = parse(data)
    digest = _digest(data)
    data_messages = [m for m in decoded['messages'] if m.get('kind') == 'data']
    sessions = [m for m in data_messages if m['global_mesg'] == GLOBAL['SESSION']]
    laps = [m for m in data_messages if m['global_mesg'] == GLOBAL['LAP']]
    records = [m for m in data_messages if m['global_mesg'] == GLOBAL['RECORD']]
    events_raw = [m for m in data_messages if m['global_mesg'] == GLOBAL['EVENT']]

    # ---- record -> lap：按时间窗归属，不按顺序切分 ----
    lap_windows = [(_window(lap), lap) for lap in laps]
    orphan_records = []
    for record in records:
        ts = record.get('timestamp')
        owner = None
        if ts is not None:
            for (start, end), lap in lap_windows:
                if start is not None and end is not None and start <= ts <= end:
                    owner = lap
                    break
        record['_lap_offset'] = owner['offset'] if owner else None
        if owner is None:
            orphan_records.append(record)

    # ---- lap -> session：同样按时间窗归属 ----
    session_windows = [(_window(session), session) for session in sessions]
    orphan_laps = []
    for lap in laps:
        start, end = _window(lap)
        owner = None
        if start is not None:
            for (s_start, s_end), session in session_windows:
                if s_start is not None and s_end is not None \
                        and s_start <= start and (s_end is None or end <= s_end):
                    owner = session
                    break
        lap['_session_offset'] = owner['offset'] if owner else None
        if owner is None:
            orphan_laps.append(lap)

    trainings = []
    events = _build_events(digest, sessions, laps, records, events_raw,
                           orphan_records, orphan_laps, decoded)

    if decoded['complete']:
        for index, session in enumerate(sessions):
            start, end = _window(session)
            session_laps = [lap for lap in laps if lap.get('_session_offset') == session['offset']]
            lap_offsets = {lap['offset'] for lap in session_laps}
            session_records = [record for record in records
                               if record.get('_lap_offset') in lap_offsets]
            training_id = _stable_id(digest, 'session', session['offset'])
            trainings.append(dict(
                training_id=training_id,
                start_time=start, end_time=end,
                summary=_summary(session),
                session_offset=session['offset'],
                laps=[dict(lap_offset=lap['offset'], window=list(_window(lap)),
                           total_distance=_field_value(lap, 9),
                           avg_heart_rate=_field_value(lap, 13),
                           record_count=sum(1 for r in records
                                            if r.get('_lap_offset') == lap['offset']))
                      for lap in session_laps],
                record_count=len(session_records),
                record_offsets=[record['offset'] for record in session_records],
                event_ids=[event['event_id'] for event in events
                           if event.get('session_offset') == session['offset']],
                evidence=_evidence(session)))

    preview = None
    if not decoded['complete']:
        preview = _build_preview(digest, decoded, records, laps, sessions, events)

    return dict(complete=decoded['complete'], digest=digest, header=decoded['header'],
                errors=decoded['errors'], file_crc_ok=decoded['file_crc_ok'],
                trainings=trainings, preview=preview, events=events,
                orphan_messages=[_evidence(m) for m in orphan_records + orphan_laps],
                counts=dict(session=len(sessions), lap=len(laps), record=len(records),
                            event=len(events_raw),
                            definition=sum(1 for m in decoded['messages']
                                           if m.get('kind') == 'definition')))


def _summary(session):
    return dict(sport=_field_value(session, 5),
                total_elapsed_time=_field_value(session, 7),
                total_timer_time=_field_value(session, 8),
                total_distance=_field_value(session, 9),
                total_calories=_field_value(session, 11),
                avg_heart_rate=_field_value(session, 13))


def _build_events(digest, sessions, laps, records, events_raw, orphan_records,
                  orphan_laps, decoded):
    events = []

    def add(kind, message, detail=None, session_offset=None):
        event = dict(
            event_id=_stable_id(digest, kind, message['offset'],
                                message.get('timestamp')),
            kind=kind, timestamp=message.get('timestamp'),
            session_offset=session_offset, evidence=_evidence(message))
        if detail:
            event['detail'] = detail
        events.append(event)
        return event

    for session in sessions:
        add('session_start', session,
            detail=dict(start_time=_field_value(session, 2)),
            session_offset=session['offset'])
        add('session_end', session, session_offset=session['offset'])
    for lap in laps:
        add('lap_marker', lap, detail=dict(window=_window(lap)),
            session_offset=lap.get('_session_offset'))
    for raw in events_raw:
        add('device_event', raw,
            detail=dict(event=_field_value(raw, 0),
                        event_type=_field_value(raw, 1),
                        data=_field_value(raw, 3)))
    for record in orphan_records:
        add('orphan_record', record,
            detail=dict(reason='timestamp 不属于任何 lap 时间窗'))
    for lap in orphan_laps:
        add('orphan_lap', lap, detail=dict(reason='时间窗不属于任何 session'))

    # ---- 缺失区间：相邻 record 时间不连续即形成闭区间缺口 ----
    timed = sorted((r for r in records if r.get('timestamp') is not None),
                   key=lambda item: item['timestamp'])
    for previous, current in zip(timed, timed[1:]):
        gap = current['timestamp'] - previous['timestamp']
        if gap > 1:
            events.append(dict(
                event_id=_stable_id(digest, 'missing_interval', previous['offset'],
                                    current['offset']),
                kind='missing_interval',
                interval=[previous['timestamp'] + 1, current['timestamp'] - 1],
                gap_seconds=gap,
                evidence=dict(from_offset=previous['offset'],
                              to_offset=current['offset'],
                              from_timestamp_evidence=previous.get('timestamp_evidence'),
                              to_timestamp_evidence=current.get('timestamp_evidence')),
                detail=dict(reason='相邻轨迹点时间戳不连续，区间内无测量')))

    # ---- 无效测量事件：保留定位，不参与数值统计 ----
    for record in records:
        invalid_fields = [dict(number=f['number'], name=f['name'], raw_hex=f['raw_hex'])
                          for f in record['fields'] if f['invalid']]
        invalid_dev = [dict(number=f['number'], raw_hex=f['raw_hex'],
                            description=f['description'])
                       for f in record['dev_fields'] if f['invalid']]
        if invalid_fields or invalid_dev:
            events.append(dict(
                event_id=_stable_id(digest, 'invalid_measurement', record['offset']),
                kind='invalid_measurement', timestamp=record.get('timestamp'),
                evidence=_evidence(record),
                detail=dict(invalid_fields=invalid_fields,
                            invalid_dev_fields=invalid_dev,
                            reason='字段等于协议无效值，已保留原始字节')))

    if decoded['truncated']:
        events.append(dict(
            event_id=_stable_id(digest, 'truncated', decoded['header']['payload_size']),
            kind='truncated', evidence=dict(errors=decoded['errors']),
            detail=dict(reason='文件截断，只能提供不完整预览')))
    return events


def _build_preview(digest, decoded, records, laps, sessions, events):
    timed = [r.get('timestamp') for r in records if r.get('timestamp') is not None]
    return dict(preview_id=_stable_id(digest, 'preview'),
                incomplete=True,
                reason='；'.join(error['reason'] for error in decoded['errors']),
                header=decoded['header'],
                parsed_counts=dict(session=len(sessions), lap=len(laps),
                                   record=len(records)),
                time_span=[min(timed), max(timed)] if timed else None,
                last_message_offset=max((m['offset'] for m in decoded['messages']),
                                        default=None),
                event_ids=[event['event_id'] for event in events])
