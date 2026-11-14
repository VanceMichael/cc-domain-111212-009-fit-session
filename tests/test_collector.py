"""定义驱动 FIT 采集器的业务测试。

夹具全部由 sampledata_ext 在本地虚构生成，不读取外部文件、不连接真实设备。
"""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import app
from collector import collect
from fitcodec import BASE_TYPES, FIT_BASE_TYPE_ID, parse
from sampledata_ext import BASE_TS, device_files
from scenario import scenario
from storage import Archive
from transport import ReplaySource, fetch


class CodecTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = {name: blob for name, (blob, _meta) in device_files().items()}
        cls.decoded = parse(cls.files['device-activity.fit'])

    def test_file_integrity_and_completeness(self):
        report = collect(self.files['device-activity.fit'])
        self.assertTrue(report['complete'])
        self.assertEqual(report['errors'], [])
        self.assertTrue(report['file_crc_ok'])
        self.assertFalse(collect(self.files['device-truncated.fit'])['complete'])
        corrupt = collect(self.files['device-corrupt.fit'])
        self.assertFalse(corrupt['complete'])
        self.assertTrue(
            any(e['reason'] == 'file_crc_mismatch' for e in corrupt['errors']))

    def test_local_message_redefinition_replaces_layout(self):
        # 本地消息号 1 依次承载 record(20, 小端) -> lap(19, 大端) -> event(21, 小端)
        definitions = [m for m in self.decoded['messages'] if m['kind'] == 'definition']
        local_one = [m for m in definitions if m['local_mesg'] == 1]
        self.assertEqual([m['global_mesg'] for m in local_one], [20, 19, 21])
        self.assertEqual([m['architecture'] for m in local_one], [0, 1, 0])
        # 重定义后的数据消息必须按各自全局消息解码，而不是沿用首个 record 布局。
        data_one = [m for m in self.decoded['messages']
                    if m.get('kind') == 'data' and m['local_mesg'] == 1]
        self.assertEqual([m['global_mesg'] for m in data_one][:2], [20, 20])
        self.assertIn(19, [m['global_mesg'] for m in data_one])
        self.assertIn(21, [m['global_mesg'] for m in data_one])

    def test_big_endian_lap_decoded_with_correct_endianness(self):
        lap = next(m for m in self.decoded['messages']
                   if m.get('kind') == 'data' and m['global_mesg'] == 19)
        self.assertEqual(lap['architecture'], 1)
        values = {f['number']: f['value'] for f in lap['fields']}
        self.assertEqual(values[253], BASE_TS + 6)       # 大端 uint32
        self.assertEqual(values[9], 200.0)               # 缩放 /100
        self.assertEqual(values[7], 7.0)                 # 缩放 /1000

    def test_compressed_timestamp_wraparound_recovered_monotonically(self):
        records = [m for m in self.decoded['messages']
                   if m.get('kind') == 'data' and m['global_mesg'] == 20]
        timestamps = [m['timestamp'] for m in records]
        expected = [BASE_TS + delta for delta in
                    [0, 1, 2, 3, 4, 5, 11, 12, 13, 14, 15, 16]]
        self.assertEqual(timestamps, expected)  # 低 5 位回绕后无时间倒退
        self.assertEqual(records[0]['timestamp_evidence'], 'field')
        self.assertTrue(all(m['timestamp_evidence'] == 'compressed_header'
                            for m in records[1:]))
        synthesized = next(f for f in records[1]['fields'] if f.get('synthesized'))
        self.assertEqual(synthesized['number'], 253)

    def test_scaling_and_offset_applied_to_standard_fields(self):
        record = next(m for m in self.decoded['messages']
                      if m.get('kind') == 'data' and m['global_mesg'] == 20)
        values = {f['name']: f['value'] for f in record['fields'] if f['name']}
        self.assertEqual(values['altitude'], 2000.0)       # 12500/5 - 500
        self.assertEqual(values['speed'], 2.78)            # 2780/1000
        self.assertAlmostEqual(values['position_lat'],
                               312700000 / 11930465, places=6)

    def test_invalid_values_marked_and_kept_as_raw_bytes(self):
        records = [m for m in self.decoded['messages']
                   if m.get('kind') == 'data' and m['global_mesg'] == 20]
        hr_invalid = next(f for f in records[2]['fields'] if f['number'] == 3)
        self.assertTrue(hr_invalid['invalid'])
        self.assertIsNone(hr_invalid['value'])
        self.assertEqual(hr_invalid['raw_hex'], 'ff')
        alt_invalid = next(f for f in records[3]['fields'] if f['number'] == 2)
        self.assertEqual(alt_invalid['raw_hex'], 'ffff')
        speed_invalid = next(f for f in records[5]['fields'] if f['number'] == 6)
        self.assertTrue(speed_invalid['invalid'])

    def test_array_developer_field_scaled_from_description(self):
        record = next(m for m in self.decoded['messages']
                      if m.get('kind') == 'data' and m['global_mesg'] == 20)
        muscle = next(f for f in record['dev_fields'] if f['number'] == 0)
        self.assertEqual(muscle['value'], [500.0, 505.0])  # [1000,1010]/2
        self.assertEqual(muscle['description']['field_name'], 'muscle')
        self.assertEqual(muscle['description']['units'], 'pct')
        imbalance = next(f for f in record['dev_fields'] if f['number'] == 1)
        self.assertEqual(imbalance['value'], -5)           # sint16 负值
        self.assertEqual(imbalance['description']['field_name'], 'imbalanc')

    def test_unknown_developer_field_keeps_raw_bytes_without_guessing(self):
        records = [m for m in self.decoded['messages']
                   if m.get('kind') == 'data' and m['global_mesg'] == 20]
        unknown = next(f for f in records[1]['dev_fields'] if f['number'] == 3)
        self.assertIsNone(unknown['description'])
        self.assertIsNone(unknown['name'])
        self.assertIsNone(unknown['value'])
        self.assertEqual(unknown['raw_hex'], '5a')         # 只保留原始字节
        # 未知标准字段同样保留字段号与字节，不分配任何业务名称。
        unknown_std = next(f for f in records[0]['fields'] if f['number'] == 70)
        self.assertIsNone(unknown_std['name'])
        self.assertEqual(unknown_std['value'], 0x40)

    def test_base_type_enum_mapping_covers_sdk_ids(self):
        self.assertEqual(FIT_BASE_TYPE_ID[132], 0x84)
        self.assertEqual(FIT_BASE_TYPE_ID[131], 0x83)
        self.assertEqual(FIT_BASE_TYPE_ID[134], 0x86)
        for internal in set(FIT_BASE_TYPE_ID.values()):
            self.assertIn(internal, BASE_TYPES)


class CollectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = {name: blob for name, (blob, _meta) in device_files().items()}
        cls.report = collect(cls.files['device-activity.fit'])

    def test_records_linked_to_laps_by_time_window_not_equal_split(self):
        training = self.report['trainings'][0]
        lap_counts = [lap['record_count'] for lap in training['laps']]
        self.assertEqual(lap_counts, [6, 6])
        self.assertEqual(training['record_count'], 12)
        for lap in training['laps']:
            self.assertIsInstance(lap['window'], list)
            self.assertEqual(len(lap['window']), 2)

    def test_missing_interval_event_with_locating_evidence(self):
        gaps = [ev for ev in self.report['events'] if ev['kind'] == 'missing_interval']
        self.assertEqual(len(gaps), 1)
        gap = gaps[0]
        self.assertEqual(gap['interval'], [BASE_TS + 6, BASE_TS + 10])
        self.assertEqual(gap['gap_seconds'], 6)
        self.assertIn('from_offset', gap['evidence'])
        self.assertIn('to_offset', gap['evidence'])
        self.assertEqual(gap['evidence']['from_timestamp_evidence'],
                         'compressed_header')

    def test_invalid_measurement_events_reference_offsets(self):
        invalid_events = [ev for ev in self.report['events']
                          if ev['kind'] == 'invalid_measurement']
        self.assertEqual(len(invalid_events), 3)
        for event in invalid_events:
            self.assertIn('message_offset', event['evidence'])
            self.assertTrue(event['detail']['invalid_fields']
                            or event['detail']['invalid_dev_fields'])

    def test_event_ids_stable_across_repeated_reception(self):
        again = collect(self.files['device-activity.fit'])
        self.assertEqual([ev['event_id'] for ev in again['events']],
                         [ev['event_id'] for ev in self.report['events']])
        self.assertEqual(again['trainings'][0]['training_id'],
                         self.report['trainings'][0]['training_id'])

    def test_truncated_file_produces_preview_but_no_training(self):
        report = collect(self.files['device-truncated.fit'])
        self.assertFalse(report['complete'])
        self.assertEqual(report['trainings'], [])
        self.assertTrue(report['preview']['incomplete'])
        self.assertIsNotNone(report['preview']['time_span'])
        self.assertTrue(any(ev['kind'] == 'truncated' for ev in report['events']))

    def test_corrupt_file_never_enters_training_list(self):
        report = collect(self.files['device-corrupt.fit'])
        self.assertFalse(report['complete'])
        self.assertEqual(report['trainings'], [])
        self.assertTrue(report['preview']['incomplete'])

    def test_mid_record_truncation_is_handled_without_raising(self):
        from fitcodec import parse
        from sampledata_ext import _build_activity
        # 截断夹具覆盖了记录/负载不完整路径，解析不得抛异常。
        report = parse(self.files['device-truncated.fit'])
        self.assertTrue(report['truncated'])
        self.assertTrue(
            any('crc' in e['reason'] or 'incomplete' in e['reason']
                for e in report['errors']))
        # 再构造一个切在首条 record 数据字段中间的文件。
        builder, _meta = _build_activity()
        document = builder.document()
        segment = next(seg for seg in builder.segments
                       if seg[0] == 'data' and seg[1] > 150)
        _kind, offset, length = segment
        cut = 14 + offset + 1 + length // 2
        mid = parse(document[:cut])
        self.assertTrue(mid['truncated'])
        self.assertFalse(mid['complete'])
        self.assertTrue(
            any(e['reason'].startswith('truncated') for e in mid['errors']))

    def test_nan_float_invalid_value_recognized(self):
        from fitcodec import _is_invalid
        self.assertTrue(_is_invalid([float('nan')], 0x88))
        self.assertFalse(_is_invalid([1.25], 0x88))
        self.assertTrue(_is_invalid([float('nan'), float('nan')], 0x89))


class StorageIdempotencyTests(unittest.TestCase):
    def test_reingest_is_idempotent_and_preview_isolated(self):
        files = {name: blob for name, (blob, _meta) in device_files().items()}
        with tempfile.TemporaryDirectory() as directory:
            archive = Archive(Path(directory) / 'store.sqlite3')
            good = files['device-activity.fit']
            first = archive.ingest('partner', good)
            second = archive.ingest('partner', good)
            self.assertEqual(
                [ev['event_id'] for ev in first['events']],
                [ev['event_id'] for ev in second['events']])
            self.assertEqual(len(archive.list_trainings()), 1)
            # 再收一次不产生重复训练或重复事件。
            archive.ingest('partner', good)
            self.assertEqual(len(archive.list_trainings()), 1)
            complete_events = archive.list_events(complete_only=True)
            self.assertTrue(all(item['complete'] for item in complete_events))

            archive.ingest('partner', files['device-truncated.fit'])
            archive.ingest('partner', files['device-corrupt.fit'])
            # 不完整文件不得进入完整训练列表。
            self.assertEqual(len(archive.list_trainings()), 1)
            all_events = archive.list_events()
            self.assertTrue(any(not item['complete'] for item in all_events))
            archive.close()


class CliAndScenarioTests(unittest.TestCase):
    def test_collect_cli_trainings_and_events(self):
        files = {name: blob for name, (blob, _meta) in device_files().items()}
        with tempfile.TemporaryDirectory() as directory:
            good = Path(directory) / 'device-activity.fit'
            truncated = Path(directory) / 'device-truncated.fit'
            good.write_bytes(files['device-activity.fit'])
            truncated.write_bytes(files['device-truncated.fit'])
            database = str(Path(directory) / 'store.sqlite3')

            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(
                    app.main(['collect', str(good), '--database', database,
                              '--source', 'cli']), 0)
            collected = json.loads(output.getvalue())
            self.assertTrue(collected['complete'])
            self.assertEqual(collected['trainings'], 1)
            self.assertIsNone(collected['preview'])

            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(
                    app.main(['collect', str(truncated), '--database', database,
                              '--source', 'cli']), 0)
            partial = json.loads(output.getvalue())
            self.assertFalse(partial['complete'])
            self.assertEqual(partial['trainings'], 0)
            self.assertTrue(partial['preview']['incomplete'])

            with contextlib.redirect_stdout(io.StringIO()) as output:
                app.main(['trainings', '--database', database])
            trainings = json.loads(output.getvalue())
            self.assertEqual(len(trainings), 1)  # 截断文件不进入完整列表

            with contextlib.redirect_stdout(io.StringIO()) as output:
                app.main(['events', '--database', database, '--complete-only'])
            events = json.loads(output.getvalue())
            self.assertTrue(all(item['complete'] for item in events))
            self.assertTrue(any(item['kind'] == 'missing_interval' for item in events))

    def test_device_fixtures_command_does_not_overwrite(self):
        files = {name: blob for name, (blob, _meta) in device_files().items()}
        with tempfile.TemporaryDirectory() as directory:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(
                    app.main(['device-fixtures', directory]), 0)
            written = json.loads(output.getvalue())['files']
            self.assertEqual(written, sorted(files))
            with contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(
                    app.main(['device-fixtures', directory]), 2)
            self.assertIn('ValueError', error.getvalue())

    def test_scenario_serves_device_files_over_loopback(self):
        routes = scenario()
        device_routes = [key for key in routes if key.startswith('GET /device/')]
        self.assertEqual(len(device_routes), 3)
        with ReplaySource(routes) as server:
            for key in device_routes:
                path = key.split(' ', 1)[1]
                status, _headers, raw = fetch(server.url + path)
                self.assertEqual(status, 200, key)
                report = collect(raw)
                if 'activity' in path:
                    self.assertTrue(report['complete'])
                else:
                    self.assertFalse(report['complete'])

    def test_collect_demo_command_is_stable_and_finite(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(app.main(['collect-demo']), 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report['stored_complete_trainings'], 1)
        self.assertTrue(report['truncated_files_excluded_from_trainings'])
        for item in report['files'].values():
            self.assertTrue(item['stable_event_ids'])


class BoundaryTimestampTests(unittest.TestCase):
    def test_compressed_timestamp_wraps_across_uint32_modulus(self):
        from fitcodec import parse
        from sampledata_ext import DeviceBuilder, UINT32, fit_file
        builder = DeviceBuilder()
        builder.definition(0, 20, [(253, 4, UINT32)])
        import struct
        near_end = (1 << 32) - 3            # 低 5 位 = 29
        builder.data(0, struct.pack('<I', near_end))
        # 低 5 位偏移依次 30、31、0、1：跨越 2^32 取模边界后必须单调递增。
        for offset in (30, 31, 0, 1):
            builder.compressed(0, offset, b'')
        decoded = parse(fit_file(builder.payload()))
        self.assertTrue(decoded['complete'], decoded['errors'])
        records = [m for m in decoded['messages']
                   if m.get('kind') == 'data' and m['global_mesg'] == 20]
        self.assertEqual(
            [m['timestamp'] for m in records],
            [near_end, near_end + 1, near_end + 2, 0, 1])


if __name__ == '__main__':
    unittest.main()
