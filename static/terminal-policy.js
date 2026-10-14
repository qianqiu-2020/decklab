(function (global) {
  'use strict';
  function install(ctx, options) {
    function suppressReply() {
      // The collector owns protocol replies for every shared live session.
      // Browsers retain keyboard/mouse input, never answer historical queries.
      return true;
    }
    function guardCsi(id, queries) {
      ctx.term.parser.registerCsiHandler(id, function (params) {
        return suppressReply() && (!queries || queries.indexOf(params[0]) !== -1);
      });
    }
    guardCsi({ final: 'n' }, [5, 6]);
    guardCsi({ prefix: '?', final: 'n' }, [6]);
    ['H', 'f'].forEach(function (final) {
      ctx.term.parser.registerCsiHandler({final: final}, function (params) {
        // systemd probes size by jumping to INT16_MAX-1 before requesting CPR.
        // With replies suppressed it cannot restore the original cursor, so
        // suppress that probe move too. Ordinary menu/application CUP remains.
        return !ctx.screenManaged && suppressReply() && params.length === 2
          && params[0] === 32766 && params[1] === 32766;
      });
    });
    ['', '>', '='].forEach(function (prefix) { guardCsi({ prefix: prefix, final: 'c' }); });
    ['', '?'].forEach(function (prefix) {
      guardCsi({prefix: prefix, intermediates: '$', final: 'p'});
    });
    guardCsi({ final: 't' }, [14, 16, 18, 20, 21]);
    [4, 10, 11, 12].forEach(function (id) {
      ctx.term.parser.registerOscHandler(id, function (data) {
        return suppressReply() && data.indexOf('?') !== -1;
      });
    });
    ['$', '+'].forEach(function (intermediates) {
      ctx.term.parser.registerDcsHandler({ intermediates: intermediates, final: 'q' }, suppressReply);
    });
    ctx.term.onData(function (data) {
      if (!options.isCurrent(ctx) || !options.keyboardEnabled()) return;
      options.send(data, ctx);
    });
    ctx.term.onSelectionChange(function () {
      if (!options.isCurrent(ctx)) return;
      var selection = ctx.term.getSelection();
      if (!selection) { ctx.lastSel = ''; clearTimeout(ctx.selTimer); return; }
      if (selection === ctx.lastSel) return;
      clearTimeout(ctx.selTimer);
      ctx.selTimer = setTimeout(function () {
        if (!options.isCurrent(ctx)) return;
        ctx.lastSel = selection;
        options.copyText(selection, true);
      }, 200);
    });
    ctx.term.attachCustomKeyEventHandler(function (event) {
      if (!options.isCurrent(ctx)) return false;
      if (event.type !== 'keydown') return true;
      if (event.ctrlKey && event.code === 'KeyV') return false;
      if (event.ctrlKey && event.shiftKey && event.code === 'KeyC') {
        options.copySelection(ctx);
        return false;
      }
      return true;
    });
  }
  global.SerialdTerminalPolicy = { install: install };
})(window);
