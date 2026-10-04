"""Import a user-selected key file without exposing its contents to output."""
import importlib.util
import os
from pathlib import Path
import re
import sys


def main():
    try:
        path = Path(sys.argv[1])
        raw = path.read_bytes()
        if len(raw) > 65536:
            raise ValueError()
        content = None
        for encoding in ('utf-8-sig', 'utf-16', 'gb18030'):
            try:
                content = raw.decode(encoding)
                break
            except UnicodeError:
                continue
        if content is None:
            raise ValueError()
        keys = set(re.findall(r'(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{16,256}(?![A-Za-z0-9_-])', content))
        if len(keys) != 1:
            print('IMPORT_BLOCKED: expected exactly one recognizable API key; file contents were not printed.')
            return 2
        spec = importlib.util.spec_from_file_location('secret_setup', Path(__file__).with_name('secret-input.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        target = Path(os.environ['LOCALAPPDATA']) / 'MaiYeBang/deepseek.key.dpapi'
        module.save_secret(target, keys.pop())
        print('IMPORTED: API key stored with Windows account encryption. No provider request made.')
        return 0
    except Exception:
        print('IMPORT_FAILED: contents suppressed; original file retained.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
