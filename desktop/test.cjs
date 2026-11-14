const {test}=require('node:test');const assert=require('node:assert/strict');const {workerCommand,allowedNavigation}=require('./runtime.cjs');
test('packaged worker uses bundled exe rather than requiring Python',()=>{const c=workerCommand(true,'C:/App/resources','C:/source');assert.match(c.file,/onq-worker.exe$/);assert.deepEqual(c.args,[]);});
test('development worker reuses existing service',()=>{const c=workerCommand(false,'','C:/project');assert.match(c.file,/python.exe$/);assert.match(c.args[0],/launch.py$/);});
test('external pages and deceptive localhost URLs cannot navigate desktop',()=>{assert.equal(allowedNavigation('http://127.0.0.1:8765/api/state'),true);for(const u of ['https://evil.test','http://127.0.0.1:8765.evil.test','file:///C:/private','http://localhost:9999'])assert.equal(allowedNavigation(u),false);});


test('only a fresh scheduled completion opens Activity once', () => {
  const { newScheduledCompletion } = require('./runtime.cjs');
  const started = Date.parse('2026-11-13T23:59:00Z');
  const fresh = { id: 'second', finished: '2026-11-14T00:00:00Z' };
  assert.equal(newScheduledCompletion(null, null, started), false);
  assert.equal(newScheduledCompletion({ id: 'first', finished: '2026-11-13T23:58:00Z' }, null, started), false);
  assert.equal(newScheduledCompletion(fresh, null, started), true);
  assert.equal(newScheduledCompletion(fresh, fresh.id, started), false);
});
