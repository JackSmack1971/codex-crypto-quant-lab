"""Offline dependency-reproducibility check for pyproject.toml and constraints-ci.txt.

Fails when a runtime dependency is neither exact-pinned nor upper-bounded, when a declared
dependency has no exact constraint pin, or when a pin violates the pyproject range. With
--installed it also compares installed distribution versions to the pins. It does not lock
or verify transitive dependencies.
"""
import argparse
import re
import sys
import tomllib
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = re.compile(r'^\s*([A-Za-z0-9_.-]+)\s*(.*)$')


def norm(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def parse_constraints(text):
    pins, errors = {}, []
    for line in text.splitlines():
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        match = re.fullmatch(r'([A-Za-z0-9_.-]+)==([0-9][A-Za-z0-9.+!-]*)', line)
        if not match:
            errors.append(f'constraints-ci.txt: not an exact pin: {line}')
        else:
            pins[norm(match.group(1))] = match.group(2)
    return pins, errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--installed', action='store_true', help='compare installed versions with the pins')
    args = parser.parse_args(argv)
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))['project']
    specs = list(project.get('dependencies', []))
    for group in project.get('optional-dependencies', {}).values():
        specs.extend(group)
    pins, errors = parse_constraints((ROOT / 'constraints-ci.txt').read_text(encoding='utf-8'))
    try:
        from packaging.specifiers import SpecifierSet
    except ImportError:
        SpecifierSet = None
    for spec in sorted(set(specs)):
        name, rest = NAME.match(spec).groups()
        if '==' not in rest and '<' not in rest:
            errors.append(f'pyproject: {name} has no exact pin or upper bound: {spec}')
        pin = pins.get(norm(name))
        if pin is None:
            errors.append(f'constraints-ci.txt: no exact pin for {name}')
        elif SpecifierSet is not None and pin not in SpecifierSet(rest.strip()):
            errors.append(f'constraints-ci.txt: {name}=={pin} violates pyproject range {rest.strip()}')
    if args.installed:
        for name, pin in sorted(pins.items()):
            try:
                have = metadata.version(name)
            except metadata.PackageNotFoundError:
                errors.append(f'{name} is not installed')
                continue
            if have != pin:
                errors.append(f'{name}: installed {have} != pinned {pin}')
    if errors:
        print('\n'.join(errors))
        return 1
    print(f'dependency pins consistent ({len(pins)} direct pins; transitive dependencies not locked)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
