import json
import xml.etree.ElementTree as ET

def decode(body, headers):
    return [{'key': x['Key'], 'value': x} for x in json.loads(body)['Contents']]
