#!/usr/bin/env python3
"""Extract only the two approved public Clerk settings; never print environment contents."""
from pathlib import Path
import argparse, base64, re, shlex
from urllib.parse import urlsplit
root = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser(description=__doc__)
parser.parse_args()
wanted = {'CLERK_PUBLISHABLE_KEY', 'CLERK_ISSUER'}
values = {}
with (root/'.env').open() as stream:
    for line in stream:
        key, separator, value = line.partition('=')
        key = key.strip().removeprefix('export ').strip()
        if not separator or key not in wanted:
            continue
        if key in values:
            raise SystemExit('Duplicate public Clerk setting')
        parts = shlex.split(value, comments=True)
        if len(parts) != 1:
            raise SystemExit('Malformed public Clerk setting')
        values[key] = parts[0]
if set(values) != wanted:
    raise SystemExit('Both public Clerk settings are required')
key, issuer = values['CLERK_PUBLISHABLE_KEY'], values['CLERK_ISSUER']
if not re.fullmatch(r'pk_(test|live)_[A-Za-z0-9+/=_-]+', key):
    raise SystemExit('Invalid publishable key format')
url = urlsplit(issuer)
if url.scheme != 'https' or not url.hostname or url.path or url.query or url.fragment or url.username or url.password:
    raise SystemExit('Issuer must be the exact configured HTTPS origin')
encoded = base64.b64encode(issuer.encode()).decode()
config = root/'fe/Config/Local.xcconfig'
config.parent.mkdir(parents=True, exist_ok=True)
config.write_text('// Generated public configuration. Ignored; no secret settings are read or copied.\n'
                  + 'CLERK_PUBLISHABLE_KEY = '+key+'\nCLERK_ISSUER_BASE64 = '+encoded+'\n')
config.chmod(0o600)
print('Generated ignored public Clerk configuration. API address is separately set in APIConfiguration.baseURL.')
