"""Offline documentation inventory check; never imports host/collector modules.

This checks name coverage, not parameter semantics, defaults or platform behavior.
Those still require review and contract tests.
"""
import ast
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
ENVIRONMENT = re.compile(r'\bSERIALD_[A-Z_]+\b')
GUIDES = ('CONFIGURATION', 'INSTALL', 'WINDOWS', 'LOG_OPERATIONS',
          'TRANSFER_ARTIFACTS', 'HARDWARE_EXTENSIONS', 'AGENT_ACCESS')


def inventory(root):
    root = Path(root)
    environment, routes = set(), {'/healthz'}
    sources = list((root / 'seriald_core').glob('*.py'))
    sources += [root / 'seriald.py', root / 'seriald_windows.py']
    for source in sources:
        tree = ast.parse(source.read_text(encoding='utf-8'), filename=str(source))
        prefixes = {}
        if source.name == 'actions.py':
            for branch in ast.walk(tree):
                test = branch.test if isinstance(branch, ast.If) else None
                if (isinstance(test, ast.Call) and isinstance(test.func, ast.Attribute)
                        and isinstance(test.func.value, ast.Name) and test.func.value.id == 'action'
                        and test.func.attr == 'startswith' and test.args
                        and isinstance(test.args[0], ast.Constant)
                        and isinstance(test.args[0].value, str)):
                    for statement in branch.body:
                        for child in ast.walk(statement):
                            prefixes[id(child)] = '/api/' + test.args[0].value
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                environment.update(ENVIRONMENT.findall(node.value))
                if node.value.startswith('/api/') and not node.value.endswith(('/', '-')):
                    routes.add(node.value)
            # Device actions are dispatched after stripping the /api/ prefix.
            if source.name == 'actions.py' and isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == 'action':
                for comparison in node.comparators:
                    names = comparison.elts if isinstance(comparison, (ast.Tuple, ast.List)) else [comparison]
                    for name in names:
                        if isinstance(name, ast.Constant) and isinstance(name.value, str):
                            routes.add(prefixes.get(id(node), '/api/') + name.value)
    return environment, routes


def check(root=ROOT):
    root = Path(root)
    environment, routes = inventory(root)
    api = (root / 'docs/API.md').read_text(encoding='utf-8')
    configuration = '\n'.join((root / ('docs/' + guide + '.md')).read_text(encoding='utf-8')
                              for guide in GUIDES)
    documented_environment = set(ENVIRONMENT.findall(configuration))
    documented_routes = set(re.findall(r'/(?:api/[a-zA-Z0-9_/-]+|healthz)\b', api))
    missing = ['Undocumented environment variable: ' + name
               for name in sorted(environment - documented_environment)]
    missing += ['Undocumented API route: ' + route for route in sorted(routes - documented_routes)]
    return environment, routes, missing


if __name__ == '__main__':
    environment, routes, missing = check()
    for failure in missing:
        print(failure)
    if missing:
        raise SystemExit(1)
    print('Documented inventory: %d host variables, %d API routes; semantic review remains separate.'
          % (len(environment), len(routes)))
