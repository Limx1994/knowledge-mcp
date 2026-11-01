from contextlib import ExitStack
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock, patch

from runtime import ServiceState


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
            with self.assertLogs('knowledge.runtime', level='ERROR'):
                state.serve()
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


if __name__ == '__main__':
    unittest.main()
