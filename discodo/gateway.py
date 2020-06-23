import sys
import time
import json
import asyncio
import threading
import websockets
import concurrent.futures
from collections import deque


class keepAlive(threading.Thread):
    def __init__(self, ws, interval: int, *args, **kwargs):
        threading.Thread.__init__(self, *args, **kwargs)
        self.daemon = True

        self.ws = ws
        self.interval = interval
        self.Stopped = threading.Event()
        self.latency = None
        self.recent_latencies = deque(maxlen=20)

        self.last_ack = self.last_send = time.perf_counter()
        self.timeout = ws.heartbeatTimeout
        self.threadId = ws.threadId

    def run(self):
        while not self.Stopped.wait(self.interval):
            if (self.last_ack + self.timeout) < time.perf_counter():
                Runner = asyncio.run_coroutine_threadsafe(
                    self.ws.close(4000), self.ws.loop)

                try:
                    Runner.result()
                except:
                    pass
                finally:
                    return self.stop()

            payload = {
                'op': self.ws.HEARTBEAT,
                'd': int(time.time() * 1000)
            }
            Runner = asyncio.run_coroutine_threadsafe(
                self.ws.sendJson(payload), self.ws.loop)
            try:
                totalBlocked = 0
                while True:
                    try:
                        Runner.result(10)
                    except concurrent.futures.TimeoutError:
                        totalBlocked += 10
                        print(f'Heartbeat blocked for more than {totalBlocked} seconds.')
            except:
                return self.stop()
            else:
                self._lastSend = time.perf_counter()

    def ack(self):
        self._lastAck = time.perf_counter()
        self.latency = self._lastAck - self._lastSend
        self.recent_acks.append(self.latency)

    def stop(self):
        self.Stopped.set()


class VoiceSocket(websockets.client.WebSocketClientProtocol):
    IDENTIFY = 0
    SELECT_PROTOCOL = 1
    READY = 2
    HEARTBEAT = 3
    SESSION_DESCRIPTION = 4
    SPEAKING = 5
    HEARTBEAT_ACK = 6
    RESUME = 7
    HELLO = 8
    RESUMED = 9
    CLIENT_DISCONNECT = 13

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @classmethod
    async def connect(cls, client, loop=asyncio.get_event_loop(), resume=False):
        ws = await websockets.connect(f'wss://{client.endpoint}/?v=4', loop=loop, klass=cls, compression=None)
        ws.client = client

        ws.heartbeatTimeout = 60.0
        ws.threadId = threading.get_ident()

        if not resume:
            await ws.identify()
        else:
            await ws.resume()

        return ws

    @property
    async def latency(self):
        return self._keepAliver.latency if self._keepAliver else None

    @property
    async def averageLatency(self):
        if not self._keepAliver:
            return None

        return sum(self._keepAliver.recent_latencies) / len(self._keepAliver.recent_latencies)

    async def sendJson(self, data):
        await self.send(json.dumps(data))

    async def identify(self):
        payload = {
            'op': self.IDENTIFY,
            'd': {
                'server_id': str(self.client.server_id),
                'user_id': str(self.client.user_id),
                'session_id': self.client.session_id,
                'token': self.client.token
            }
        }
        await self.sendJson(payload)

    async def resume(self):
        payload = {
            'op': self.RESUME,
            'd': {
                'server_id': str(self.client.server_id),
                'user_id': str(self.client.user_id),
                'session_id': self.client.session_id,
                'token': self.client.token
            }
        }
        await self.sendJson(payload)

    async def receive(self, message):
        Operation, Data = message['op'], message.get('d')

        if Operation == self.READY:
            pass
        elif Operation == self.HEARTBEAT_ACK:
            self._keepAliver.ack()
        elif Operation == self.INVALIDATE_SESSION:
            await self.identify()
        elif Operation == self.SESSION_DESCRIPTION:
            pass
        elif Operation == self.HELLO:
            interval = Data['heartbeat_interval'] / 1000.0
            self._keepAliver = keepAlive(self, min(interval, 5.0))
            self._keepAliver.start()

    async def polling(self):
        try:
            Message = await asyncio.wait_for(self.recv(), timeout=30.0)
            await self.receive(json.loads(Message))
        except websockets.exceptions.ConnectionClosed as exc:
            raise websockets.exceptions.ConnectionClosed

    async def close(self, *args, **kwargs):
        if self._keepAliver:
            self._keepAliver.stop()

        await super().close_connection(*args, **kwargs)
