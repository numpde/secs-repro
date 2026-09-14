"""Closed, bounded presentation contract shared by inspector and host wrapper.

This validates output shape, not API causes or recovery policy. It imports only
stdlib so deployment tooling can load this exact source without provider setup.
"""
import json
import re
from datetime import datetime

MAX_DOCUMENT_BYTES = 32768
_ENVELOPE = {'schema_id', 'record_count', 'observed_at', 'current_automation', 'records'}
_COMMON = {'record_digest', 'provider_ref', 'job_ref', 'provider_attempt_key', 'current_automation', 'next_actor', 'next_action', 'stage', 'restart_behavior'}
_TERMINAL = {'execution_attempt_ref', 'operation', 'command_fingerprint', 'command_bytes', 'command_retained', 'delivery'}
_RECOVERY = {'action', 'code', 'description', 'detail', 'request_id', 'observed_state', 'execution_attempt_ref', 'operation', 'command_fingerprint', 'command_retained', 'delivery', 'automatic_resends', 'automatic_reads', 'new_work', 'next_actor', 'next_action'}


def _reject():
    raise ValueError('Provider journal inspection returned an invalid document.')


def _fields(value, required):
    if type(value) is not dict or set(value) != required:
        _reject()


def _text(value, limit=4096):
    if type(value) is not str or not value:
        _reject()
    try:
        if len(value.encode('utf-8')) > limit:
            _reject()
    except UnicodeError:
        _reject()


def validate_inspection_document(document):
    _fields(document, _ENVELOPE)
    if document['schema_id'] != 'secs.journal_inspection.v1' or document['current_automation'] != 'stopped':
        _reject()
    _text(document['observed_at'], 64)
    try:
        if datetime.fromisoformat(document['observed_at']).utcoffset() is None:
            _reject()
    except ValueError:
        _reject()
    records = document['records']
    if type(records) is not list or len(records) > 1 or type(document['record_count']) is not int or document['record_count'] != len(records):
        _reject()
    for record in records:
        if type(record) is not dict:
            _reject()
        stage = record.get('stage')
        _text(stage, 32)
        required = set(_COMMON)
        if stage == 'active':
            required |= {'execution_attempt_ref'}
        elif stage in {'terminal', 'terminal_held', 'terminal_reconciling'}:
            required |= _TERMINAL
            if stage != 'terminal':
                required |= {'recovery_on_restart'}
        elif stage != 'start':
            _reject()
        _fields(record, required)
        if (type(record['record_digest']) is not str
                or re.fullmatch(r'sha256:[0-9a-f]{64}', record['record_digest']) is None):
            _reject()
        if record['current_automation'] != 'stopped':
            _reject()
        for name, value in record.items():
            if name not in {'command_bytes', 'command_retained', 'recovery_on_restart'}:
                _text(value)
        if stage.startswith('terminal'):
            if (record['operation'] not in {'complete', 'fail'} or record['delivery'] != 'unconfirmed'
                    or record['command_retained'] is not True or type(record['command_bytes']) is not int
                    or not 1 <= record['command_bytes'] <= 3 * 1024 * 1024):
                _reject()
        if 'recovery_on_restart' in record:
            recovery = record['recovery_on_restart']
            _fields(recovery, _RECOVERY)
            for name, value in recovery.items():
                if name != 'command_retained' and not (name == 'observed_state' and value is None):
                    _text(value)
            for name in ('execution_attempt_ref', 'operation', 'command_fingerprint', 'command_retained', 'delivery'):
                if type(recovery[name]) is not type(record[name]) or recovery[name] != record[name]:
                    _reject()
    try:
        if len(json.dumps(document, ensure_ascii=False).encode('utf-8')) > MAX_DOCUMENT_BYTES:
            _reject()
    except UnicodeError:
        _reject()
    return document


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            _reject()
        result[name] = value
    return result


def parse_inspection_document(raw):
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_DOCUMENT_BYTES:
        _reject()
    try:
        document = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError):
        _reject()
    return validate_inspection_document(document)
