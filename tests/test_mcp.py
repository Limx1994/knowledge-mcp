import asyncio
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import time
from contextlib import contextmanager

from mcp import Client
from mcp.client.stdio import StdioServerParameters

PROJECT = Path(__file__).resolve().parents[1]
TEMP = PROJECT / '.test-work'
TEMP.mkdir(exist_ok=True)


def stop_service(root):
    from runtime import control_request
    from server import SingleInstance
    import pywintypes
    import win32api, win32con, win32event
    processes = []
    try:
        status = control_request(root, 'status')
        for pid in {status['pid'], status.get('launcher_pid', status['pid'])}:
            processes.append(win32api.OpenProcess(win32con.SYNCHRONIZE, False, pid))
        if status.get('tray_window'):
            import win32gui, win32con
            from tray import EXIT_APP
            win32gui.PostMessage(status['tray_window'], win32con.WM_COMMAND, EXIT_APP, 0)
        else:
            control_request(root, 'shutdown')
    except pywintypes.error as exc:
        if exc.winerror != 2:
            raise
    for process in processes:
        try:
            if win32event.WaitForSingleObject(process, 15000) != win32event.WAIT_OBJECT_0:
                raise RuntimeError('后台或单文件启动器未在 15 秒内退出')
        finally:
            process.Close()
    deadline = time.monotonic() + 15
    while True:
        try:
            instance = SingleInstance()
            instance.close()
            return
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.1)


@contextmanager
def service_directory():
    with tempfile.TemporaryDirectory(dir=TEMP) as temp:
        try:
            yield temp
        except BaseException:
            log = Path(temp) / '中文 知识库' / '.data' / 'service.log'
            if log.exists():
                print(log.read_text(encoding='utf-8')[-5000:], file=sys.stderr)
            raise
        finally:
            root = Path(temp) / '中文 知识库'
            if (root / '.data' / 'service.log').exists():
                stop_service(root)


class ProtocolTests(unittest.TestCase):
    def test_concurrent_first_connections(self):
        commands = [(sys.executable, [str(PROJECT / 'server.py')])]
        if os.environ.get('KNOWLEDGE_TEST_EXE'):
            commands.append((os.environ['KNOWLEDGE_TEST_EXE'], []))
        for command, args in commands:
            with self.subTest(command=command):
                asyncio.run(self.concurrent_first_connections(command, args))

    async def concurrent_first_connections(self, command, args):
        from runtime import control_request
        with service_directory() as temp:
            root = Path(temp) / '中文 知识库'
            root.mkdir()
            params = StdioServerParameters(command=command,
                args=[*args, '--root', str(root)], cwd=TEMP)
            barrier = asyncio.Barrier(2)

            async def connect():
                await barrier.wait()
                async with Client(params, read_timeout_seconds=30) as client:
                    self.assertEqual(len((await client.list_tools()).tools), 18)
                    await barrier.wait()
                    status = await asyncio.to_thread(control_request, root, 'status')
                    self.assertEqual(status['connections'], 2)
                    await barrier.wait()
                    return status['pid']

            async with asyncio.timeout(45), asyncio.TaskGroup() as group:
                first = group.create_task(connect())
                second = group.create_task(connect())
            self.assertEqual(first.result(), second.result())

    def test_background_crash_reconnect(self):
        asyncio.run(self.crash_reconnect())

    async def crash_reconnect(self):
        from runtime import control_request
        import win32api, win32con, win32event
        with service_directory() as temp:
            root = Path(temp) / '中文 知识库'
            root.mkdir()
            params = StdioServerParameters(command=sys.executable,
                args=[str(PROJECT / 'server.py'), '--root', str(root)])
            async with Client(params, read_timeout_seconds=30) as client:
                result = await client.call_tool('create_document', {'path': 'recovery.md', 'content': '异常后恢复'})
                self.assertFalse(result.is_error)
            pid = control_request(root, 'status')['pid']
            process = win32api.OpenProcess(win32con.PROCESS_TERMINATE | win32con.SYNCHRONIZE, False, pid)
            try:
                win32api.TerminateProcess(process, 1)
                self.assertEqual(win32event.WaitForSingleObject(process, 10000), win32event.WAIT_OBJECT_0)
            finally:
                process.Close()
            async with Client(params, read_timeout_seconds=30) as client:
                result = await client.call_tool('search_documents', {'query': '异常后恢复'})
                self.assertEqual(result.structured_content['total'], 1)
                self.assertNotEqual(control_request(root, 'status')['pid'], pid)

    def test_no_request_does_not_start(self):
        commands = [[sys.executable, str(PROJECT / 'server.py')]]
        if os.environ.get('KNOWLEDGE_TEST_EXE'):
            commands.append([os.environ['KNOWLEDGE_TEST_EXE']])
        with tempfile.TemporaryDirectory(dir=TEMP) as temp:
            for command in commands:
                result = subprocess.run([*command, '--root', temp], input=b'', capture_output=True, timeout=20)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, b'')
                self.assertFalse((Path(temp) / '.data').exists())

    def test_singleton_crash_release(self):
        asyncio.run(self.crash_release())

    async def crash_release(self):
        from server import SingleInstance
        code = ('from server import SingleInstance; import sys; '
                'instance=SingleInstance(); print("ready",flush=True); sys.stdin.read()')
        proc = await asyncio.create_subprocess_exec(
            sys.executable, '-u', '-c', code, cwd=PROJECT,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        try:
            self.assertEqual(await asyncio.wait_for(proc.stdout.readline(), 15), b'ready\r\n')
            with self.assertRaisesRegex(Exception, '不能重复启动'):
                SingleInstance()
        finally:
            if proc.returncode is None:
                proc.kill()
            await asyncio.wait_for(proc.wait(), 15)
        instance = SingleInstance()
        instance.close()

    def test_stdio_source(self):
        asyncio.run(self.lifecycle(sys.executable, [str(PROJECT / 'server.py')]))

    @unittest.skipUnless(os.environ.get('KNOWLEDGE_TEST_EXE'), '设置 KNOWLEDGE_TEST_EXE 验证打包产物')
    def test_moved_exe(self):
        exe = Path(os.environ['KNOWLEDGE_TEST_EXE']).resolve()
        with tempfile.TemporaryDirectory(dir=TEMP) as temp:
            moved = Path(temp) / '带 空格的工具'
            moved.mkdir()
            target = moved / 'knowledge-mcp.exe'
            shutil.copy2(exe, target)
            asyncio.run(self.lifecycle(str(target), [], clean_env=True))
            moved_again = Path(temp) / '再次移动.exe'
            target.rename(moved_again)
            asyncio.run(self.lifecycle(str(moved_again), [], clean_env=True))

    async def lifecycle(self, command, args, clean_env=False):
        with service_directory() as temp:
            root = Path(temp) / '中文 知识库'
            root.mkdir()
            env = None
            if clean_env:
                env = {k: v for k, v in os.environ.items() if k.upper() not in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV')}
                env['PATH'] = str(Path(os.environ['SystemRoot']) / 'System32')
            params = StdioServerParameters(command=command, args=[*args, '--root', str(root)], cwd=TEMP, env=env)
            async with Client(params, read_timeout_seconds=30) as client:
                tools = await client.list_tools()
                self.assertEqual(len(tools.tools), 18)
                other_root = Path(temp) / '另一个知识库'
                other_root.mkdir()
                duplicate = command
                if clean_env:
                    copy = Path(temp) / '另一个副本.exe'
                    shutil.copy2(command, copy)
                    duplicate = str(copy)
                result = await asyncio.to_thread(
                    subprocess.run, [duplicate, *args, '--root', str(other_root)],
                    input=b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n',
                    capture_output=True, cwd=TEMP, env=env, timeout=30)
                self.assertEqual(result.returncode, 1, result)
                self.assertEqual(result.stdout, b'')
                message = result.stderr.decode('utf-8', errors='replace')
                if '其他知识库' not in message:
                    message = result.stderr.decode('gb18030', errors='replace')
                self.assertIn('其他知识库', message)
                self.assertFalse((other_root / '.data').exists())

                async def call(name, **kwargs):
                    result = await client.call_tool(name, kwargs)
                    self.assertFalse(result.is_error, result)
                    return result.structured_content

                await call('create_directory', path='工具/空目录')
                entries = await call('list_entries', directory='工具')
                await call('delete_directory', path='工具/空目录', expected_version=entries['items'][0]['version'])
                entry = await call('create_document', path='工具/a.md', content='原始内容')
                chunk = await call('read_document_chunk', path=entry['path'])
                changed = await call('patch_document', path=entry['path'], start_byte=chunk['start_byte'],
                                     end_byte=chunk['end_byte'], content='局部修改', expected_version=chunk['version'])
                await call('rename_document', path=entry['path'], new_name='B.md', expected_version=changed['version'])
                await call('move_document', path='工具/B.md', target='工具/子目录/B.md', expected_version=changed['version'])
                entries = await call('list_entries', directory='工具')
                await call('rename_directory', path='工具', new_name='工具改名', expected_version=entries['version'])
                entries = await call('list_entries', directory='工具改名')
                await call('move_directory', path='工具改名', target='工具移动', expected_version=entries['version'])
                await call('delete_document', path='工具移动/子目录/B.md', expected_version=changed['version'])
                records = await call('list_deleted')
                await call('restore_document', record_id=records['items'][0]['record_id'])
                # Remove the fixture externally so original lifecycle counts remain unchanged.
                shutil.rmtree(root / 'knowledge' / '工具移动')
                await call('refresh_index')
                item = await call('create_document', path='规范/说明.md', content='知识库 Python\n第二行')
                read = await call('read_document', path=item['path'])
                self.assertEqual(read['version'], item['version'])
                updated = await call('update_document', path=item['path'], content='更新后的知识库', expected_version=read['version'])
                found = await call('search_documents', query='知识库')
                self.assertEqual(found['total'], 1)
                self.assertEqual(found['items'][0]['snippets'][0]['line'], 1)
                conflict = await client.call_tool('update_document', dict(path=item['path'], content='stale', expected_version=read['version']))
                self.assertTrue(conflict.is_error)
                self.assertIn(updated['version'], conflict.content[0].text)
                deleted = await call('delete_document', path=item['path'], expected_version=updated['version'])
                self.assertEqual((await call('list_deleted'))['total'], 1)
                self.assertEqual((await call('search_documents', query='知识库'))['total'], 0)
                await call('restore_document', record_id=deleted['record_id'])
                self.assertEqual((await call('list_documents'))['total'], 1)
                (root / 'knowledge' / '规范' / '说明.md').write_text('手动修改生效', encoding='utf-8')
                self.assertTrue((await call('refresh_index', full=True))['ok'])
                self.assertEqual((await call('search_documents', query='手动修改'))['total'], 1)
                bad = await client.call_tool('read_document', dict(path='../server.py'))
                self.assertTrue(bad.is_error)
                await asyncio.to_thread(self.check_live_web, root)
                self.assertEqual((await call('search_documents', query='跨接口'))['total'], 1)
                async with Client(params, read_timeout_seconds=30) as second:
                    self.assertEqual(len((await second.list_tools()).tools), 18)
                    from runtime import control_request
                    self.assertEqual(control_request(root, 'status')['connections'], 2)
            from runtime import control_request
            first_pid = control_request(root, 'status')['pid']
            async with Client(params, read_timeout_seconds=30) as reconnected:
                self.assertEqual(len((await reconnected.list_tools()).tools), 18)
                self.assertEqual(control_request(root, 'status')['pid'], first_pid)
                found = await reconnected.call_tool('search_documents', {'query': '手动修改'})
                self.assertEqual(found.structured_content['total'], 1)

    def check_live_web(self, root):
        import httpx2
        from urllib.parse import urlsplit, parse_qs
        from runtime import control_request
        url = control_request(root, 'ticket')['url']
        parts = urlsplit(url)
        base = f'{parts.scheme}://{parts.netloc}'
        with httpx2.Client(base_url=base, trust_env=False) as client:
            self.assertEqual(client.get('/').status_code, 200)
            self.assertEqual(client.get('/assets/app.js').status_code, 200)
            response = client.post('/api/session', json={'ticket': parse_qs(parts.fragment)['ticket'][0]}, headers={'Origin': base})
            self.assertEqual(response.status_code, 200)
            headers = {'Origin': base, 'X-CSRF-Token': response.json()['csrf']}
            self.assertGreater(client.get('/api/status').json()['connections'], 0)
            response = client.post('/api/documents', json=dict(action='create', path='网页/互通.md', content='跨接口验证'), headers=headers)
            self.assertEqual(response.status_code, 200)


    def test_no_root_reports_setup_and_stops(self):
        with tempfile.TemporaryDirectory(dir=TEMP) as temp:
            config = Path(temp) / 'config.json'
            env = {k: v for k, v in os.environ.items()
                   if k.upper() not in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV')}
            env['KNOWLEDGE_MCP_CONFIG'] = str(config)
            result = subprocess.run([sys.executable, str(PROJECT / 'server.py')],
                                    input=b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n',
                                    capture_output=True, env=env, cwd=TEMP, timeout=30)
            self.assertEqual(result.returncode, 1, result)
            text = result.stderr.decode('utf-8', errors='replace')
            if '尚未配置' not in text:
                text = result.stderr.decode('gb18030', errors='replace')
            self.assertIn('尚未配置', text)
            from runtime import control_request
            self.assertFalse(control_request('', 'status')['configured'])
            control_request('', 'shutdown')
            deadline = time.monotonic() + 15
            import pywintypes
            from runtime import connect_pipe
            while True:
                try:
                    connect_pipe(0.2).close()
                except pywintypes.error as exc:
                    if exc.winerror == 2:
                        break
                    raise
                if time.monotonic() > deadline:
                    raise AssertionError('后台未退出')
                time.sleep(0.1)
            from server import SingleInstance
            while True:
                try:
                    instance = SingleInstance()
                    instance.close()
                    break
                except Exception:
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(0.1)
            self.assertFalse(config.exists())


if __name__ == '__main__':
    unittest.main()
