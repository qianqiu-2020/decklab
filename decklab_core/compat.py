"""Read identifiers from installations that predate the DeckLab rename."""
import os


GRANT_PREFIXES = ('decklab-ag-', 'seriald-ag-')
BEARER_GRANT_PREFIXES = tuple('Bearer ' + prefix for prefix in GRANT_PREFIXES)
LEGACY_STATIC_ROUTES = {
    '/serialctl.py': '/decklabctl.py',
    '/seriald_mcp.py': '/decklab_mcp.py',
    '/skills/seriald-board/SKILL.md': '/skills/decklab-board/SKILL.md',
}
NODE_HEADERS = ('Actor', 'Connection', 'Grant-Generation', 'Desktop-Origin')


def adopt_environment():
    for key, value in list(os.environ.items()):
        if key.startswith('SERIALD_'):
            os.environ.setdefault('DECKLAB_' + key[len('SERIALD_'):], value)


class LegacyRequest:
    def parse_request(self):
        if not super().parse_request():
            return False
        if self.command in ('GET', 'HEAD'):
            route, separator, query = self.path.partition('?')
            self.path = LEGACY_STATIC_ROUTES.get(route, route) + separator + query
        for suffix in NODE_HEADERS:
            current = 'X-DeckLab-' + suffix
            previous = self.headers.get('X-Seriald-' + suffix)
            if previous is not None and current not in self.headers:
                self.headers[current] = previous
        return True
