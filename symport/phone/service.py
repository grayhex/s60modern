"""Capability-based device access. Mounted volumes are never inferred to be phones.

Roots are explicitly enrolled by the operator, not accepted from HTTP clients.
No symlinks, recursive deletion, shell execution or implicit installation.
"""
from __future__ import annotations

import json
import os
import threading
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from . import capabilities
from .bridge import PCBridge
from .contracts import Transport
from .firmware import assess
from .local import LocalTransports


class DeviceManager:
    def __init__(self, repo: Path, transport: Transport | None = None):
        self.transport = transport or (
            PCBridge(os.environ['SYMPORT_BRIDGE_URL'], os.getenv('SYMPORT_BRIDGE_KEY', ''))
            if os.getenv('SYMPORT_BRIDGE_URL') else
            LocalTransports(repo)
        )
        self.lock = threading.Lock()
        self.observed = capabilities.ObservedWrites()
        self.rows: dict[str, dict] = {}
        self.journal_lock = threading.Lock()
        self.journal = repo / 'build' / 'symport' / 'device-manager' / 'operations.jsonl'

    def execute(self, data: dict) -> dict:
        allowed = {'discover', 'history', 'list', 'download', 'upload', 'transfer',
                   'mkdir', 'delete', 'backup', 'restore-preview', 'restore', 'contacts-stage',
                   'contacts-read', 'contacts-import', 'media-upload', 'software-transfer',
                   'apps-inventory', 'calendar-read', 'media-list', 'media-info', 'media-chunk', 'media-thumbnail',
                   'device-details', 'device-imei', 'personal-read', 'personal-preview', 'personal-apply',
                   'personal-calendar-import', 'personal-export', 'personal-backup', 'personal-backup-download', 'sms-send', 'connection-diagnose', 'connection-status', 'connection-host', 'connection-select', 'capabilities'}
        if not isinstance(data.get('action'), str) or data['action'] not in allowed:
            raise ValueError('Unknown device operation')
        if data['action'] not in ('discover', 'history', 'connection-status', 'connection-host'):
            if not isinstance(data.get('device'), str) or not isinstance(data.get('path', ''), str):
                raise ValueError('Device and path must be strings')
            if len(data.get('path', '')) > 1024 or not isinstance(data.get('content', ''), str):
                raise ValueError('Invalid path or content')
        if data['action'] == 'media-upload' and data.get('kind') not in ('audio', 'video'):
            raise ValueError('Выберите тип медиа: аудио или видео')
        if data['action'] in ('contacts-stage', 'contacts-import'):
            from .contacts import normalize
            data = dict(data, contacts=normalize(data.get('contacts')))
        with self.lock:
            if data.get('action') in ('discover', 'capabilities'):
                devices = self._observe()
                if data['action'] == 'capabilities':
                    # Read-only re-probe of one connection; never writes, sends or installs.
                    row = next((d for d in devices if d['id'] == data['device']), None)
                    if row is None:
                        raise ValueError('Unknown connection; discover the phone again')
                    return {'device': row}
                from .models import catalog
                return {'devices': devices, 'models': catalog(), 'connections': self.transport.diagnostics() if hasattr(self.transport, 'diagnostics') else [], 'limitations': {
                    'contacts': 'Импорт Google CSV / Apple vCard, передача vCard на карту или запись через Gammu с чтением для проверки',
                    'sms': 'Requires a tested Gammu or native Agent adapter',
                    'applications': 'Mounted volumes do not expose the installed application registry; apps-inventory lists SIS package files only',
                    'calendar': 'Calendar and tasks require Gammu storage access; use Nokia Suite iCalendar export if the selected driver cannot read them',
                    'backup': 'File backup covers only the selected folder, not contacts, SMS, applications or firmware',
                    'bluetooth': 'OBEX pairing and service discovery must run on the PC connected to the phone',
                }}
            if data.get('action') == 'connection-select':
                # The user chose a route in the app. Nothing is sent to the phone; only the choice is journalled.
                if data['device'] not in self.rows:
                    raise ValueError('Unknown connection; discover the phone again')
                self.record('connection-select', 'success')
                return {'status': 'selected', 'device': data['device']}
            if data.get('action') == 'connection-host':
                return self.transport.execute(data)
            if data.get('action') == 'connection-status':
                return {'connections': self.transport.diagnostics() if hasattr(self.transport, 'diagnostics') else []}
            if data.get('action') == 'history':
                with self.journal_lock:
                    if not self.journal.exists():
                        return {'operations': []}
                    return {'operations': [json.loads(x) for x in self.journal.read_text().splitlines()[-100:]]}
            status = 'failed'
            try:
                try:
                    if hasattr(self, "before_execute"):
                        self.before_execute(data)
                    result = self.transport.execute(data)
                except zipfile.BadZipFile as exc:
                    raise ValueError('Invalid ZIP archive') from exc
                status = 'unverified' if result.get('status') in ('partial-or-unverified', 'submitted-to-network') else 'success'
                self.observed.record(self.rows.get(data.get('device')), data['action'], result)
                return result
            finally:
                if data['action'] not in ('media-info', 'media-chunk', 'media-thumbnail'):
                    self.record(data['action'], status)

    def _observe(self) -> list[dict]:
        """Probe every connection and return normalised rows (matrix = source of truth)."""
        devices = []
        for observed in self.transport.devices():
            item = capabilities.finalize(dict(observed))
            imei = item.get('imei')
            if isinstance(imei, str) and imei.isdigit():
                item['imei'] = '•' * max(0, len(imei) - 4) + imei[-4:]
            item['firmware_update'] = assess(item)
            from .models import presentation
            item['model_profile'] = presentation(str(item.get('model') or ''), rm=item.get('rm'), product_code=item.get('product_code'))
            if item['model_profile'] and item['model_profile'].get('platform_family') == 'symbian3':
                from .platforms import release
                current = release(item.get('os_release'))
                source = item.get('os_source')
                item['model_profile']['release_evidence'] = {'value': current if source else None,
                    'status': 'observed' if current and source else 'unknown', 'source': str(source or '')[:120]}
                if current and source:
                    item['model_profile']['symbian'] = current
            devices.append(self.observed.overlay(item))
        # Rows of connections that vanished must not keep proving anything.
        self.rows = {d['id']: d for d in devices}
        for gone in set(self.observed.proofs) - set(self.rows):
            self.observed.proofs.pop(gone, None)
        return devices

    def record(self, action: str, status: str) -> None:
        """Only logical operations, never per-chunk media traffic or private data."""
        with self.journal_lock:
            self.journal.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Never retain payloads, phone paths, contacts, SMS or exception text.
            record = {'time': datetime.now(UTC).isoformat(),
                      'action': str(action)[:40], 'status': status}
            if self.journal.exists() and self.journal.stat().st_size > 1024 * 1024:
                self.journal.unlink()
            with self.journal.open('a', encoding='utf-8') as log:
                os.chmod(self.journal, 0o600)
                log.write(json.dumps(record) + '\n')
