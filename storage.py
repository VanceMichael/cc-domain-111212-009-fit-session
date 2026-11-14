import hashlib
import json
import sqlite3

from collector import collect


class Archive:
    def __init__(self, path):
        self.connection = sqlite3.connect(path, timeout=5)
        self.connection.execute('PRAGMA foreign_keys=ON')
        self.connection.executescript(
            'CREATE TABLE IF NOT EXISTS captures('
            'id INTEGER PRIMARY KEY, source TEXT NOT NULL, digest TEXT NOT NULL,'
            'raw BLOB NOT NULL, rows_json TEXT NOT NULL, UNIQUE(source,digest));'
            'CREATE TABLE IF NOT EXISTS trainings('
            'id INTEGER PRIMARY KEY, source TEXT NOT NULL, digest TEXT NOT NULL,'
            'training_id TEXT NOT NULL, complete INTEGER NOT NULL,'
            'report_json TEXT NOT NULL, UNIQUE(source,training_id));'
            'CREATE TABLE IF NOT EXISTS training_events('
            'id INTEGER PRIMARY KEY, source TEXT NOT NULL, digest TEXT NOT NULL,'
            'event_id TEXT NOT NULL, kind TEXT NOT NULL, complete INTEGER NOT NULL,'
            'event_json TEXT NOT NULL, UNIQUE(source,event_id));')

    def save(self, source, raw, rows):
        if not source.strip():
            raise ValueError('来源标识不能为空')
        digest = hashlib.sha256(raw).hexdigest()
        payload = json.dumps(rows, ensure_ascii=False, sort_keys=True)
        with self.connection:
            self.connection.execute(
                'INSERT OR IGNORE INTO captures(source,digest,raw,rows_json) VALUES(?,?,?,?)',
                (source, digest, raw, payload))
            record = self.connection.execute(
                'SELECT id FROM captures WHERE source=? AND digest=?', (source, digest)).fetchone()
        return record[0]

    def ingest(self, source, raw):
        """采集一份 FIT 原件并写入训练/事件表。

        完整性校验不通过（截断或 CRC 失败）时，只登记 preview 事件并返回
        ``complete=False`` 的报告，不产生任何完整训练记录；同一文件重复接收时，
        training_id / event_id 仅由摘要与字节偏移推导，UNIQUE 约束保证幂等。
        """
        if not source.strip():
            raise ValueError('来源标识不能为空')
        report = collect(raw)
        digest = report['digest']
        with self.connection:
            for training in report['trainings']:
                self.connection.execute(
                    'INSERT OR IGNORE INTO trainings(source,digest,training_id,complete,'
                    'report_json) VALUES(?,?,?,1,?)',
                    (source, digest, training['training_id'],
                     json.dumps(training, ensure_ascii=False, sort_keys=True)))
            for event in report['events']:
                self.connection.execute(
                    'INSERT OR IGNORE INTO training_events(source,digest,event_id,kind,'
                    'complete,event_json) VALUES(?,?,?,?,?,?)',
                    (source, digest, event['event_id'], event['kind'],
                     1 if report['complete'] else 0,
                     json.dumps(event, ensure_ascii=False, sort_keys=True)))
        return report

    def list(self):
        return [dict(id=row[0], source=row[1], digest=row[2], rows=json.loads(row[3]))
                for row in self.connection.execute(
                    'SELECT id,source,digest,rows_json FROM captures ORDER BY id')]

    def list_trainings(self):
        """只返回完整训练；截断/损坏文件永远不会出现在这里。"""
        return [dict(training_id=row[0], source=row[1], digest=row[2],
                     report=json.loads(row[3]))
                for row in self.connection.execute(
                    'SELECT training_id,source,digest,report_json FROM trainings '
                    'WHERE complete=1 ORDER BY id')]

    def list_events(self, complete_only=False):
        statement = ('SELECT event_id,kind,complete,event_json FROM training_events '
                     'ORDER BY id')
        if complete_only:
            statement = ('SELECT event_id,kind,complete,event_json FROM training_events '
                         'WHERE complete=1 ORDER BY id')
        return [dict(event_id=row[0], kind=row[1], complete=bool(row[2]),
                     event=json.loads(row[3]))
                for row in self.connection.execute(statement)]

    def raw(self, capture_id):
        row = self.connection.execute(
            'SELECT raw FROM captures WHERE id=?', (capture_id,)).fetchone()
        if row is None:
            raise KeyError(capture_id)
        return bytes(row[0])

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
