const path = require('node:path');

function workerCommand(packaged, resources, project) {
  return packaged
    ? { file: path.join(resources, 'worker', 'onq-worker.exe'), args: [], cwd: path.join(resources, 'worker') }
    : { file: path.join(project, '.venv', 'Scripts', 'python.exe'), args: [path.join(project, 'launch.py')], cwd: project };
}

function allowedNavigation(url) {
  try {
    return new URL(url).origin === 'http://127.0.0.1:8765';
  } catch {
    return false;
  }
}

function newScheduledCompletion(completion, lastId, started) {
  return Boolean(completion?.id && completion.id !== lastId && Date.parse(completion.finished) >= started);
}

module.exports = { workerCommand, allowedNavigation, newScheduledCompletion };
