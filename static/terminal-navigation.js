// Terminal-buffer search and echoed-command navigation. No device/API state.
(function (global) {
  'use strict';
  function create(options) {
    var $ = options.byId, isCurrent = options.isCurrent;
    function terminalMatches(ctx, query) {
      var active = ctx && ctx.term && ctx.term.buffer && ctx.term.buffer.active;
      if (!active || !query) return [];
      var needle = query.toLocaleLowerCase();
      var matches = [];
      for (var row = 0; row < active.length; row++) {
        var line = active.getLine(row);
        if (!line || !line.translateToString) continue;
        var text = line.translateToString(true).toLocaleLowerCase();
        var from = 0, found;
        while ((found = text.indexOf(needle, from)) !== -1) {
          matches.push({ row: row, col: found });
          from = found + Math.max(1, needle.length);
        }
      }
      return matches;
    }

    function searchTerminal(direction) {
      var ctx = options.getView();
      if (!isCurrent(ctx)) return;
      var query = $('terminal-search').value;
      if (!query) {
        ctx.term.clearSelection(); ctx.searchQuery = ''; ctx.searchMatch = -1;
        $('terminal-search-status').textContent = ''; return;
      }
      var matches = terminalMatches(ctx, query);
      if (!matches.length) {
        ctx.term.clearSelection(); ctx.searchQuery = query; ctx.searchMatch = -1;
        $('terminal-search-status').textContent = '无匹配'; return;
      }
      if (ctx.searchQuery !== query || ctx.searchMatch < 0 || ctx.searchMatch >= matches.length) {
        ctx.searchMatch = direction < 0 ? matches.length - 1 : 0;
      } else {
        ctx.searchMatch = (ctx.searchMatch + direction + matches.length) % matches.length;
      }
      ctx.searchQuery = query;
      var match = matches[ctx.searchMatch];
      ctx.term.select(match.col, match.row, query.length);
      ctx.term.scrollToLine(match.row);
      $('terminal-search-status').textContent = (ctx.searchMatch + 1) + ' / ' + matches.length;
    }

    function clearTerminalSearch(focusTerminal) {
      $('terminal-search').value = '';
      $('terminal-search-status').textContent = '';
      var view = options.getView();
      if (view && view.term) {
        view.searchQuery = ''; view.searchMatch = -1; view.term.clearSelection();
        if (focusTerminal) view.term.focus();
      }
    }

    // Index what the shell actually echoed, never raw keystrokes that could be
    // passwords or input to full-screen programs.
    function terminalCommands(ctx) {
      var active = ctx.term && ctx.term.buffer && ctx.term.buffer.active;
      if (!active || !active.getLine) return [];
      var entries = [];
      for (var row = 0; row < active.length; row++) {
        var line = active.getLine(row);
        if (!line || !line.translateToString) continue;
        var shown = line.translateToString(true);
        var match = /^(?:\d{4}-\d\d-\d\d \d\d:\d\d:\d\d  )?([^\s@]+@[^\s]+[#$])\s+(.+)$/.exec(shown);
        if (match && match[2].trim() && !/^[^\s@]+@[^\s]+[#$](?:\s|$)/.test(match[2])) {
          entries.push({ row: row, column: shown.indexOf(match[2]),
                         prompt: match[1], command: match[2].trim() });
        }
      }
      return entries;
    }

    function clearCommandDecorations(ctx) {
      (ctx.commandDecorations || []).forEach(function (decoration) { decoration.dispose(); });
      ctx.commandDecorations = [];
      ctx.commandColorSignature = '';
    }

    function colorCommandLines(ctx) {
      if (!isCurrent(ctx) || !ctx.term.registerMarker || !ctx.term.registerDecoration) return;
      var active = ctx.term.buffer.active;
      if (active.type === 'alternate') return;
      var entries = terminalCommands(ctx).slice(-300);
      var signature = ctx.term.cols + '|' + entries.map(function (entry) {
        return entry.row + ':' + entry.column + ':' + entry.command;
      }).join('|');
      if (signature === ctx.commandColorSignature) return;
      clearCommandDecorations(ctx);
      ctx.commandColorSignature = signature;
      var commandColor = '#C6EFE4';
      entries.forEach(function (entry) {
        if (entry.column >= ctx.term.cols) return;
        var marker = ctx.term.registerMarker(entry.row - (active.baseY + active.cursorY));
        if (!marker) return;
        var background = ctx.term.registerDecoration({ marker: marker, x: 0,
          width: ctx.term.cols, backgroundColor: '#29414A' });
        var foreground = ctx.term.registerDecoration({ marker: marker, x: entry.column,
          width: Math.min(entry.command.length, ctx.term.cols - entry.column),
          foregroundColor: commandColor });
        if (background) ctx.commandDecorations.push(background);
        if (foreground) ctx.commandDecorations.push(foreground);
        ctx.commandDecorations.push(marker);
      });
    }

    function jumpToCommand(ctx, index) {
      if (!isCurrent(ctx) || index < 0 || index >= ctx.navEntries.length) return;
      ctx.navSelected = index;
      var entry = ctx.navEntries[index];
      ctx.term.scrollToLine(entry.row);
      ctx.term.select(entry.column, entry.row, Math.min(entry.command.length, ctx.term.cols - entry.column));
      Array.prototype.forEach.call($('d-command-nav').children, function (button, position) {
        button.classList.toggle('on', position === index);
      });
    }

    function renderCommandNav(ctx) {
      if (!isCurrent(ctx) || !options.navigationVisible()) return;
      var updated = terminalCommands(ctx);
      if (ctx.navRendered && updated.length === ctx.navEntries.length && updated.every(function (entry, index) {
        return entry.row === ctx.navEntries[index].row && entry.command === ctx.navEntries[index].command;
      })) return;
      var selected = ctx.navEntries[ctx.navSelected];
      ctx.navEntries = updated;
      ctx.navRendered = true;
      ctx.navSelected = selected ? ctx.navEntries.findIndex(function (entry) {
        return entry.row === selected.row && entry.command === selected.command;
      }) : -1;
      var list = $('d-command-nav'); list.textContent = '';
      $('command-nav-count').textContent = ctx.navEntries.length + ' 条';
      if (!ctx.navEntries.length) {
        list.textContent = '当前终端缓冲没有可识别的 shell 命令。固件菜单和全屏程序不按命令切分。';
        return;
      }
      ctx.navEntries.forEach(function (entry, index) {
        var button = document.createElement('button');
        button.type = 'button'; button.className = 'command-nav-item';
        button.classList.toggle('on', index === ctx.navSelected);
        var number = document.createElement('span'); number.className = 'command-nav-number';
        number.textContent = String(index + 1).padStart(2, '0');
        var body = document.createElement('span'); body.className = 'command-nav-body';
        var command = document.createElement('strong'); command.textContent = entry.command;
        var prompt = document.createElement('small'); prompt.textContent = entry.prompt;
        body.appendChild(command); body.appendChild(prompt);
        button.appendChild(number); button.appendChild(body);
        button.addEventListener('click', function () { jumpToCommand(ctx, index); });
        list.appendChild(button);
      });
    }

    function scheduleCommandNav(ctx) {
      if (!isCurrent(ctx) || ctx.navTimer) return;
      ctx.navTimer = setTimeout(function () {
        ctx.navTimer = 0;
        colorCommandLines(ctx);
        renderCommandNav(ctx);
      }, 350);
    }

    return {
      search: searchTerminal, clearSearch: clearTerminalSearch,
      clearDecorations: clearCommandDecorations, renderNavigation: renderCommandNav,
      scheduleNavigation: scheduleCommandNav, jump: jumpToCommand
    };
  }
  global.SerialdTerminalNavigation = { create: create };
})(window);
