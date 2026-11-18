"""枚举续领、通知核实、指定版本读取与历史登记的协同编排。

职责边界：
- 枚举提供前缀覆盖证明（完成扫描才有完整覆盖），但不是冻结快照；
- 通知只触发定向核实，至少一次投递、可能迟到或缺段，不替代枚举；
- HEAD 只证明本次对象观察，不证明扫描时点的当前目录；
- 指定 versionId 的 GET 才是历史原件的出处；410 表示该历史原件被永久清除，
  与删除标记（对象当前被撤下、旧原件可能仍可读）是两回事。
"""
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit, parse_qsl, quote

from .decode import decode_versions
from .registry import Registry
from .transport import fetch as default_fetch


def _query_string(pairs):
    # 保留无值参数的裸键形式，如 ?versions，不写成 versions=
    rendered = []
    for key, value in pairs:
        name = quote(key, safe='')
        rendered.append(name if value == '' else name + '=' + quote(value, safe=''))
    return '&'.join(rendered)


def with_query(url, extra):
    """在保留原查询串的基础上追加参数（None 值参数不传）。"""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k not in extra]
    query.extend((k, v) for k, v in extra.items() if v is not None)
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       _query_string(query), parts.fragment))


def origin_base(url):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, '', '', ''))


def list_url(entry_url, key_marker, version_id_marker):
    return with_query(entry_url, {'key-marker': key_marker,
                                  'version-id-marker': version_id_marker})


def object_url(origin, bucket, key, version_id=None):
    url = f'{origin}/objects/{key}'
    if version_id is not None:
        url = with_query(url, {'versionId': version_id})
    return url


def head_url(origin, key):
    return f'{origin}/objects/{key}'


class Catalog:
    def __init__(self, database, *, fetch=default_fetch):
        self.registry = Registry(database)
        self._fetch = fetch

    def close(self):
        self.registry.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- 前缀枚举（可中断续领） ----------------------------------------

    def enumerate_prefix(self, entry_url, bucket, prefix, *, resume=False,
                         verify=False, max_pages=None, max_bytes=1048576):
        """按 key-marker/version-id-marker 成对续页，登记全部版本与删除标记。

        resume=True 时从上次未完成扫描的检查点继续；本方法只登记、不删除，
        扫描期间对象更新导致页内 IsLatest 变化时，当前目录以 HEAD 核实为准。
        """
        started = self.registry.start_scan(bucket, prefix, resume=resume)
        scan_id = started['scan_id']
        key_marker = started['key_marker']
        version_id_marker = started['version_id_marker']
        pages = 0
        observed_keys = []
        while True:
            url = list_url(entry_url, key_marker, version_id_marker)
            response = self._fetch(url, max_bytes=max_bytes)
            if not 200 <= response.status < 300:
                raise RuntimeError(
                    f'list versions failed status={response.status} url={url}')
            page = decode_versions(response.body)
            self.registry.record_page(scan_id, bucket, page)
            observed_keys.extend(item['Key'] for item in page['versions'])
            observed_keys.extend(item['Key'] for item in page['delete_markers'])
            pages += 1
            if not page['is_truncated']:
                break
            key_marker = page['next_key_marker']
            version_id_marker = page['next_version_id_marker']
            if not key_marker or not version_id_marker:
                raise RuntimeError(
                    'truncated page missing NextKeyMarker/NextVersionIdMarker')
            # 每页落检查点：中断后已采集页面不丢，续领从成对标记继续
            self.registry.checkpoint(scan_id, key_marker, version_id_marker)
            if max_pages is not None and pages >= max_pages:
                break
        if max_pages is None or pages < max_pages:
            self.registry.complete_scan(scan_id)
        result = dict(scan_id=scan_id, resumed=started['resumed'], pages=pages,
                      completed=self.registry.get_scan(scan_id)['state'] == 'completed',
                      observed_keys=sorted(set(observed_keys)))
        if verify:
            result['verified'] = [
                self.verify_key(origin_base(entry_url), bucket, key, scan_id=scan_id)
                for key in result['observed_keys']]
        return result

    # ---- 指定版本原件读取 ----------------------------------------------

    def fetch_version_object(self, origin, bucket, key, version_id, *,
                             max_bytes=1048576):
        """读取并登记一个指定版本的原件。

        200 保存原件与收据；410 登记为永久缺失（保留策略清除）；
        其余状态记为可重试失败。已保存的原件不会被任何后续结果覆盖。
        """
        existing = self.registry.saved_object(bucket, key, version_id)
        if existing is not None and existing['fetch_status'] == 'saved':
            return dict(key=key, version_id=version_id, disposition='already_saved')

        url = object_url(origin, bucket, key, version_id)
        response = self._fetch(url, max_bytes=max_bytes)
        if 200 <= response.status < 300:
            receipt_id = self.registry.save_receipt(response)
            status = self.registry.record_object_fetch(
                bucket, key, version_id,
                dict(fetch_status='saved', status_code=response.status,
                     receipt_id=receipt_id,
                     saved_at=datetime.now(timezone.utc).isoformat()))
            return dict(key=key, version_id=version_id, disposition=status,
                        receipt_id=receipt_id)
        if response.status == 410:
            receipt_id = self.registry.save_receipt(response)
            status = self.registry.record_object_fetch(
                bucket, key, version_id,
                dict(fetch_status='permanently_missing', status_code=410,
                     receipt_id=receipt_id, note='source reports version expired'))
            return dict(key=key, version_id=version_id, disposition=status,
                        receipt_id=receipt_id)
        self.registry.record_object_fetch(
            bucket, key, version_id,
            dict(fetch_status='failed', status_code=response.status,
                 note='retryable source error'))
        return dict(key=key, version_id=version_id, disposition='failed',
                    status_code=response.status)

    def fetch_registered_objects(self, origin, bucket, *, keys=None,
                                 max_bytes=1048576):
        """为登记簿中仍可领取的各版本补领原件；删除标记不是原件，跳过。"""
        targets = keys or self.registry.known_keys(bucket)
        results = []
        for key in targets:
            for version in self.registry.list_versions(bucket, key):
                if version['kind'] == 'delete_marker':
                    continue
                known = self.registry.saved_object(
                    bucket, key, version['version_id'])
                if known is not None and known['fetch_status'] in (
                        'saved', 'permanently_missing'):
                    continue
                results.append(self.fetch_version_object(
                    origin, bucket, key, version['version_id'],
                    max_bytes=max_bytes))
        return results

    # ---- 定向核实（HEAD 与通知） ---------------------------------------

    def verify_key(self, origin, bucket, key, *, scan_id=None):
        """HEAD 一个 key 的当前状态；删除标记为最新版本时来源返回 404 带头。"""
        response = self._fetch(head_url(origin, key), method='HEAD')
        self.registry.record_head(bucket, key, response, scan_id=scan_id)
        return dict(key=key, status_code=response.status,
                    current_version_id=response.header('x-amz-version-id') or None,
                    delete_marker=response.header(
                        'x-amz-delete-marker').lower() == 'true')

    def handle_notification(self, origin, bucket, record):
        """处理一条通知：同 key 比 sequencer，仅对新通知做定向核实。

        sequencer 只在同一 key 内比较；核实动作只作用于通知自带的 key。
        通知不触发枚举结论的修改，覆盖证明仍来自前缀枚举。
        """
        disposition = self.registry.ingest_notification(bucket, record)
        result = dict(key=record['key'], version_id=record['versionId'],
                      event_name=record['eventName'],
                      sequencer=record['sequencer'], disposition=disposition,
                      verification=None)
        if disposition != 'new':
            return result
        result['verification'] = self.verify_key(origin, bucket, record['key'])
        self.registry.mark_notification_applied(
            bucket, record['key'], record['sequencer'])
        return result

    def fetch_notifications(self, origin, bucket, *, max_bytes=1048576):
        """拉取通知入口并逐条处理；至少一次投递下重复条目安全幂等。"""
        response = self._fetch(f'{origin}/notifications', max_bytes=max_bytes)
        if not 200 <= response.status < 300:
            raise RuntimeError(
                f'notifications failed status={response.status}')
        from json import loads
        return [self.handle_notification(origin, bucket, record)
                for record in loads(response.body).get('Records', [])]

    # ---- 报告 -----------------------------------------------------------

    def history_report(self, bucket, prefix):
        return self.registry.history_report(bucket, prefix)
