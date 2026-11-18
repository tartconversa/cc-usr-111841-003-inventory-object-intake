"""版本历史采集的命令行入口；单次读取仍由 acquire.service 提供。"""
import argparse
import json

from .catalog import Catalog, origin_base


def _print(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='acquire', description='大厅资料版本历史采集')
    parser.add_argument('database', help='本地 SQLite 登记簿路径')
    sub = parser.add_subparsers(dest='command', required=True)

    scan = sub.add_parser('scan', help='按前缀枚举版本与删除标记（可续领）')
    scan.add_argument('entry_url', help='ListObjectVersions 入口，如 /bucket?versions')
    scan.add_argument('--bucket', required=True)
    scan.add_argument('--prefix', default='')
    scan.add_argument('--resume', action='store_true')
    scan.add_argument('--verify', action='store_true', help='枚举后对观察到的 key 做 HEAD 核实')
    scan.add_argument('--max-pages', type=int, default=None)

    fetch = sub.add_parser('fetch', help='读取登记簿中各版本仍可领取的原件')
    fetch.add_argument('origin', help='来源根地址，如 http://127.0.0.1:8000')
    fetch.add_argument('--bucket', required=True)
    fetch.add_argument('--key', action='append', help='限定 key，可重复传入')

    verify = sub.add_parser('verify', help='HEAD 核实指定 key 的当前状态')
    verify.add_argument('origin')
    verify.add_argument('--bucket', required=True)
    verify.add_argument('--key', action='append', required=True)

    notify = sub.add_parser('notifications', help='拉取通知并触发定向核实')
    notify.add_argument('origin')
    notify.add_argument('--bucket', required=True)

    report = sub.add_parser('report', help='输出前缀历史资料与不确定范围')
    report.add_argument('--bucket', required=True)
    report.add_argument('--prefix', default='')

    args = parser.parse_args(argv)
    with Catalog(args.database) as catalog:
        if args.command == 'scan':
            _print(catalog.enumerate_prefix(
                args.entry_url, args.bucket, args.prefix,
                resume=args.resume, verify=args.verify,
                max_pages=args.max_pages))
        elif args.command == 'fetch':
            _print(catalog.fetch_registered_objects(
                args.origin, args.bucket, keys=args.key))
        elif args.command == 'verify':
            _print([catalog.verify_key(args.origin, args.bucket, key)
                    for key in args.key])
        elif args.command == 'notifications':
            _print(catalog.fetch_notifications(args.origin, args.bucket))
        elif args.command == 'report':
            _print(catalog.history_report(args.bucket, args.prefix))


if __name__ == '__main__':
    main()
