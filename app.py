import argparse
import base64
import json
from pathlib import Path
import sys
import tempfile

# 隔离模式（python -I）下脚本目录不会自动进入 sys.path，显式加入以保证
# 同目录模块可导入；目录只含本项目代码，不读取任何外部输入路径。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from source import inspect_bytes
from sampledata import samples
from sampledata_ext import device_files
from storage import Archive
from transport import ReplaySource, fetch

MAX_INPUT = 16*1024*1024

def read_input(path):
    with Path(path).open('rb') as handle:
        data = handle.read(MAX_INPUT+1)
    if len(data) > MAX_INPUT:
        raise ValueError('输入超过接收限制')
    return data

def demo():
    fixtures = samples()
    name = next(key for key in fixtures if key.startswith('sample.'))
    data = fixtures[name]
    routes = {'GET /sample':[{'base64':base64.b64encode(data).decode()}]}
    with tempfile.TemporaryDirectory() as directory, ReplaySource(routes) as server:
        status, headers, raw = fetch(server.url + '/sample')
        if status != 200:
            raise ValueError('来源请求失败')
        rows = inspect_bytes(raw)
        with Archive(Path(directory)/'archive.sqlite3') as archive:
            first = archive.save('local-demo', raw, rows)
            repeated = archive.save('local-demo', raw, rows)
            return dict(input=name, bytes=len(raw), captures=len(archive.list()),
                        same_capture=first==repeated, rows=rows, requests=len(server.requests))

def collect_demo():
    """端到端演练：本地生成设备文件 -> 回环 HTTP 领取 -> 解码采集 -> 入库幂等。

    覆盖消息号重定义、压缩时间戳回绕、数组、无效测量、缺失区间、截断预览、
    CRC 损坏与同文件重复接收，全程不接触真实手表或在线账号。
    """
    fixtures = {name: blob for name, (blob, _meta) in device_files().items()}
    routes = {'GET /' + name: [{'base64': base64.b64encode(blob).decode(),
                                'chunk_bytes': 7}]
              for name, blob in fixtures.items()}
    summary = {}
    with tempfile.TemporaryDirectory() as directory, ReplaySource(routes) as server:
        with Archive(Path(directory)/'collect.sqlite3') as archive:
            for name in sorted(fixtures):
                status, _headers, raw = fetch(server.url + '/' + name)
                if status != 200:
                    raise ValueError('来源请求失败：' + name)
                first = archive.ingest('local-device', raw)
                # 同一文件重新接收：事件与训练标识必须稳定，且不产生重复行。
                second = archive.ingest('local-device', raw)
                same_ids = [ev['event_id'] for ev in first['events']] == \
                           [ev['event_id'] for ev in second['events']]
                summary[name] = dict(
                    complete=first['complete'],
                    trainings=len(first['trainings']),
                    event_kinds=sorted({ev['kind'] for ev in first['events']}),
                    stable_event_ids=same_ids,
                    preview_incomplete=(second['preview']['incomplete']
                                        if second['preview'] else None))
            trainings = archive.list_trainings()
            events = archive.list_events()
            return dict(requests=len(server.requests), files=summary,
                        stored_complete_trainings=len(trainings),
                        stored_events=len(events),
                        truncated_files_excluded_from_trainings=len(trainings) == 1)

def main(argv=None):
    parser = argparse.ArgumentParser(description='来源检查与原件留存')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('inspect', 'capture'):
        command = commands.add_parser(name)
        command.add_argument('path')
        if name == 'capture':
            command.add_argument('--database', required=True)
            command.add_argument('--source', required=True)
    listing = commands.add_parser('list')
    listing.add_argument('--database', required=True)
    collect = commands.add_parser('collect')
    collect.add_argument('path')
    collect.add_argument('--database', required=True)
    collect.add_argument('--source', required=True)
    trainings = commands.add_parser('trainings')
    trainings.add_argument('--database', required=True)
    events = commands.add_parser('events')
    events.add_argument('--database', required=True)
    events.add_argument('--complete-only', action='store_true')
    fixtures = commands.add_parser('fixtures')
    fixtures.add_argument('directory')
    device = commands.add_parser('device-fixtures')
    device.add_argument('directory')
    commands.add_parser('demo')
    commands.add_parser('collect-demo')
    args = parser.parse_args(argv)
    try:
        if args.command == 'demo':
            result = demo()
        elif args.command == 'collect-demo':
            result = collect_demo()
        elif args.command == 'fixtures':
            destination = Path(args.directory)
            destination.mkdir(parents=True, exist_ok=True)
            for name, raw in samples().items():
                path = destination/name
                if path.exists():
                    raise ValueError('不覆盖已有夹具文件：'+name)
                path.write_bytes(raw)
            result = dict(files=sorted(samples()))
        elif args.command == 'device-fixtures':
            destination = Path(args.directory)
            destination.mkdir(parents=True, exist_ok=True)
            written = []
            for name, (raw, _meta) in device_files().items():
                path = destination/name
                if path.exists():
                    raise ValueError('不覆盖已有夹具文件：'+name)
                path.write_bytes(raw)
                written.append(name)
            result = dict(files=sorted(written))
        elif args.command == 'list':
            with Archive(args.database) as archive:
                result = archive.list()
        elif args.command == 'trainings':
            with Archive(args.database) as archive:
                result = archive.list_trainings()
        elif args.command == 'events':
            with Archive(args.database) as archive:
                result = archive.list_events(args.complete_only)
        elif args.command == 'collect':
            raw = read_input(args.path)
            with Archive(args.database) as archive:
                report = archive.ingest(args.source, raw)
                result = dict(complete=report['complete'], digest=report['digest'],
                              trainings=len(report['trainings']),
                              events=len(report['events']),
                              errors=report['errors'],
                              preview=report['preview'])
        else:
            raw = read_input(args.path)
            rows = inspect_bytes(raw)
            if args.command == 'inspect':
                result = rows
            else:
                with Archive(args.database) as archive:
                    result = dict(capture_id=archive.save(args.source,raw,rows), rows=len(rows))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as error:
        print(json.dumps(dict(error=type(error).__name__, reason=str(error)), ensure_ascii=False), file=sys.stderr)
        return 2

if __name__ == '__main__':
    raise SystemExit(main())
