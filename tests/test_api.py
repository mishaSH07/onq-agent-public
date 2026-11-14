from conftest import add_courses


def test_desktop_protocol_and_local_request_boundary(monkeypatch,tmp_path):
    monkeypatch.setenv('ONQ_STATE',str(tmp_path/'state'))
    monkeypatch.setattr('onq.store.default_documents',lambda:tmp_path/'documents')
    from fastapi.testclient import TestClient
    from onq.app import app, store
    with TestClient(app) as client:
        assert client.get('/api/health').json()=={'application':'onq-library','protocol':2,'version':'0.4.8'}
        assert client.patch('/api/settings',json={'paused':True}).status_code==403
        assert client.patch('/api/settings',headers={'X-OnQ-Client':'dashboard','Origin':'https://external.test'},json={'paused':True}).status_code==403
        assert client.patch('/api/settings',headers={'X-OnQ-Client':'dashboard'},json={'paused':True}).status_code==200
        assert client.get('/api/state').json()['settings']['paused'] is True
        reply=client.post('/api/run',headers={'X-OnQ-Client':'dashboard'},json={'mode':'publish'})
        assert reply.status_code==409 and 'disabled' in reply.json()['detail']

        state=client.get('/api/state').json()
        assert state['courses'] == []
        add_courses(store)
        course = store.courses()[0]
        response=client.post('/api/courses/'+course['id']+'/harness/queue',headers={'X-OnQ-Client':'dashboard'})
        assert response.status_code==409 and 'build bundles' in response.json()['detail']

        from unittest.mock import AsyncMock
        from onq.app import service
        from onq.browser import SignInNeeded

        headers={'X-OnQ-Client':'dashboard'}
        route='/api/courses/'+course['id']+'/chatgpt'
        sign_in=AsyncMock()
        probe=AsyncMock(return_value=['original.pdf'])
        monkeypatch.setattr(service.browsers,'login',sign_in)
        monkeypatch.setattr(service.publisher,'inspect_sources',probe)
        assert client.post(route+'/login').status_code==403
        assert client.post(route+'/login',headers=headers).json()=={'opened':True}
        sign_in.assert_awaited_once_with('chatgpt',course['project']+'?tab=sources')
        assert client.post(route+'/check',headers=headers).json()=={'sources_open':True,'visible_files':['original.pdf']}
        probe.assert_awaited_once_with(course['project'])
        assert not client.get('/api/state').json()['settings']['cloud_verified']
        probe.side_effect=SignInNeeded('Sign in to ChatGPT')
        assert client.post(route+'/check',headers=headers).status_code==409

        auto=route+'/automatic'
        assert client.patch(auto,headers=headers,json={'enabled':True}).status_code==409
        assert client.patch(auto,headers=headers,json={'enabled':'false'}).status_code==400
        assert client.patch(auto,headers=headers,json={'enabled':False}).status_code==200
        fresh=client.get('/api/state').json()
        c=next(c for c in fresh['courses'] if c['id']==course['id'])
        assert not c['publishing']['automatic']
        assert not c['publishing']['eligible']
