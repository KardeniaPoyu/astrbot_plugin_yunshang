import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from delivery import DeliveryStore
from push_service import PushService

NOW=datetime(2026,11,12,12,tzinfo=timezone(timedelta(hours=8)))

class PushServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=DeliveryStore(Path(self.tmp.name)/'state.db')
        self.store.bootstrap(NOW-timedelta(minutes=1))
        self.prepares=0
        self.sent=[]

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    async def prepare(self,start,end):
        self.prepares+=1
        return {'items':[{'id':'1'}]}, [{'kind':'text','text':'page one'},{'kind':'text','text':'page two'}]

    async def send(self,target,payload):
        self.sent.append((target,payload['text']))
        return True

    async def test_partial_group_failure_retries_only_failed_pages(self):
        failures=0
        async def send(target,payload):
            nonlocal failures
            if target=='group-b' and failures==0:
                failures+=1
                return False
            return await self.send(target,payload)
        svc=PushService(self.store,['group-a','group-b','group-c'],self.prepare,send)
        first=await svc.tick(NOW)
        self.assertEqual(first['sent'],4)
        self.assertEqual(first['failed'],1)
        second=await svc.tick(NOW+timedelta(seconds=62))
        self.assertEqual(second['sent'],2)
        await svc.tick(NOW+timedelta(minutes=5))
        self.assertEqual(len(self.sent),6)
        self.assertEqual(len(set(self.sent)),6)
        self.assertEqual(self.prepares,1)

    async def test_first_enable_does_not_send_the_old_closed_window(self):
        other=DeliveryStore(Path(self.tmp.name)/'new.db')
        try:
            other.bootstrap(NOW)
            await PushService(other,['group-a'],self.prepare,self.send).tick(NOW)
            self.assertEqual(self.prepares,0)
            self.assertEqual(self.sent,[])
        finally:other.close()

    async def test_removed_group_is_not_sent_persisted_pages_after_restart(self):
        ticket=self.store.claim_window(NOW-timedelta(hours=4),NOW,NOW)
        self.store.finish_window(ticket,{'items':[{'id':'1'}]},[
            {'target':target,'page':page,'payload':{'kind':'text','text':str(page)}}
            for target in ('removed-group','group-a') for page in (0,1)])
        svc=PushService(self.store,['group-a'],self.prepare,self.send)
        await svc.tick(NOW)
        await svc.tick(NOW+timedelta(seconds=30))
        self.assertEqual(self.sent,[('group-a','0'),('group-a','1')])
        self.assertEqual(self.prepares,0)

    async def test_failed_collection_is_not_committed_as_empty(self):
        calls=0
        async def prepare(start,end):
            nonlocal calls
            calls+=1
            if calls==1:raise TimeoutError()
            return await self.prepare(start,end)
        svc=PushService(self.store,['group-a'],prepare,self.send)
        await svc.tick(NOW)
        self.assertEqual(self.sent,[])
        await svc.tick(NOW+timedelta(seconds=30))
        self.assertEqual(calls,1)
        await svc.tick(NOW+timedelta(seconds=62))
        self.assertEqual(calls,2)
        self.assertEqual(len(self.sent),2)

    async def test_expired_login_does_not_block_prepared_deliveries(self):
        ticket=self.store.claim_window(NOW-timedelta(hours=4),NOW,NOW)
        self.store.finish_window(ticket,{'items':[{'id':'1'}]},[
            {'target':'group-a','page':0,'payload':{'kind':'text','text':'ready'}}])
        svc=PushService(self.store,['group-a'],self.prepare,self.send)
        await svc.tick(NOW,allow_prepare=False)
        self.assertEqual(self.sent,[('group-a','ready')])
        self.assertEqual(self.prepares,0)

    async def test_failed_ack_retries_database_without_resending(self):
        svc=PushService(self.store,['group-a'],self.prepare,self.send)
        original=self.store.finish_delivery
        failures=0
        def acknowledge(job):
            nonlocal failures
            if failures==0:
                failures+=1
                raise OSError('database unavailable')
            return original(job)
        with patch.object(self.store,'finish_delivery',side_effect=acknowledge):
            with self.assertRaises(OSError): await svc.tick(NOW)
            await svc.tick(NOW+timedelta(seconds=181))
        self.assertEqual(self.sent,[('group-a','page one'),('group-a','page two')])

    async def test_empty_batch_does_not_send_or_repeat_preparation(self):
        async def prepare(start,end):
            self.prepares+=1
            return {'items':[]},[]
        svc=PushService(self.store,['group-a'],prepare,self.send)
        await svc.tick(NOW)
        await svc.tick(NOW+timedelta(minutes=1))
        self.assertEqual(self.prepares,1)
        self.assertEqual(self.sent,[])

    async def test_cancelled_preparation_can_recover_after_lease(self):
        began=asyncio.Event()
        async def prepare(start,end):
            began.set()
            await asyncio.Event().wait()
        svc=PushService(self.store,['group-a'],prepare,self.send)
        task=asyncio.create_task(svc.tick(NOW))
        await began.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        recovered=PushService(self.store,['group-a'],self.prepare,self.send)
        await recovered.tick(NOW+timedelta(seconds=181))
        self.assertEqual(len(self.sent),2)

    async def test_delivery_lease_uses_time_after_slow_preparation(self):
        elapsed=[1000.0]
        async def prepare(start,end):
            elapsed[0]+=90
            return await self.prepare(start,end)
        svc=PushService(self.store,['group-a'],prepare,self.send)
        claimed=[]
        original=self.store.claim_delivery
        def claim(now):
            claimed.append(now)
            return original(now)
        with patch('push_service.monotonic',side_effect=lambda:elapsed[0]),patch.object(self.store,'claim_delivery',side_effect=claim):
            await svc.tick(NOW)
        self.assertGreaterEqual(claimed[0],NOW+timedelta(seconds=90))

if __name__=='__main__':unittest.main()
