const path = require('node:path');

const DENYLIST = new Set(['curl', 'wget', 'gh', 'az', 'mail', 'sendmail', 'osascript', 'git']);
const GIT_COMMIT_SUBCOMMANDS = new Set(['push', 'commit', 'tag']);

function bashCommandIsCommit(command) {
  const none = { isCommit: false, matched: '' };
  if (!command || !command.trim()) return none;
  let parts = command.trim().split(/\s+/);
  let head = parts[0];
  while (head.includes('=') && !head.startsWith('/') && !head.startsWith('.')) {
    if (parts.length < 2) return none;
    parts = parts.slice(1);
    head = parts[0];
  }
  const base = path.basename(head);
  if (!DENYLIST.has(base)) return none;
  if (base === 'git') {
    const sub = parts[1] || '';
    if (!GIT_COMMIT_SUBCOMMANDS.has(sub)) return none;
    return { isCommit: true, matched: `git ${sub}` };
  }
  return { isCommit: true, matched: base };
}

module.exports = { bashCommandIsCommit, DENYLIST };
