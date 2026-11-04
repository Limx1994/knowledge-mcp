from contextlib import ExitStack
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import Mock, patch

import pywintypes

from runtime import ServiceState, SpawnGate, run_bridge
from knowledge_store import StoreError


class RuntimeTests(unittest.TestCase):
    def state(self):
        store = Mock()
        store.root = Path(__file__).resolve().parents[1]
        store.guard = threading.RLock()
        state = ServiceState(store)
        state.status = Mock(return_value=dict(ok=True))
        return state

    def test_completed_requests_release_workers(self):
        state = self.state()
        threads = []
        channels = []
        thread_type = threading.Thread

        def worker(*args, **kwargs):
            thread = thread_type(*args, **kwargs)
            threads.append(thread)
            return thread

        def pipe(*args):
            if len(channels) == 100:
                state.stopping.set()
            return Mock()

        def channel(*args, **kwargs):
            result = Mock()
            result.receive.return_value = dict(root=str(state.store.root), command='status')
            channels.append(result)
            return result

        with ExitStack() as stack:
            stack.enter_context(patch('runtime.pipe_security', return_value=None))
            stack.enter_context(patch('runtime.win32pipe.CreateNamedPipe', side_effect=pipe))
            stack.enter_context(patch('runtime.win32pipe.ConnectNamedPipe'))
            stack.enter_context(patch('runtime.win32file.GetOverlappedResult'))
            stack.enter_context(patch('runtime.PipeChannel', side_effect=channel))
            stack.enter_context(patch('runtime.threading.Thread', side_effect=worker))
            state.serve()
            for thread in threads:
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
        self.assertEqual(state.status.call_count, 100)
        self.assertEqual(state.workers, set())
        self.assertEqual(state.channels, set())
        for result in channels:
            result.send.assert_called_once_with(dict(ok=True))
            result.close.assert_called_once()

    def test_close_waits_for_worker_cleanup(self):
        state = self.state()
        receiving = threading.Event()
        disconnected = threading.Event()
        cleaning = threading.Event()
        release = threading.Event()
        result = Mock()

        def receive():
            receiving.set()
            if not disconnected.wait(5):
                raise RuntimeError('test disconnect timeout')
            return dict(root=str(state.store.root), command='status')

        def disconnect():
            disconnected.set()
            if threading.current_thread() is worker:
                cleaning.set()
                if not release.wait(5):
                    raise RuntimeError('test cleanup timeout')

        result.receive.side_effect = receive
        result.close.side_effect = disconnect
        worker = threading.Thread(target=state.handle, args=(result,))
        with state.guard:
            state.channels.add(result)
            state.workers.add(worker)
            worker.start()
        closer = threading.Thread(target=state.close)
        try:
            self.assertTrue(receiving.wait(5))
            with patch('runtime.connect_pipe', return_value=Mock()):
                closer.start()
                self.assertTrue(cleaning.wait(5))
                self.assertTrue(closer.is_alive())
                state.store.close.assert_not_called()
                release.set()
                closer.join(timeout=5)
                self.assertFalse(closer.is_alive())
        finally:
            disconnected.set()
            release.set()
            worker.join(timeout=5)
            if closer.ident is not None:
                closer.join(timeout=5)
        state.store.close.assert_called_once()
        self.assertEqual(state.workers, set())
        self.assertEqual(state.channels, set())

    def test_start_failure_releases_channel(self):
        state = self.state()
        channel = Mock()
        worker = Mock()
        worker.start.side_effect = RuntimeError('start failed')
        with ExitStack() as stack:
            stack.enter_context(patch('runtime.pipe_security', return_value=None))
            stack.enter_context(patch('runtime.win32pipe.CreateNamedPipe', return_value=Mock()))
            stack.enter_context(patch('runtime.win32pipe.ConnectNamedPipe'))
            stack.enter_context(patch('runtime.win32file.GetOverlappedResult'))
            stack.enter_context(patch('runtime.PipeChannel', return_value=channel))
            stack.enter_context(patch('runtime.threading.Thread', return_value=worker))
            record = stack.enter_context(patch('runtime.crashlog.record_current'))
            with self.assertLogs('knowledge.runtime', level='ERROR'):
                state.serve()
        record.assert_called_once()
        self.assertTrue(state.stopping.is_set())
        self.assertEqual(state.workers, set())
        self.assertEqual(state.channels, set())
        channel.close.assert_called_once()

    def test_close_during_worker_start(self):
        state = self.state()
        starting = threading.Event()
        release = threading.Event()
        closing = threading.Event()
        thread_type = threading.Thread
        workers = []
        channel = Mock()
        channel.receive.return_value = dict(root=str(state.store.root), command='status')

        def worker(*args, **kwargs):
            thread = thread_type(*args, **kwargs)
            start = thread.start

            def delayed_start():
                starting.set()
                if not release.wait(5):
                    raise RuntimeError('test start timeout')
                start()

            thread.start = delayed_start
            workers.append(thread)
            return thread

        def wake(*args):
            closing.set()
            return Mock()

        listener = thread_type(target=state.serve)
        closer = thread_type(target=state.close)
        with ExitStack() as stack:
            stack.enter_context(patch('runtime.pipe_security', return_value=None))
            stack.enter_context(patch('runtime.win32pipe.CreateNamedPipe', return_value=Mock()))
            stack.enter_context(patch('runtime.win32pipe.ConnectNamedPipe'))
            stack.enter_context(patch('runtime.win32file.GetOverlappedResult'))
            stack.enter_context(patch('runtime.PipeChannel', return_value=channel))
            stack.enter_context(patch('runtime.threading.Thread', side_effect=worker))
            stack.enter_context(patch('runtime.connect_pipe', side_effect=wake))
            try:
                listener.start()
                self.assertTrue(starting.wait(5))
                closer.start()
                self.assertTrue(closing.wait(5))
                state.store.close.assert_not_called()
                release.set()
            finally:
                release.set()
                state.stopping.set()
                listener.join(timeout=5)
                if closer.ident is not None:
                    closer.join(timeout=5)
                for thread in workers:
                    thread.join(timeout=5)
        self.assertFalse(listener.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertEqual(state.workers, set())
        self.assertEqual(state.channels, set())
        state.store.close.assert_called_once()


class ServeLoopTests(unittest.TestCase):
    """P0-1/P1-1 回归：接受循环不得因预连接或连上即断而卡死或退出。"""

    def state(self):
        store = Mock()
        store.root = Path(__file__).resolve().parents[1]
        store.guard = threading.RLock()
        state = ServiceState(store)
        state.status = Mock(return_value=dict(ok=True))
        return state

    def test_serve_survives_aborted_and_preconnected_clients(self):
        state = self.state()
        thread_type = threading.Thread
        listeners = []
        outcomes = iter([232, 535, None, None, None])
        started = []

        def create(*args):
            handle = Mock(name=f'pipe{len(listeners)}')
            listeners.append(handle)
            if len(listeners) >= 6:
                state.stopping.set()
            return handle

        def connect(handle):
            outcome = next(outcomes)
            if outcome is not None:
                raise pywintypes.error(outcome, 'ConnectNamedPipe', 'probe')

        def channel(*args, **kwargs):
            result = Mock()
            result.receive.return_value = dict(root=str(state.store.root), command='status')
            return result

        def worker(*args, **kwargs):
            thread = thread_type(*args, **kwargs)
            started.append(thread)
            return thread

        with ExitStack() as stack:
            stack.enter_context(patch('runtime.pipe_security', return_value=None))
            stack.enter_context(patch('runtime.win32pipe.CreateNamedPipe', side_effect=create))
            stack.enter_context(patch('runtime.win32pipe.ConnectNamedPipe', side_effect=connect))
            stack.enter_context(patch('runtime.PipeChannel', side_effect=channel))
            stack.enter_context(patch('runtime.threading.Thread', side_effect=worker))
            record = stack.enter_context(patch('runtime.crashlog.record_current'))
            state.serve()
            for thread in started:
                thread.join(timeout=5)
        # 232/535 只换新实例继续监听：两次正常连接仍被分发，服务未被判定为监听故障。
        self.assertEqual(len(started), 2)
        record.assert_not_called()

    def test_serve_creates_next_listener_before_dispatch(self):
        state = self.state()
        order = []
        listeners = []

        def create(*args):
            handle = Mock(name=f'pipe{len(listeners)}')
            listeners.append(handle)
            order.append(('create', len(listeners)))
            if len(listeners) >= 3:
                state.stopping.set()
            return handle

        def connect(handle):
            order.append(('connect', listeners.index(handle) + 1))

        def channel(*args, **kwargs):
            order.append('dispatch')
            return Mock()

        with ExitStack() as stack:
            stack.enter_context(patch('runtime.pipe_security', return_value=None))
            stack.enter_context(patch('runtime.win32pipe.CreateNamedPipe', side_effect=create))
            stack.enter_context(patch('runtime.win32pipe.ConnectNamedPipe', side_effect=connect))
            stack.enter_context(patch('runtime.PipeChannel', side_effect=channel))
            stack.enter_context(patch('runtime.threading.Thread', return_value=Mock()))
            state.serve()
        # 每次分发前都必须已有下一个监听实例在等待，消除零监听窗口。
        self.assertGreaterEqual(len(listeners), 3)
        self.assertEqual(order[0], ('create', 1))
        self.assertEqual(order[1], ('connect', 1))
        self.assertEqual(order[2], ('create', 2))
        self.assertEqual(order[3], 'dispatch')


class SpawnGateTests(unittest.TestCase):
    """P0-2 回归：并发首连只允许一个进程启动后台。"""

    def test_only_one_owner_and_released_after_close(self):
        name = 'Global\\LocalKnowledgeMcp.Test.Spawn.' + str(time.time_ns())
        with patch.object(SpawnGate, 'NAME', name):
            first = SpawnGate()
            try:
                self.assertTrue(first.owned)
                second = SpawnGate()
                try:
                    self.assertFalse(second.owned)
                finally:
                    second.close()
            finally:
                first.close()
            third = SpawnGate()
            try:
                self.assertTrue(third.owned)
            finally:
                third.close()


class BridgeSpawnTests(unittest.TestCase):
    """P0-2 回归：231（实例忙）不得触发重复启动后台。"""

    REQUEST = b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'

    def bridge(self, first_error, gate_owned):
        source = Mock()
        lines = iter([self.REQUEST, b''])
        source.readline.side_effect = lambda limit: next(lines, b'')
        target = Mock()
        channel = Mock()
        channel.receive.return_value = dict(ok=True, pid=4242)
        channel.read_line.return_value = b''
        attempts = []

        def connect(timeout=1):
            attempts.append(timeout)
            if len(attempts) == 1:
                raise pywintypes.error(first_error, 'CreateFile', 'probe')
            return channel

        gate = Mock()
        gate.owned = gate_owned
        spawns = []

        def spawn(root=None):
            spawns.append(root)
            return Mock()

        with ExitStack() as stack:
            stack.enter_context(patch('runtime.standard_stream',
                                      side_effect=lambda kind: source if kind == 'in' else target))
            stack.enter_context(patch('runtime.connect_pipe', side_effect=connect))
            stack.enter_context(patch('runtime.SpawnGate', return_value=gate))
            stack.enter_context(patch('runtime.spawn_background', side_effect=spawn))
            stack.enter_context(patch('runtime.normalized_root', side_effect=lambda value: value))
            stack.enter_context(patch('runtime.threading.Thread', return_value=Mock()))
            try:
                run_bridge('root')
            except StoreError:
                pass  # 转发线程尚未置位 input_closed 时桥接按设计报错，与 spawn 判定无关。
            time.sleep(0.2)
        return spawns, channel

    def test_busy_instances_do_not_spawn_background(self):
        spawns, _ = self.bridge(231, True)
        self.assertEqual(spawns, [])

    def test_missing_pipe_spawns_only_for_gate_owner(self):
        spawns, _ = self.bridge(2, True)
        self.assertEqual(spawns, ['root'])

    def test_missing_pipe_skips_spawn_without_gate(self):
        spawns, _ = self.bridge(2, False)
        self.assertEqual(spawns, [])


class CloseTests(unittest.TestCase):
    """P3 回归：web 关闭失败不跳过清理，worker 并发等待不放大成 5s×N。"""

    def state(self):
        store = Mock()
        store.root = Path(__file__).resolve().parents[1]
        store.guard = threading.RLock()
        return ServiceState(store)

    def test_web_failure_does_not_skip_store_close(self):
        state = self.state()
        state.web = Mock()
        state.web.close.side_effect = RuntimeError('web down')
        with patch('runtime.connect_pipe', return_value=Mock()):
            state.close()
        state.store.close.assert_called_once()

    def test_stuck_workers_share_one_deadline(self):
        state = self.state()
        workers = [Mock() for _ in range(3)]
        for index, worker in enumerate(workers):
            worker.is_alive.return_value = True
            worker.join.side_effect = lambda timeout, i=index: time.sleep(min(timeout, 0.05))
        state.workers = set(workers)
        with patch('runtime.connect_pipe', return_value=Mock()):
            start = time.monotonic()
            state.close()
            elapsed = time.monotonic() - start
        state.store.close.assert_called_once()
        self.assertLess(elapsed, 8.0, f'close() 耗时 {elapsed:.1f}s，疑似串行 5s×N')


if __name__ == '__main__':
    unittest.main()
