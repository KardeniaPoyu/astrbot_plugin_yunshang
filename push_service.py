"""Framework-independent orchestration for durable scheduled batches."""
import asyncio
from time import monotonic
from datetime import timedelta

try:
    from .delivery import completed_window
except ImportError:
    from delivery import completed_window


class PushService:
    def __init__(self, store, targets, prepare, send, *, interval_hours=4):
        self.store = store
        self.targets = list(dict.fromkeys(targets))
        self.prepare = prepare
        self.send = send
        self.interval_hours = interval_hours
        self.last_error = ''
        self._pending_acks = {}

    def _ack(self, job):
        acknowledged = self.store.finish_delivery(job)
        self._pending_acks.pop(job['id'], None)
        return acknowledged

    async def tick(self, now, *, allow_prepare=True):
        began = monotonic()
        current = lambda: now + timedelta(seconds=monotonic() - began)
        start, end = completed_window(now, self.interval_hours)
        stats = {'prepared': False, 'sent': 0, 'failed': 0}
        # Retry a failed local acknowledgement without repeating a remote send.
        # If the database remains unavailable, leave this tick before claiming.
        for job in list(self._pending_acks.values()):
            if self._ack(job):
                stats['sent'] += 1
        ticket = self.store.claim_window(start, end, now) if allow_prepare else None
        if ticket:
            try:
                digest, pages = await asyncio.wait_for(self.prepare(start, end), timeout=120)
                if digest.get('items') and not pages:
                    raise RuntimeError('Nonempty digest has no delivery pages')
                deliveries = [{'target': target, 'page': page, 'payload': payload}
                              for target in self.targets for page, payload in enumerate(pages)]
                stats['prepared'] = self.store.finish_window(ticket, digest, deliveries)
            except Exception as exc:
                self.last_error = type(exc).__name__
                self.store.fail_window(ticket, current(), self.last_error)
                stats['failed'] += 1
        for _ in range(max(1, len(self.targets) * 2)):
            job = self.store.claim_delivery(current())
            if not job:
                break
            if job['target'] not in self.targets:
                self.store.cancel_delivery(job)
                continue
            try:
                result = await asyncio.wait_for(self.send(job['target'], job['payload']), timeout=30)
                if result is False:
                    raise RuntimeError('Platform rejected message')
            except Exception as exc:
                self.last_error = type(exc).__name__
                self.store.fail_delivery(job, current(), self.last_error)
                stats['failed'] += 1
            else:
                self._pending_acks[job['id']] = job
                if self._ack(job):
                    stats['sent'] += 1
        return stats
