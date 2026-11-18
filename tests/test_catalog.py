import json
import tempfile
import threading
import unittest
from pathlib import Path

from acquire.catalog import (Catalog, list_url, object_url, origin_base,
                             with_query)
from acquire.origin import make_server
from acquire.registry import Registry
from acquire.transport import Response


def listing(body, truncated=False, next_key=None, next_version=None):
    payload = json.loads(body) if isinstance(body, str) else body
    payload['IsTruncated'] = truncated
    if truncated:
        payload['NextKeyMarker'] = next_key
        payload['NextVersionIdMarker'] = next_version
    return {'status': 200, 'headers': {'Content-Type': 'application/json'},
            'body': json.dumps(payload, ensure_ascii=False)}


def route(path, body=None, *, method='GET', status=200, headers=None,
          body_base64=None):
    item = {'path': path, 'method': method, 'status': status,
            'headers': headers or {'Content-Type': 'application/json'}}
    if body_base64 is not None:
        item['body_base64'] = body_base64
    else:
        item['body'] = body if body is not None else ''
    return item


def build_scenario():
    page1 = listing({'Versions': [
        {'Key': 'east/guide.json', 'VersionId': 'opaque-A', 'IsLatest': False,
         'LastModified': '2026-11-20T01:00:00Z'},
        {'Key': 'east/guide.json', 'VersionId': 'opaque-B', 'IsLatest': False,
         'LastModified': '2026-11-19T01:00:00Z'},
        # 已被保留策略永久清除的历史版本仍出现在枚举里，但 GET 为 410
        {'Key': 'east/guide.json', 'VersionId': 'opaque-C', 'IsLatest': False,
         'LastModified': '2026-11-17T01:00:00Z'}],
        'DeleteMarkers': []}, truncated=True,
        next_key='east/guide.json', next_version='opaque-C')
    page2 = listing({'Versions': [
        {'Key': 'west/form.pdf', 'VersionId': 'opaque-W1', 'IsLatest': True,
         'LastModified': '2026-11-18T01:00:00Z'}],
        'DeleteMarkers': [
        {'Key': 'east/guide.json', 'VersionId': 'marker-D', 'IsLatest': True,
         'LastModified': '2026-11-21T01:00:00Z'}]}, truncated=False)

    same_body = '{"service": "company-change", "hall": "east"}'
    routes = [
        route('/bucket?versions', page1['body']),
        route('/bucket?versions&key-marker=east%2Fguide.json'
              '&version-id-marker=opaque-C', page2['body']),
        # 两个不同版本内容完全相同：仍各自是独立历史身份
        route('/objects/east/guide.json?versionId=opaque-A', same_body),
        route('/objects/east/guide.json?versionId=opaque-B', same_body),
        # 历史版本被保留策略永久清除：410 不等于对象当前被撤下
        route('/objects/east/guide.json?versionId=opaque-C',
              'version no longer retained', status=410),
        route('/objects/west/form.pdf?versionId=opaque-W1', 'PDF-BYTES'),
        # 最新版本是删除标记：普通 GET/HEAD 404，但带头说明身份
        route('/objects/east/guide.json', method='HEAD', status=404,
              headers={'x-amz-version-id': 'marker-D',
                       'x-amz-delete-marker': 'true'}),
        route('/objects/west/form.pdf', method='HEAD', status=200,
              headers={'x-amz-version-id': 'opaque-W1',
                       'x-amz-delete-marker': 'false'}),
        route('/objects/ghost/notice.txt', method='HEAD', status=404,
              headers={'Content-Type': 'application/json'}),
        route('/notifications', json.dumps({'Records': [
            {'key': 'east/guide.json', 'versionId': 'marker-D',
             'eventName': 'ObjectRemoved:DeleteMarkerCreated',
             'sequencer': '10'},
            {'key': 'east/guide.json', 'versionId': 'opaque-A',
             'eventName': 'ObjectCreated:Put', 'sequencer': '0f'},
            {'key': 'west/form.pdf', 'versionId': 'opaque-W1',
             'eventName': 'ObjectCreated:Put', 'sequencer': '03'},
            # 通知提到、枚举从未覆盖的 key
            {'key': 'ghost/notice.txt', 'versionId': 'opaque-G1',
             'eventName': 'ObjectCreated:Put', 'sequencer': '07'}]}),
        ),
    ]
    return {'entry_path': '/bucket?versions',
            'media': 'application/json', 'routes': routes}


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = str(Path(self.temporary.name) / 'ledger.sqlite3')
        self.scenario = build_scenario()
        self.server = make_server(self.scenario)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.origin = f'http://127.0.0.1:{self.server.server_port}'
        self.entry = self.origin + self.scenario['entry_path']
        self.bucket = 'hall-east'
        self.prefix = ''

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.temporary.cleanup()

    def test_paged_enumeration_records_versions_and_delete_markers(self):
        with Catalog(self.database) as catalog:
            result = catalog.enumerate_prefix(self.entry, self.bucket,
                                              self.prefix)
            self.assertTrue(result['completed'])
            self.assertEqual(result['pages'], 2)
            self.assertEqual(result['resumed'], False)
            versions = catalog.registry.list_versions(
                self.bucket, 'east/guide.json')
            ids = [v['version_id'] for v in versions]
            self.assertEqual(
                ids, ['opaque-A', 'opaque-B', 'opaque-C', 'marker-D'])
            self.assertEqual(
                [v['kind'] for v in versions],
                ['version', 'version', 'version', 'delete_marker'])
            marker = next(v for v in versions if v['kind'] == 'delete_marker')
            self.assertTrue(marker['is_latest'])
            self.assertEqual(
                catalog.registry.known_keys(self.bucket),
                ['east/guide.json', 'west/form.pdf'])

    def test_resume_continues_from_pair_marker_after_failure(self):
        attempts = {'n': 0}

        def flaky(url, **kwargs):
            if 'key-marker' in url:
                attempts['n'] += 1
                if attempts['n'] == 1:
                    raise RuntimeError('connection lost mid scan')
            from acquire.transport import fetch
            return fetch(url, **kwargs)

        with Catalog(self.database, fetch=flaky) as catalog:
            with self.assertRaisesRegex(RuntimeError, 'connection lost'):
                catalog.enumerate_prefix(self.entry, self.bucket, self.prefix)
            scan = catalog.registry.get_scan(
                catalog.registry.connection.execute(
                    'SELECT id FROM scans').fetchone()[0])
            self.assertEqual(scan['state'], 'running')
            self.assertEqual(scan['key_marker'], 'east/guide.json')
            self.assertEqual(scan['version_id_marker'], 'opaque-C')
            # 已采集的第一页保留
            self.assertEqual(
                len(catalog.registry.list_versions(
                    self.bucket, 'east/guide.json')), 3)

            resumed = catalog.enumerate_prefix(
                self.entry, self.bucket, self.prefix, resume=True)
            self.assertTrue(resumed['resumed'])
            self.assertTrue(resumed['completed'])
            self.assertEqual(
                len(catalog.registry.list_versions(
                    self.bucket, 'east/guide.json')), 4)

    def test_resume_with_max_pages_then_complete(self):
        with Catalog(self.database) as catalog:
            first = catalog.enumerate_prefix(
                self.entry, self.bucket, self.prefix, max_pages=1)
            self.assertFalse(first['completed'])
            second = catalog.enumerate_prefix(
                self.entry, self.bucket, self.prefix, resume=True)
            self.assertTrue(second['resumed'])
            self.assertTrue(second['completed'])
            report = catalog.history_report(self.bucket, self.prefix)
            self.assertTrue(report['enumeration_completed'])
            self.assertEqual([], report['uncertainty'])

    def test_versioned_get_distinguishes_saved_and_permanently_missing(self):
        with Catalog(self.database) as catalog:
            catalog.enumerate_prefix(self.entry, self.bucket, self.prefix)
            ok = catalog.fetch_version_object(
                self.origin, self.bucket, 'east/guide.json', 'opaque-A')
            self.assertEqual(ok['disposition'], 'saved')
            receipt = catalog.registry.receipt(ok['receipt_id'])
            self.assertEqual(receipt['status'], 200)
            self.assertEqual(
                receipt['body'],
                b'{"service": "company-change", "hall": "east"}')

            gone = catalog.fetch_version_object(
                self.origin, self.bucket, 'east/guide.json', 'opaque-C')
            self.assertEqual(gone['disposition'], 'permanently_missing')
            missing_receipt = catalog.registry.receipt(gone['receipt_id'])
            self.assertEqual(missing_receipt['status'], 410)

            saved = catalog.registry.saved_object(
                self.bucket, 'east/guide.json', 'opaque-A')
            expired = catalog.registry.saved_object(
                self.bucket, 'east/guide.json', 'opaque-C')
            self.assertEqual(saved['fetch_status'], 'saved')
            self.assertEqual(expired['fetch_status'], 'permanently_missing')

    def test_identical_content_remains_two_historical_identities(self):
        with Catalog(self.database) as catalog:
            catalog.enumerate_prefix(self.entry, self.bucket, self.prefix)
            for version_id in ('opaque-A', 'opaque-B'):
                result = catalog.fetch_version_object(
                    self.origin, self.bucket, 'east/guide.json', version_id)
                self.assertEqual(result['disposition'], 'saved')
            entries = {v['version_id']: catalog.registry.saved_object(
                self.bucket, 'east/guide.json', v['version_id'])
                for v in catalog.registry.list_versions(
                    self.bucket, 'east/guide.json') if v['kind'] == 'version'
                and v['version_id'] in ('opaque-A', 'opaque-B')}
            a = catalog.registry.receipt(entries['opaque-A']['receipt_id'])
            b = catalog.registry.receipt(entries['opaque-B']['receipt_id'])
            self.assertEqual(a['body'], b['body'])  # 内容相同
            self.assertNotEqual(entries['opaque-A']['receipt_id'],
                                entries['opaque-B']['receipt_id'])  # 出处不同

    def test_saved_original_is_never_overwritten_by_later_failure(self):
        with Catalog(self.database) as catalog:
            catalog.enumerate_prefix(self.entry, self.bucket, self.prefix)
            saved = catalog.fetch_version_object(
                self.origin, self.bucket, 'west/form.pdf', 'opaque-W1')
            self.assertEqual(saved['disposition'], 'saved')
            original_receipt = saved['receipt_id']

            def failing(url, **kwargs):
                return Response(url, 500, (), b'')

            catalog._fetch = failing
            again = catalog.fetch_version_object(
                self.origin, self.bucket, 'west/form.pdf', 'opaque-W1')
            # 已保存原件直接保留，甚至不再请求来源
            self.assertEqual(again['disposition'], 'already_saved')
            record = catalog.registry.saved_object(
                self.bucket, 'west/form.pdf', 'opaque-W1')
            self.assertEqual(record['fetch_status'], 'saved')
            self.assertEqual(record['receipt_id'], original_receipt)

    def test_retryable_failure_is_recorded_then_recovered(self):
        with Catalog(self.database) as catalog:
            catalog.enumerate_prefix(self.entry, self.bucket, self.prefix)
            real_fetch = catalog._fetch

            def flaky(url, **kwargs):
                if url.endswith('versionId=opaque-W1'):
                    return Response(url, 503, (), b'busy')
                return real_fetch(url, **kwargs)

            catalog._fetch = flaky
            failed = catalog.fetch_version_object(
                self.origin, self.bucket, 'west/form.pdf', 'opaque-W1')
            self.assertEqual(failed['disposition'], 'failed')
            self.assertEqual(failed['status_code'], 503)
            record = catalog.registry.saved_object(
                self.bucket, 'west/form.pdf', 'opaque-W1')
            self.assertEqual(record['fetch_status'], 'failed')
            # 失败状态进入报告的不确定范围
            report = catalog.history_report(self.bucket, self.prefix)
            self.assertTrue(any('west/form.pdf@opaque-W1' in note
                                for note in report['uncertainty']))

            catalog._fetch = real_fetch
            recovered = catalog.fetch_version_object(
                self.origin, self.bucket, 'west/form.pdf', 'opaque-W1')
            self.assertEqual(recovered['disposition'], 'saved')
            self.assertEqual(
                catalog.registry.saved_object(
                    self.bucket, 'west/form.pdf', 'opaque-W1')
                ['fetch_status'], 'saved')
            report = catalog.history_report(self.bucket, self.prefix)
            self.assertFalse(any('west/form.pdf@opaque-W1' in note
                                 for note in report['uncertainty']))

    def test_re_enumeration_does_not_delete_saved_originals(self):
        with Catalog(self.database) as catalog:
            catalog.enumerate_prefix(self.entry, self.bucket, self.prefix)
            catalog.fetch_version_object(
                self.origin, self.bucket, 'west/form.pdf', 'opaque-W1')
            # 再扫一遍；即使来源页面变化，已保存原件仍在
            catalog.enumerate_prefix(self.entry, self.bucket, self.prefix)
            record = catalog.registry.saved_object(
                self.bucket, 'west/form.pdf', 'opaque-W1')
            self.assertEqual(record['fetch_status'], 'saved')
            self.assertIsNotNone(
                catalog.registry.receipt(record['receipt_id']))

    def test_delete_marker_head_404_but_marker_skips_object_fetch(self):
        with Catalog(self.database) as catalog:
            catalog.enumerate_prefix(self.entry, self.bucket, self.prefix)
            observed = catalog.verify_key(
                self.origin, self.bucket, 'east/guide.json')
            self.assertEqual(observed['status_code'], 404)
            self.assertTrue(observed['delete_marker'])
            self.assertEqual(observed['current_version_id'], 'marker-D')
            state = catalog.registry.key_state(
                self.bucket, 'east/guide.json')
            self.assertTrue(state['delete_marker'])
            # 删除标记不是可领取原件；补领时跳过
            results = catalog.fetch_registered_objects(
                self.origin, self.bucket, keys=['east/guide.json'])
            self.assertTrue(all(r['version_id'] != 'marker-D'
                                for r in results))

    def test_head_observation_is_kept_separate_from_enumeration(self):
        with Catalog(self.database) as catalog:
            catalog.verify_key(self.origin, self.bucket, 'west/form.pdf')
            # 只有 HEAD 观察，没有枚举条目
            self.assertEqual(
                catalog.registry.list_versions(self.bucket, 'west/form.pdf'),
                [])
            state = catalog.registry.key_state(self.bucket, 'west/form.pdf')
            self.assertEqual(state['current_version_id'], 'opaque-W1')
            self.assertFalse(state['delete_marker'])

    def test_notification_sequencer_compared_only_within_same_key(self):
        with Catalog(self.database) as catalog:
            results = catalog.fetch_notifications(self.origin, self.bucket)
            by_key_seq = {(r['key'], r['sequencer']): r for r in results}
            east_10 = by_key_seq[('east/guide.json', '10')]
            east_0f = by_key_seq[('east/guide.json', '0f')]
            west_03 = by_key_seq[('west/form.pdf', '03')]
            self.assertEqual(east_10['disposition'], 'new')
            self.assertEqual(east_0f['disposition'], 'stale')
            # west 的 03 与 east 的 0f/10 互不比较，仍是新通知
            self.assertEqual(west_03['disposition'], 'new')
            self.assertEqual(
                catalog.registry.notification_watermark(
                    self.bucket, 'east/guide.json'), '10')
            self.assertEqual(
                catalog.registry.notification_watermark(
                    self.bucket, 'west/form.pdf'), '03')

            # 至少一次投递：同样的通知再来一轮全部幂等
            second = catalog.fetch_notifications(self.origin, self.bucket)
            self.assertTrue(all(r['disposition'] == 'duplicate'
                                for r in second))
            # 迟到通知不会作用到别的 key：核实只针对记录自带 key
            self.assertEqual(
                {v['key'] for v in second},
                {r['key'] for r in results})

    def test_late_notification_for_other_key_does_not_cross_verify(self):
        with Catalog(self.database) as catalog:
            record = {'key': 'east/guide.json', 'versionId': 'marker-D',
                      'eventName': 'ObjectRemoved:DeleteMarkerCreated',
                      'sequencer': '10'}
            handled = catalog.handle_notification(self.origin, self.bucket,
                                                   record)
            self.assertEqual(handled['verification']['key'],
                             'east/guide.json')
            # west 从未被该通知核实
            self.assertIsNone(
                catalog.registry.key_state(self.bucket, 'west/form.pdf'))

    def test_ghost_notification_is_flagged_not_silently_enumerated(self):
        with Catalog(self.database) as catalog:
            catalog.enumerate_prefix(self.entry, self.bucket, self.prefix)
            handled = catalog.handle_notification(self.origin, self.bucket, {
                'key': 'ghost/notice.txt', 'versionId': 'opaque-G1',
                'eventName': 'ObjectCreated:Put', 'sequencer': '07'})
            self.assertEqual(handled['disposition'], 'new')
            # 定向核实做了，但枚举覆盖没有被通知冒充
            self.assertEqual(
                catalog.registry.list_versions(
                    self.bucket, 'ghost/notice.txt'), [])
            report = catalog.history_report(self.bucket, self.prefix)
            self.assertIn('ghost/notice.txt',
                          report['unverified_notification_keys'])
            self.assertTrue(any('never seen by enumeration' in note
                                for note in report['uncertainty']))

    def test_report_separates_provenance_and_uncertainty(self):
        with Catalog(self.database) as catalog:
            fresh = catalog.history_report(self.bucket, self.prefix)
            self.assertFalse(fresh['enumeration_completed'])
            self.assertTrue(any('never completed' in note
                                for note in fresh['uncertainty']))

            catalog.enumerate_prefix(self.entry, self.bucket, self.prefix)
            catalog.fetch_registered_objects(self.origin, self.bucket)
            catalog.fetch_version_object(
                self.origin, self.bucket, 'east/guide.json', 'opaque-C')
            catalog.verify_key(self.origin, self.bucket, 'east/guide.json')
            report = catalog.history_report(self.bucket, self.prefix)
            self.assertTrue(report['enumeration_completed'])
            self.assertEqual(report['counts']['delete_markers'], 1)
            self.assertEqual(report['counts']['saved_objects'], 3)
            self.assertEqual(report['counts']['permanently_missing'], 1)
            self.assertEqual(report['counts']['head_verified'], 1)
            east = next(k for k in report['keys']
                        if k['key'] == 'east/guide.json')
            provenance = {v['version_id']:
                          (v['object'] or {}).get('fetch_status')
                          for v in east['versions']}
            self.assertEqual(provenance['marker-D'], None)
            self.assertEqual(provenance['opaque-A'], 'saved')
            self.assertEqual(provenance['opaque-C'], 'permanently_missing')

    def test_url_helpers_encode_pair_markers_and_version_id(self):
        url = list_url('http://host/bucket?versions',
                       'east/guide.json', 'opaque-C')
        self.assertEqual(
            url,
            'http://host/bucket?versions&key-marker=east%2Fguide.json'
            '&version-id-marker=opaque-C')
        self.assertEqual(
            object_url('http://host', self.bucket, 'east/guide.json',
                       'opaque-A'),
            'http://host/objects/east/guide.json?versionId=opaque-A')
        self.assertEqual(origin_base(self.entry), self.origin)
        self.assertEqual(
            with_query('http://host/bucket?versions',
                       {'key-marker': 'k', 'version-id-marker': None}),
            'http://host/bucket?versions&key-marker=k')

    def test_registry_rejects_non_hex_sequencer(self):
        registry = Registry(self.database)
        try:
            with self.assertRaises(ValueError):
                registry.ingest_notification(
                    self.bucket,
                    {'key': 'k', 'versionId': 'v', 'eventName': 'x',
                     'sequencer': 'zz'})
        finally:
            registry.close()


if __name__ == '__main__':
    unittest.main()
