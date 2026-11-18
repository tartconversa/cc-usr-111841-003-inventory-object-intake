import json
import xml.etree.ElementTree as ET

def decode(body, headers):
    return [{'key': x['Key']+'@'+x['VersionId'], 'value': x} for x in json.loads(body)['Versions']]
