"""Bounded archival explanation shared by the installed command and host wrapper."""
import json
import re

_FIELDS = {'schema_id', 'archive_path', 'record_digest', 'execution_attempt_ref',
           'delivery', 'retained', 'api_effect', 'automatic', 'next_actor', 'next_action'}
MAX_DOCUMENT_BYTES = 16384


def _reject():
    raise ValueError('Provider journal archive returned an invalid result; inspect retained state before retrying.')


def validate_archive_document(document):
    if type(document) is not dict or set(document) != _FIELDS:
        _reject()
    for value in document.values():
        if type(value) is not str or not value or not value.isprintable():
            _reject()
    if (document['schema_id'] != 'secs.journal_archive_result.v1'
            or document['delivery'] != 'unconfirmed' or document['next_actor'] != 'provider_operator'
            or re.fullmatch(r'sha256:[0-9a-f]{64}', document['record_digest']) is None
            or re.fullmatch(r'execution_attempt:sha256:[0-9a-f]{64}', document['execution_attempt_ref']) is None
            or not document['archive_path'].endswith('/' + document['record_digest'][7:] + '.archive.json')):
        _reject()
    try:
        if len(json.dumps(document, ensure_ascii=False).encode('utf-8')) > MAX_DOCUMENT_BYTES:
            _reject()
    except UnicodeError:
        _reject()
    return document


def _unique_object(pairs):
    document = {}
    for key, value in pairs:
        if key in document:
            _reject()
        document[key] = value
    return document


def parse_archive_document(raw):
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_DOCUMENT_BYTES:
        _reject()
    try:
        document = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError):
        _reject()
    return validate_archive_document(document)
