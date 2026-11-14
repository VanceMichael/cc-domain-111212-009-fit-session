import base64
import json
import sys
from pathlib import Path

# 隔离模式（python -I）下脚本目录不会自动进入 sys.path，显式加入。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sampledata import samples
from sampledata_ext import device_files

def response(raw, status=200, headers=None, **options):
    payload = dict(base64=base64.b64encode(raw).decode(), status=status,
                   headers=headers or {}, chunk_bytes=3)
    payload.update(options)
    return payload

def scenario():
    fixtures = samples()
    routes = {}
    routes['GET /session'] = [response(fixtures['sample.fit'],status=200)]
    routes['GET /broken'] = [response(fixtures['corrupt.fit'],status=200)]
    # 定义驱动采集演练：完整设备文件、截断文件与 CRC 损坏文件，均为本地虚构。
    for name, (raw, _meta) in device_files().items():
        routes['GET /device/' + name] = [response(raw, chunk_bytes=7)]
    return routes

def main():
    import argparse
    from transport import ReplaySource
    parser = argparse.ArgumentParser(description='来源协议本地演练入口')
    parser.add_argument('--export', action='store_true')
    args = parser.parse_args()
    routes = scenario()
    if args.export:
        print(json.dumps(routes,ensure_ascii=False,sort_keys=True))
        return
    with ReplaySource(routes) as server:
        print(json.dumps({'url':server.url,'routes':list(routes)},ensure_ascii=False),flush=True)
        try:
            server.thread.join()
        except KeyboardInterrupt:
            pass

if __name__ == '__main__':
    main()
