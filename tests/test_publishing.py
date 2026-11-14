from onq.publisher import browser_verification_title

def test_recognizes_observed_browser_verification_without_claiming_project_access():
    assert browser_verification_title('Just a moment...')
    assert browser_verification_title('Verify you are human')
    assert not browser_verification_title('ChatGPT - Example Course 1')
    assert not browser_verification_title('ChatGPT')


def test_storage_probe_reports_browser_verification_after_waiting_for_storage():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    import pytest
    from playwright.async_api import TimeoutError
    from onq.browser import ActionNeeded
    from onq.publisher import Publisher

    page = Mock(url="https://chatgpt.com/settings/storage", goto=AsyncMock(), close=AsyncMock(),
                title=AsyncMock(side_effect=['ChatGPT', 'Just a moment...']))
    page.get_by_role.return_value = Mock(
        count=AsyncMock(return_value=0), wait_for=AsyncMock(side_effect=TimeoutError('Storage missing'))
    )
    context = SimpleNamespace(new_page=AsyncMock(return_value=page))
    publisher = Publisher(None, SimpleNamespace(context=AsyncMock(return_value=context)))

    with pytest.raises(ActionNeeded, match='Complete browser verification'):
        asyncio.run(publisher.source_limits(40 * 1024**2))
    page.close.assert_not_awaited()


def test_inline_sources_probe_opens_exact_project_without_clicking():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from onq.publisher import Publisher

    project = 'https://chatgpt.com/g/g-p-example/project'
    sources = project + '?tab=sources'
    marker = Mock(count=AsyncMock(return_value=1), wait_for=AsyncMock())
    page = Mock(url=sources, goto=AsyncMock(), title=AsyncMock(return_value='ChatGPT'))
    page.get_by_role.side_effect = lambda role, name, exact: (
        marker if name == 'Add sources' else Mock(count=AsyncMock(return_value=0))
    )
    browsers = SimpleNamespace(context=AsyncMock(return_value=SimpleNamespace(pages=[page])))
    publisher = Publisher(None, browsers)
    assert asyncio.run(publisher.open_sources(project)) is page
    page.goto.assert_awaited_once_with(sources, wait_until='domcontentloaded', timeout=30000)
    marker.click.assert_not_called()


def test_inline_sources_probe_rejects_different_project():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    import pytest
    from onq.browser import ActionNeeded
    from onq.publisher import Publisher

    page = Mock(url='https://chatgpt.com/g/g-p-other/project?tab=sources',
                goto=AsyncMock(), title=AsyncMock(return_value='ChatGPT'))
    page.get_by_role.return_value.count = AsyncMock(return_value=0)
    browsers = SimpleNamespace(context=AsyncMock(return_value=SimpleNamespace(pages=[page])))
    with pytest.raises(ActionNeeded, match='destination'):
        asyncio.run(Publisher(None, browsers).open_sources('https://chatgpt.com/g/g-p-example/project'))

def test_replacement_preflight_reserves_capacity_and_only_removes_verified_sources():
    import pytest
    from onq.browser import ActionNeeded
    from onq.publisher import plan_replacement

    remote = [{'id':'user-1','name':'original.pdf'},
              {'id':'managed-1','name':'old.pdf'},
              {'id':'managed-2','name':'same.md'}]
    owned = {'managed-1': {'name':'old.pdf','hash':'old'},
             'managed-2': {'name':'same.md','hash':'same'}}
    files = [{'upload_name':'new.pdf','hash':'new','size':20},
             {'upload_name':'same.md','hash':'same','size':5}]
    limits = {'max_files':3,'max_file_bytes':25,'available_bytes':20}
    assert plan_replacement(remote, owned, files, limits) == {
        'remove':[remote[1]], 'upload':[files[0]], 'preserve':[remote[0],remote[2]]
    }
    with pytest.raises(ActionNeeded, match='storage'):
        plan_replacement(remote, owned, files, {**limits,'available_bytes':19})
    with pytest.raises(ActionNeeded, match='capacity'):
        plan_replacement(remote, owned, files, {**limits,'available_bytes':None})
    with pytest.raises(ActionNeeded, match='file size'):
        plan_replacement(remote, owned, files, {**limits,'max_file_bytes':19})
    with pytest.raises(ActionNeeded, match='file count'):
        plan_replacement(remote, {'managed-2':owned['managed-2']}, files, limits)


def test_replacement_preflight_rejects_unowned_name_and_changed_identity():
    import pytest
    from onq.browser import ActionNeeded
    from onq.publisher import plan_replacement

    remote = [{'id':'user-1','name':'next.pdf'}]
    files = [{'upload_name':'next.pdf','hash':'next','size':1}]
    limits = {'max_files':4,'max_file_bytes':10,'available_bytes':10}
    with pytest.raises(ActionNeeded, match='ownership'):
        plan_replacement(remote, {}, files, limits)
    with pytest.raises(ActionNeeded, match='ownership'):
        plan_replacement(remote, {'user-1':{'name':'old.pdf','hash':'old'}}, files, limits)

def test_sources_inventory_reports_observed_names_and_rejects_duplicates():
    import asyncio
    from unittest.mock import AsyncMock, Mock
    import pytest
    from onq.browser import ActionNeeded
    from onq.publisher import Publisher

    project = 'https://chatgpt.com/g/g-p-example/project'
    actions = Mock(evaluate_all=AsyncMock(return_value=['Actions for original.pdf','Actions for pilot.txt']))
    page = Mock(url=project+'?tab=sources')
    page.get_by_role.return_value.filter.return_value.get_by_role.return_value = actions
    publisher = Publisher(None, None)
    publisher.open_sources = AsyncMock(return_value=page)
    assert asyncio.run(publisher.inspect_sources(project)) == ['original.pdf','pilot.txt']
    actions.evaluate_all.return_value=['Actions for pilot.txt','Actions for pilot.txt']
    with pytest.raises(ActionNeeded, match='ambiguous'):
        asyncio.run(publisher.inspect_sources(project))

def test_inline_source_verification_hashes_and_disposes_download(tmp_path):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    import pytest
    from onq.browser import ActionNeeded
    from onq.publisher import Publisher
    from onq.store import digest

    project = 'https://chatgpt.com/g/g-p-example/project'
    name = 'pilot.txt'
    payload = b'pilot version two'
    path = tmp_path/name
    path.write_bytes(payload)
    download = MagicMock(suggested_filename=name, path=AsyncMock(return_value=path), delete=AsyncMock())
    event = MagicMock()
    async def downloaded():
        return download
    type(event).value = property(lambda self: downloaded())
    chooser = MagicMock()
    chooser.__aenter__.return_value = event
    page = MagicMock(url=project+'?tab=sources', expect_download=MagicMock(return_value=chooser))
    menu = MagicMock(count=AsyncMock(return_value=1))
    control = MagicMock(count=AsyncMock(return_value=1),click=AsyncMock(),wait_for=AsyncMock())
    menu.get_by_role.return_value = control
    page.get_by_role.return_value = menu
    publisher = Publisher(None, None)
    publisher.open_sources = AsyncMock(return_value=page)
    publisher.source_action = AsyncMock(return_value=control)
    assert asyncio.run(publisher.verify_source(project,name,digest(payload),len(payload))) is page
    download.delete.assert_awaited_once()
    publisher.source_action.assert_awaited_once_with(page, name)

    path.write_bytes(b'wrong version!!')
    with pytest.raises(ActionNeeded,match='does not match'):
        asyncio.run(publisher.verify_source(project,name,digest(payload),len(payload)))
    assert download.delete.await_count == 2

def test_inline_upload_refuses_existing_name_and_verifies_readback(tmp_path):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    import pytest
    from onq.browser import ActionNeeded
    from onq.publisher import Publisher, SourceReadbackTimeout
    from onq.store import digest

    project = 'https://chatgpt.com/g/g-p-example/project'
    path = tmp_path/'pilot.txt'
    data = b'pilot'
    path.write_bytes(data)
    page = MagicMock(url=project+'?tab=sources')
    locator = page.get_by_role.return_value
    locator.count = AsyncMock(return_value=1)
    locator.click = AsyncMock()
    locator.wait_for = AsyncMock()
    locator.get_by_role.return_value = locator
    chooser = MagicMock(set_files=AsyncMock())
    async def chosen():
        return chooser
    event = MagicMock()
    type(event).value = property(lambda self: chosen())
    page.expect_file_chooser.return_value.__aenter__.return_value = event
    publisher = Publisher(None, None)
    publisher.open_sources = AsyncMock(return_value=page)
    publisher.source_names = AsyncMock(return_value=[path.name])
    ordering = MagicMock()
    publisher.verify_source_on_page = ordering.readback = AsyncMock()
    publisher.confirm_source_inventory = ordering.reopen = AsyncMock()
    publisher.verify_source = AsyncMock()
    limits = {'max_files':4,'max_file_bytes':100,'available_bytes':100}
    with pytest.raises(ActionNeeded,match='already exists'):
        asyncio.run(publisher.upload_source(project,path,digest(data),len(data),limits))
    locator.click.assert_not_awaited()
    publisher.source_names.return_value=['original.pdf']
    asyncio.run(publisher.upload_source(project,path,digest(data),len(data),limits))
    chooser.set_files.assert_awaited_once()
    assert [call[0] for call in ordering.mock_calls] == ['readback','reopen']
    publisher.verify_source.assert_awaited_once_with(project,path.name,digest(data),len(data))

    from playwright.async_api import TimeoutError
    publisher.require_chatgpt_access = AsyncMock()
    chooser.set_files.side_effect = TimeoutError('upload stalled')
    with pytest.raises(ActionNeeded, match='outcome is uncertain'):
        asyncio.run(publisher.upload_source(project,path,digest(data),len(data),limits))
    assert chooser.set_files.await_count == 2
    assert publisher.verify_source.await_count == 1

    chooser.set_files.side_effect = None
    publisher.verify_source_on_page.side_effect = SourceReadbackTimeout('download not ready')
    asyncio.run(publisher.upload_source(project,path,digest(data),len(data),limits))
    assert chooser.set_files.await_count == 3
    assert publisher.verify_source.await_count == 2

    publisher.verify_source_on_page.side_effect = ActionNeeded('downloaded bytes do not match')
    with pytest.raises(ActionNeeded, match='bytes do not match'):
        asyncio.run(publisher.upload_source(project,path,digest(data),len(data),limits))
    assert chooser.set_files.await_count == 4
    assert publisher.verify_source.await_count == 2

def test_storage_headroom_is_conservative_and_rejects_unknown_format():
    import pytest
    from onq.browser import ActionNeeded
    from onq.publisher import storage_headroom

    remaining = storage_headroom('986 MB of 20 GB used')
    assert 17_000_000_000 < remaining < 19_000_000_000
    assert storage_headroom('20 GB of 20 GB used') == 0
    with pytest.raises(ActionNeeded, match='storage'):
        storage_headroom('Storage is loading')


def test_expected_source_inventory_waits_for_hydration_but_stops_on_extra_file():
    import asyncio
    from unittest.mock import AsyncMock, patch
    import pytest
    from onq.browser import ActionNeeded
    from onq.publisher import Publisher

    publisher = Publisher(None, None)
    publisher.open_sources = AsyncMock()
    publisher.source_names = AsyncMock(side_effect=[['original.pdf'],['original.pdf','pilot.txt']])
    with patch('onq.publisher.asyncio.sleep', new_callable=AsyncMock) as sleep:
        asyncio.run(publisher.confirm_source_inventory('project', {'original.pdf','pilot.txt'}))
        sleep.assert_awaited_once()
    publisher.open_sources.assert_awaited_once()
    publisher.require_chatgpt_access = AsyncMock()
    publisher.source_names = AsyncMock(return_value=['unexpected.pdf'])
    with pytest.raises(ActionNeeded, match='exact expected inventory'):
        asyncio.run(publisher.confirm_source_inventory('project', {'original.pdf','pilot.txt'}))
    publisher.source_names.assert_awaited_once()
