"""版本历史登记簿：枚举证据、原件收据、当前观察与通知水位。

所有历史表只增不删：一次扫描中没再看到的旧版本，仍然保留之前采集到的
原件和出处记录。当前状态（key_states）与枚举观察（object_versions）分开，
因为分页扫描不是同一时点快照，页内 IsLatest 不能拼出当前目录。
"""
import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc).isoformat()


class Registry:
    def __init__(self, path):
        self.connection = sqlite3.connect(path)
        self.connection.execute('PRAGMA foreign_keys=ON')
        self.connection.executescript("""
        CREATE TABLE IF NOT EXISTS receipts(
            id TEXT PRIMARY KEY, url TEXT NOT NULL, status INTEGER NOT NULL,
            headers TEXT NOT NULL, body BLOB NOT NULL);

        CREATE TABLE IF NOT EXISTS scans(
            id TEXT PRIMARY KEY, bucket TEXT NOT NULL, prefix TEXT NOT NULL,
            state TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
            key_marker TEXT, version_id_marker TEXT);

        CREATE TABLE IF NOT EXISTS object_versions(
            bucket TEXT NOT NULL, key TEXT NOT NULL, version_id TEXT NOT NULL,
            kind TEXT NOT NULL, is_latest INTEGER, last_modified TEXT,
            source_fields TEXT NOT NULL,
            first_seen_scan TEXT NOT NULL, last_seen_scan TEXT NOT NULL,
            PRIMARY KEY(bucket, key, version_id));

        CREATE TABLE IF NOT EXISTS saved_objects(
            bucket TEXT NOT NULL, key TEXT NOT NULL, version_id TEXT NOT NULL,
            fetch_status TEXT NOT NULL, status_code INTEGER,
            receipt_id TEXT, saved_at TEXT, note TEXT,
            PRIMARY KEY(bucket, key, version_id));

        CREATE TABLE IF NOT EXISTS key_states(
            bucket TEXT NOT NULL, key TEXT NOT NULL,
            observed_at TEXT NOT NULL, status_code INTEGER NOT NULL,
            current_version_id TEXT, delete_marker INTEGER,
            scan_id TEXT, receipt_id TEXT,
            PRIMARY KEY(bucket, key));

        CREATE TABLE IF NOT EXISTS notifications(
            bucket TEXT NOT NULL, key TEXT NOT NULL, version_id TEXT NOT NULL,
            event_name TEXT NOT NULL, sequencer TEXT NOT NULL,
            received_at TEXT NOT NULL, applied INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(bucket, key, sequencer));
        """)

    def close(self):
        self.connection.close()

    # ---- 扫描进度 -------------------------------------------------------

    def start_scan(self, bucket, prefix, *, resume=False):
        """开始一次前缀枚举；resume=True 时取回未完成扫描的检查点。"""
        with self.connection:
            if resume:
                row = self.connection.execute(
                    "SELECT id,key_marker,version_id_marker FROM scans "
                    "WHERE bucket=? AND prefix=? AND state!='completed' "
                    "ORDER BY started_at DESC LIMIT 1", (bucket, prefix)).fetchone()
                if row is not None:
                    return {'scan_id': row[0], 'resumed': True,
                            'key_marker': row[1], 'version_id_marker': row[2]}
            scan_id = uuid.uuid4().hex
            self.connection.execute(
                "INSERT INTO scans VALUES(?,?,?,?,?,?,?,?)",
                (scan_id, bucket, prefix, 'running', _now(), None, None, None))
            return {'scan_id': scan_id, 'resumed': False,
                    'key_marker': None, 'version_id_marker': None}

    def get_scan(self, scan_id):
        row = self.connection.execute(
            'SELECT id,bucket,prefix,state,started_at,finished_at,'
            'key_marker,version_id_marker FROM scans WHERE id=?',
            (scan_id,)).fetchone()
        if row is None:
            return None
        return dict(scan_id=row[0], bucket=row[1], prefix=row[2], state=row[3],
                    started_at=row[4], finished_at=row[5],
                    key_marker=row[6], version_id_marker=row[7])

    def checkpoint(self, scan_id, key_marker, version_id_marker):
        with self.connection:
            self.connection.execute(
                'UPDATE scans SET state=?,key_marker=?,version_id_marker=? WHERE id=?',
                ('running', key_marker, version_id_marker, scan_id))

    def complete_scan(self, scan_id):
        with self.connection:
            self.connection.execute(
                "UPDATE scans SET state='completed',finished_at=? WHERE id=?",
                (_now(), scan_id))

    # ---- 枚举页登记 -----------------------------------------------------

    def record_page(self, scan_id, bucket, page):
        """登记一页中的版本与删除标记。

        条目按 (bucket,key,versionId) 去重；重新看到同一版本只更新观察字段，
        页面里没出现的旧条目不做任何删除。
        """
        with self.connection:
            for kind in ('versions', 'delete_markers'):
                stored_kind = 'version' if kind == 'versions' else 'delete_marker'
                for item in page[kind]:
                    self._record_entry(scan_id, bucket, stored_kind, item)

    def _record_entry(self, scan_id, bucket, kind, item):
        key = item['Key']
        version_id = item['VersionId']
        fields = json.dumps(item, ensure_ascii=False)
        self.connection.execute(
            "INSERT INTO object_versions VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(bucket,key,version_id) DO UPDATE SET "
            "is_latest=excluded.is_latest,last_modified=excluded.last_modified,"
            "source_fields=excluded.source_fields,last_seen_scan=excluded.last_seen_scan",
            (bucket, key, version_id, kind,
             1 if item.get('IsLatest') else 0,
             item.get('LastModified'), fields, scan_id, scan_id))

    # ---- 原件收据 -------------------------------------------------------

    def save_receipt(self, response):
        # 收据身份包含 URL、状态、响应头与实体字节：相同字节来自不同版本
        # URL 时仍是两张各自独立的出处收据，不能按内容摘要合并。
        material = json.dumps(
            [response.url, response.status, list(response.headers),
             response.body.hex()], ensure_ascii=False).encode('utf-8')
        receipt_id = hashlib.sha256(material).hexdigest()
        with self.connection:
            self.connection.execute(
                'INSERT OR IGNORE INTO receipts VALUES(?,?,?,?,?)',
                (receipt_id, response.url, response.status,
                 json.dumps(response.headers, ensure_ascii=False), response.body))
        return receipt_id

    def receipt(self, receipt_id):
        row = self.connection.execute(
            'SELECT url,status,headers,body FROM receipts WHERE id=?',
            (receipt_id,)).fetchone()
        if row is None:
            return None
        return dict(url=row[0], status=row[1],
                    headers=json.loads(row[2]), body=row[3])

    def record_object_fetch(self, bucket, key, version_id, outcome):
        """登记指定版本读取结果；已保存的原件不会被后续结果覆盖。"""
        with self.connection:
            row = self.connection.execute(
                'SELECT fetch_status FROM saved_objects '
                'WHERE bucket=? AND key=? AND version_id=?',
                (bucket, key, version_id)).fetchone()
            if row is not None and row[0] == 'saved':
                return 'kept'
            self.connection.execute(
                "INSERT INTO saved_objects VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(bucket,key,version_id) DO UPDATE SET "
                "fetch_status=excluded.fetch_status,status_code=excluded.status_code,"
                "receipt_id=excluded.receipt_id,saved_at=excluded.saved_at,"
                "note=excluded.note",
                (bucket, key, version_id, outcome['fetch_status'],
                 outcome.get('status_code'), outcome.get('receipt_id'),
                 outcome.get('saved_at'), outcome.get('note')))
            return outcome['fetch_status']

    def saved_object(self, bucket, key, version_id):
        row = self.connection.execute(
            'SELECT fetch_status,status_code,receipt_id,saved_at,note '
            'FROM saved_objects WHERE bucket=? AND key=? AND version_id=?',
            (bucket, key, version_id)).fetchone()
        if row is None:
            return None
        return dict(fetch_status=row[0], status_code=row[1],
                    receipt_id=row[2], saved_at=row[3], note=row[4])

    # ---- 当前状态观察（HEAD） ------------------------------------------

    def record_head(self, bucket, key, response, *, scan_id=None):
        """登记一次 HEAD 观察；它只证明本次观察，不证明扫描快照。"""
        receipt_id = self.save_receipt(response)
        with self.connection:
            self.connection.execute(
                "INSERT INTO key_states VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(bucket,key) DO UPDATE SET observed_at=excluded.observed_at,"
                "status_code=excluded.status_code,current_version_id=excluded.current_version_id,"
                "delete_marker=excluded.delete_marker,scan_id=excluded.scan_id,"
                "receipt_id=excluded.receipt_id",
                (bucket, key, _now(), response.status,
                 response.header('x-amz-version-id') or None,
                 1 if response.header('x-amz-delete-marker').lower() == 'true' else 0,
                 scan_id, receipt_id))

    def key_state(self, bucket, key):
        row = self.connection.execute(
            'SELECT observed_at,status_code,current_version_id,delete_marker,'
            'scan_id,receipt_id FROM key_states WHERE bucket=? AND key=?',
            (bucket, key)).fetchone()
        if row is None:
            return None
        return dict(observed_at=row[0], status_code=row[1],
                    current_version_id=row[2], delete_marker=bool(row[3]),
                    scan_id=row[4], receipt_id=row[5])

    # ---- 通知（至少一次投递，sequencer 仅同 key 可比） ------------------

    def ingest_notification(self, bucket, record, *, now=None):
        """登记一条通知并按同 key 的 sequencer 判定新旧。

        返回 'new'（可触发定向核实）、'stale'（落后于已知水位）或
        'duplicate'（同一 key+sequencer 已见过，可能是重复投递）。
        不同 key 的 sequencer 绝不互相比较。
        """
        key = record['key']
        sequencer = record['sequencer']
        int(sequencer, 16)  # 协议要求十六进制；非法值直接暴露
        received_at = now or _now()
        with self.connection:
            existing = self.connection.execute(
                'SELECT applied FROM notifications '
                'WHERE bucket=? AND key=? AND sequencer=?',
                (bucket, key, sequencer)).fetchone()
            if existing is not None:
                return 'duplicate'
            watermark = self.connection.execute(
                'SELECT sequencer FROM notifications WHERE bucket=? AND key=?',
                (bucket, key)).fetchall()
            if watermark and max(int(s[0], 16) for s in watermark) > int(sequencer, 16):
                disposition = 'stale'
            else:
                disposition = 'new'
            self.connection.execute(
                'INSERT INTO notifications VALUES(?,?,?,?,?,?,0)',
                (bucket, key, record['versionId'], record['eventName'],
                 sequencer, received_at))
            return disposition

    def mark_notification_applied(self, bucket, key, sequencer):
        with self.connection:
            self.connection.execute(
                'UPDATE notifications SET applied=1 '
                'WHERE bucket=? AND key=? AND sequencer=?',
                (bucket, key, sequencer))

    def notification_watermark(self, bucket, key):
        rows = self.connection.execute(
            'SELECT sequencer FROM notifications WHERE bucket=? AND key=?',
            (bucket, key)).fetchall()
        if not rows:
            return None
        return max(rows, key=lambda r: int(r[0], 16))[0]

    # ---- 查询与历史报告 -------------------------------------------------

    def list_versions(self, bucket, key):
        rows = self.connection.execute(
            'SELECT version_id,kind,is_latest,last_modified,source_fields,'
            'first_seen_scan,last_seen_scan FROM object_versions '
            'WHERE bucket=? AND key=? ORDER BY rowid', (bucket, key)).fetchall()
        return [dict(version_id=r[0], kind=r[1], is_latest=bool(r[2]),
                     last_modified=r[3], source=json.loads(r[4]),
                     first_seen_scan=r[5], last_seen_scan=r[6]) for r in rows]

    def known_keys(self, bucket):
        return [r[0] for r in self.connection.execute(
            'SELECT DISTINCT key FROM object_versions WHERE bucket=? ORDER BY key',
            (bucket,))]

    def history_report(self, bucket, prefix):
        """汇总一个前缀的历史资料与不确定范围。"""
        scan_rows = self.connection.execute(
            "SELECT id,state,started_at,finished_at,key_marker,version_id_marker "
            "FROM scans WHERE bucket=? AND prefix=? ORDER BY started_at",
            (bucket, prefix)).fetchall()
        scans = [dict(scan_id=r[0], state=r[1], started_at=r[2],
                      finished_at=r[3], key_marker=r[4],
                      version_id_marker=r[5]) for r in scan_rows]
        completed = any(s['state'] == 'completed' for s in scans)

        keys = []
        for (key,) in self.connection.execute(
                'SELECT DISTINCT key FROM object_versions WHERE bucket=? ORDER BY key',
                (bucket,)):
            versions = self.list_versions(bucket, key)
            for v in versions:
                v['object'] = self.saved_object(bucket, key, v['version_id'])
            state = self.key_state(bucket, key)
            keys.append(dict(key=key, head_observation=state,
                             watermark=self.notification_watermark(bucket, key),
                             versions=versions))

        # 通知提到但枚举从未覆盖到的 key：来源存在性尚不确定
        notified = {r[0] for r in self.connection.execute(
            'SELECT DISTINCT key FROM notifications WHERE bucket=?', (bucket,))}
        enumerated = {k['key'] for k in keys}
        unverified_keys = sorted(notified - enumerated)

        uncertainty = []
        if not completed:
            uncertainty.append(
                'prefix enumeration never completed; versions beyond the last '
                'checkpoint are not yet proven present or absent')
        if unverified_keys:
            uncertainty.append(
                'notifications reference keys never seen by enumeration: '
                + ', '.join(unverified_keys))
        for item in keys:
            for v in item['versions']:
                obj = v['object']
                if obj is not None and obj['fetch_status'] == 'failed':
                    uncertainty.append(
                        f"{item['key']}@{v['version_id']}: object read failed "
                        f"({obj['status_code']}); retry pending")

        counts = dict(
            versions=sum(len(k['versions']) for k in keys),
            delete_markers=sum(1 for k in keys for v in k['versions']
                               if v['kind'] == 'delete_marker'),
            saved_objects=sum(1 for k in keys for v in k['versions']
                              if v['object'] is not None
                              and v['object']['fetch_status'] == 'saved'),
            permanently_missing=sum(1 for k in keys for v in k['versions']
                                    if v['object'] is not None
                                    and v['object']['fetch_status'] == 'permanently_missing'),
            head_verified=sum(1 for k in keys if k['head_observation'] is not None),
        )
        return dict(bucket=bucket, prefix=prefix, enumeration_completed=completed,
                    scans=scans, counts=counts, keys=keys,
                    unverified_notification_keys=unverified_keys,
                    uncertainty=uncertainty)
