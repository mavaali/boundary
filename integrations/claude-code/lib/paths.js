const path = require('node:path');

function normalizeRel(p) {
  const orig = String(p).replace(/\\/g, '/');
  // Reject if originally absolute
  if (orig.startsWith('/')) return null;
  const s = orig;
  if (!s) return null;
  const norm = path.posix.normalize(s);
  // Reject if normalized to . or .. or escapes root via ..
  if (norm === '.' || norm === '..' || norm.startsWith('../') || norm.startsWith('/')) return null;
  return norm;
}

// Translate one glob segment (with * ? and [..]) to an anchored RegExp, matching
// within a single path segment (no '/').
function segToRegExp(seg) {
  let re = '';
  for (const ch of seg) {
    if (ch === '*') re += '[^/]*';
    else if (ch === '?') re += '[^/]';
    else re += ch.replace(/[.+^${}()|[\]\\]/g, '\\$&');
  }
  return new RegExp('^' + re + '$');
}

function matchSegments(pat, parts) {
  if (pat.length === 0) return parts.length === 0;
  const [head, ...rest] = pat;
  if (head === '**') {
    if (matchSegments(rest, parts)) return true;
    return parts.length > 0 && matchSegments(pat, parts.slice(1));
  }
  if (parts.length === 0) return false;
  if (segToRegExp(head).test(parts[0])) return matchSegments(rest, parts.slice(1));
  return false;
}

function anchoredGlobMatch(pattern, p) {
  const pat = pattern.replace(/\\/g, '/').replace(/^\/+/, '').split('/').filter(Boolean);
  const parts = p.split('/').filter(Boolean);
  return matchSegments(pat, parts);
}

function pathAllowed(writablePaths, candidate) {
  if (!writablePaths || writablePaths.length === 0) return false;
  const norm = normalizeRel(candidate);
  if (norm === null) return false;
  return writablePaths.some((pat) => anchoredGlobMatch(pat, norm));
}

module.exports = { normalizeRel, anchoredGlobMatch, pathAllowed };
