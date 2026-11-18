import json


def decode_versions(body):
    """解释 ListObjectVersions 风格的一页响应。

    返回 Versions/DeleteMarkers 两组条目以及成对的续页标记；
    VersionId 是不透明身份，这里不做任何排序或合并。
    """
    payload = json.loads(body)
    return {
        'versions': list(payload.get('Versions', [])),
        'delete_markers': list(payload.get('DeleteMarkers', [])),
        'is_truncated': bool(payload.get('IsTruncated', False)),
        'next_key_marker': payload.get('NextKeyMarker'),
        'next_version_id_marker': payload.get('NextVersionIdMarker'),
    }


def decode(body, headers):
    return [{'key': x['Key'] + '@' + x['VersionId'], 'value': x}
            for x in decode_versions(body)['versions']]
