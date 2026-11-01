"""Paged delivery of verified frozen text, without retaining its body in a draft."""

from collections import OrderedDict
import codecs
import hashlib
import json
import os
import sqlite3

from . import frozen_refs
from .artifacts import _signature
from .errors import ConflictError, ValidationError
from .workflow_store import encoded


def canonical_content(value):
    return encoded(value).encode('utf-8')


class MaterialReader:
    def __init__(self, limit=64):
        self.cache = OrderedDict()
        self.limit = limit

    def describe(self, store, ref):
        key = (str(store.root), ref)
        old = self.cache.get(key)
        if old:
            if old['path'] is None or _signature(old['path'].stat()) == old['signature']:
                self.cache.move_to_end(key)
                return old
            del self.cache[key]
        with store._read() as db:
            resolved = frozen_refs.resolve(db, store.root, ref)
            if resolved['outcome'] != 'resolved':
                raise ValidationError(f"material_unavailable: {resolved['reason']}")
            project = store._project(db)['project_id']
            if resolved.get('object'):
                resolved, path = frozen_refs.open(db, store.root, ref)
                if resolved['outcome'] != 'resolved' or path is None or not path.is_file():
                    raise ValidationError(f"material_unavailable: {resolved.get('reason') or 'not_a_text_file'}")
                inline = None
            else:
                parsed = frozen_refs.parse(ref)
                row = db.execute('SELECT content FROM publication_items WHERE publication_id=? AND item_id=?',
                                 (parsed.get('publication_id'), parsed.get('item_id'))).fetchone()
                if not row:
                    raise ValidationError('material_unavailable: no readable content')
                content = json.loads(row[0])
                if not content:
                    raise ValidationError('material_unavailable: no readable content')
                inline = canonical_content(content)
                path = None
                inline_lookup = (store.db_path, parsed['publication_id'], parsed['item_id'])
        digest = hashlib.sha256()
        decoder = codecs.getincrementaldecoder('utf-8')()
        signature = None
        try:
            if path is not None:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, 'rb') as stream:
                    signature = _signature(os.fstat(stream.fileno()))
                    size = 0
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        digest.update(chunk)
                        decoder.decode(chunk)
                        size += len(chunk)
                    decoder.decode(b'', final=True)
                    if _signature(os.fstat(stream.fileno())) != signature:
                        raise ConflictError('material_changed: read the frozen material again')
            else:
                size = len(inline)
                digest.update(inline)
                decoder.decode(inline, final=True)
        except UnicodeDecodeError as exc:
            raise ValidationError('material_not_utf8') from exc
        checksum = digest.hexdigest()
        identity = {'project_id': project, 'ref': ref, 'object': resolved.get('object'),
                    'subpath': resolved.get('subpath'), 'digest': checksum, 'total_bytes': size}
        snapshot_id = 'material-' + hashlib.sha256(encoded(identity).encode()).hexdigest()
        result = {**identity, 'snapshot_id': snapshot_id, 'path': path,
                  'inline': inline if inline is None or len(inline) <= 65536 else None,
                  'inline_lookup': inline_lookup if path is None and len(inline) > 65536 else None,
                  'signature': signature}
        self.cache[key] = result
        while len(self.cache) > self.limit:
            self.cache.popitem(last=False)
        return result

    def bytes(self, material, start, end):
        if material['path'] is None:
            if material['inline'] is not None:
                return material['inline'][start:end]
            path, publication, item = material['inline_lookup']
            with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
                row = db.execute('SELECT substr(CAST(content AS BLOB),?,?) FROM publication_items '
                                 'WHERE publication_id=? AND item_id=?', (start + 1, end-start, publication, item)).fetchone()
            if not row:
                raise ConflictError('material_changed: read the frozen material again')
            return row[0]
        fd = os.open(material['path'], os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            if _signature(os.fstat(stream.fileno())) != material['signature']:
                raise ConflictError('material_changed: read the frozen material again')
            stream.seek(start)
            data = stream.read(end - start)
            if _signature(os.fstat(stream.fileno())) != material['signature']:
                raise ConflictError('material_changed: read the frozen material again')
            return data

    def page(self, store, ref=None, cursor=None, limit=4096):
        if cursor:
            if ref is not None and ref != cursor.get('material_ref'):
                raise ValidationError('Material cursor does not match reference')
            ref = cursor.get('material_ref')
        material = self.describe(store, ref)
        if cursor and cursor.get('snapshot_id') != material['snapshot_id']:
            raise ConflictError('material_changed: read the frozen material again')
        start = cursor.get('offset') if cursor else 0
        if type(start) is not int or not 0 <= start <= material['total_bytes']:
            raise ValidationError('Invalid material cursor offset')
        end = min(start + max(4, min(int(limit or 4096), 16384)), material['total_bytes'])
        data = self.bytes(material, start, end)
        while data:
            try:
                chunk = data.decode('utf-8')
                break
            except UnicodeDecodeError as exc:
                if exc.reason != 'unexpected end of data' or exc.end != len(data):
                    raise ValidationError('Invalid UTF-8 material boundary') from exc
                data = data[:exc.start]
        else:
            chunk = ''
        end = start + len(data)
        return {'part': 'material', 'offset_unit': 'utf8_byte', 'material_ref': ref,
                'snapshot_id': material['snapshot_id'], 'manifest_digest': material['digest'],
                'offset': start, 'end': end, 'total_bytes': material['total_bytes'], 'chunk': chunk,
                'cursor': {'part': 'material', 'material_ref': ref, 'snapshot_id': material['snapshot_id'], 'offset': end}
                          if end < material['total_bytes'] else None}


def material_coverage(db, host, session, generation, material):
    row = db.execute('SELECT 1 FROM workflow_project p JOIN workflow_sessions w ON w.session_id=p.main_session_id '
                     'WHERE p.id=1 AND w.host_id=? AND w.session_id=? AND w.role=\'main\' AND w.detached=0',
                     (host, session)).fetchone()
    if not row:
        return {'complete': False, 'covered_bytes': 0}
    ranges = db.execute('SELECT start_offset,end_offset FROM evidence_read_receipts '
                        'WHERE generation=? AND host_id=? AND session_id=? AND part=\'material\' '
                        'AND snapshot_id=? AND digest=? AND model_message_id IS NOT NULL ORDER BY start_offset,end_offset',
                        (generation, host, session, material['snapshot_id'], material['digest']))
    merged = []
    for start, end in ranges:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    covered = sum(end-start for start, end in merged)
    return {'complete': bool(material['total_bytes'] and len(merged) == 1 and merged[0] == (0, material['total_bytes'])),
            'covered_bytes': covered}
