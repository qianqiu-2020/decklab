// Map apt's reserved bottom row to the viewer, without changing captured bytes
// or rewriting ordinary firmware/shell cursor movements. State belongs to a view.
(function (global) {
  'use strict';
  function create(options) {
    var pending = '', candidate = [], sourceRows = 0, targetRows = 0, opaque = false, opaqueEscape = false;
    function rewrite(raw) {
      pending += raw;
      var output = '';
      if (sourceRows && !opaque && targetRows !== options.rows()) {
        targetRows = options.rows();
        output += '\x1b7\x1b[0;' + (targetRows - 1) + 'r\x1b8';
      }
      function flush() { output += candidate.join(''); candidate = []; }
      function control(token) {
        if (token === '\x1b7') { flush(); candidate = [token]; return; }
        if (candidate.length === 1 && /^\x1b\[0;[0-9]{1,3}r$/.test(token)) {
          candidate.push(token); return;
        }
        if (candidate.length === 2 && token === '\x1b8') { candidate.push(token); return; }
        if (candidate.length === 3 && token === '\x1b[1A') {
          var bottom = Number(candidate[1].slice(4, -1));
          if (bottom >= 2 && bottom <= 199) {
            var ending = sourceRows && bottom === sourceRows;
            if (!ending) {
              sourceRows = bottom + 1;
              if (options.onStart) options.onStart();
            }
            targetRows = options.rows();
            output += '\x1b7\x1b[0;' + (options.rows() - (ending ? 0 : 1)) + 'r\x1b8\x1b[1A';
            if (ending) sourceRows = 0;
            candidate = []; return;
          }
        }
        flush();
        var position = /^\x1b\[([0-9]{1,3});0f$/.exec(token);
        if (sourceRows && position && Number(position[1]) === sourceRows) {
          output += '\x1b[' + options.rows() + ';0f';
        } else {
          output += token;
          if (token === '\x1b[r' || token === '\x1bc') sourceRows = 0;
        }
      }
      while (pending) {
        // Never interpret cursor-looking text inside OSC/DCS control strings.
        if (opaque) {
          var ch = pending[0]; pending = pending.slice(1); output += ch;
          if (ch === '\x07' || (opaqueEscape && ch === '\\')) opaque = false;
          opaqueEscape = ch === '\x1b';
          continue;
        }
        var escape = pending.indexOf('\x1b');
        if (escape !== 0) {
          flush();
          var end = escape < 0 ? pending.length : escape;
          output += pending.slice(0, end); pending = pending.slice(end);
          continue;
        }
        if (pending.length === 1) break;
        if (']P^_X'.indexOf(pending[1]) >= 0) {
          flush(); output += pending.slice(0, 2); pending = pending.slice(2);
          opaque = true; opaqueEscape = false; continue;
        }
        var match = /^\x1b\[[0-?]*[ -/]*[@-~]/.exec(pending);
        if (pending[1] === '[' && !match) {
          if (/^\x1b\[[0-?]*[ -/]*$/.test(pending) && pending.length < 128) break;
          flush(); output += pending[0]; pending = pending.slice(1); continue;
        }
        var token = match ? match[0] : pending.slice(0, 2);
        pending = pending.slice(token.length); control(token);
      }
      return output;
    }
    return { rewrite: rewrite };
  }
  global.SerialdSerialProgress = { create: create };
})(window);
