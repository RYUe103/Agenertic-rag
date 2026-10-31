from __future__ import annotations

import shutil

import pytest

from app.sandbox import SandboxRequest, SandboxStore


@pytest.mark.skipif(shutil.which('docker') is None, reason='Docker is required')
def test_python_sandbox_isolated_and_records_output(tmp_path):
    store = SandboxStore(tmp_path / 'sandbox.db')
    store.set_policy(store.policy().model_copy(update={'enabled': True}))
    result = store.run('local:alice', SandboxRequest(name='math', language='python', code='print(2 + 3)'), is_admin=True)
    assert result['status'] == 'success'
    assert result['stdout'].strip() == '5'
    assert store.list('local:alice')[0]['id'] == result['id']
    with pytest.raises(KeyError):
        store.get(result['id'], 'local:bob')


def test_sandbox_disabled_by_default(tmp_path):
    store = SandboxStore(tmp_path / 'sandbox.db')
    with pytest.raises(PermissionError):
        store.run('local:alice', SandboxRequest(name='blocked', language='python', code='print(1)'))


def test_running_sandbox_is_marked_unknown_after_restart(tmp_path):
    database = tmp_path / 'sandbox.db'
    store = SandboxStore(database)
    with store.connect() as db:
        db.execute("INSERT INTO runs(id,owner,name,language,code,status,created_at) VALUES(?,?,?,?,?,?,datetime('now'))",
                   ('stale', 'local:alice', 'stale', 'python', 'while True: pass', 'running'))
    recovered = SandboxStore(database)
    row = recovered.get('stale', 'local:alice')
    assert row['status'] == 'unknown'
    assert '服务重启' in row['stderr']


def test_enabling_sandbox_does_not_grant_execution_to_users(tmp_path):
    store = SandboxStore(tmp_path / 'sandbox.db')
    store.set_policy(store.policy().model_copy(update={'enabled': True}))
    with pytest.raises(PermissionError):
        store.run('ldap:alice', SandboxRequest(name='blocked', language='python', code='print(1)'))


@pytest.mark.skipif(shutil.which('docker') is None, reason='Docker is required')
def test_knowledge_sdk_reads_only_selected_snapshots(tmp_path):
    store = SandboxStore(tmp_path / 'sandbox.db')
    store.set_policy(store.policy().model_copy(update={'enabled': True, 'allow_documents': True}))
    code = "import knowledge_sdk as k\nprint(k.list_documents()[0]['filename'])\nprint(k.read_document('src_one'))\n"
    request = SandboxRequest(name='read', language='python', code=code, source_ids=['src_one'])
    result = store.run('local:admin', request, is_admin=True, documents=[{'source_id': 'src_one', 'filename': 'one.txt', 'markdown': 'private text'}])
    assert result['status'] == 'success'
    assert result['stdout'].splitlines() == ['one.txt', 'private text']
    store.set_policy(store.policy().model_copy(update={'allow_documents': False}))
    with pytest.raises(PermissionError):
        store.run('local:admin', request, is_admin=True)


@pytest.mark.skipif(shutil.which('docker') is None, reason='Docker is required')
def test_artifacts_are_saved_without_following_symlinks(tmp_path):
    store = SandboxStore(tmp_path / 'sandbox.db')
    store.set_policy(store.policy().model_copy(update={'enabled': True}))
    code = "from pathlib import Path\nPath('/tmp/output/report.txt').write_text('hello')\nPath('/tmp/output/link').symlink_to('/etc/passwd')\n"
    result = store.run('local:alice', SandboxRequest(name='report', language='python', code=code), is_admin=True)
    assert result['status'] == 'success'
    assert [item['name'] for item in result['artifacts']] == ['report.txt']
    item = result['artifacts'][0]
    assert store.artifact(result['id'], item['id'], 'local:alice') == ('report.txt', b'hello')
    with pytest.raises(KeyError):
        store.artifact(result['id'], item['id'], 'local:bob')


@pytest.mark.skipif(shutil.which('docker') is None, reason='Docker is required')
def test_container_enforces_network_readonly_and_timeout(tmp_path):
    store = SandboxStore(tmp_path / 'sandbox.db')
    store.set_policy(store.policy().model_copy(update={'enabled': True, 'timeout_seconds': 3}))
    script = '''import socket
try:
    open('/root-check', 'w')
    raise AssertionError('root filesystem writable')
except OSError:
    pass
try:
    socket.create_connection(('1.1.1.1', 80), timeout=1)
    raise AssertionError('network available')
except OSError:
    pass
print('isolated')
'''
    result = store.run('local:admin', SandboxRequest(name='isolation', language='python', code=script), is_admin=True)
    assert result['status'] == 'success'
    assert result['stdout'].strip() == 'isolated'
    result = store.run('local:admin', SandboxRequest(name='timeout', language='python', code='while True: pass'), is_admin=True)
    assert result['status'] == 'timeout'
