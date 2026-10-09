"""Ad-hoc sign a new simulator App copy, preserving the compiled input."""
import argparse
import json
from pathlib import Path
import plistlib
import subprocess

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--source', required=True)
p.add_argument('--output', required=True)
a = p.parse_args()
source, output = Path(a.source).resolve(), Path(a.output).resolve()
if output.exists() or source == output or source in output.parents:
    raise SystemExit('Choose a new output outside the source App')
info = plistlib.loads((source / 'Info.plist').read_bytes())
if 'iPhoneSimulator' not in info.get('CFBundleSupportedPlatforms', []):
    raise SystemExit('Input must be a simulator build, not a device App')
output.parent.mkdir(parents=True, exist_ok=True)
subprocess.run(['/bin/cp', '-cR', str(source), str(output)], check=True)
for path in output.rglob('*'):
    if path.is_symlink() and not path.resolve().is_relative_to(output):
        raise SystemExit('App contains a symlink outside the package: ' + str(path))
for path in output.rglob('Info.plist'):
    try:
        info = plistlib.loads(path.read_bytes())
    except (ValueError, plistlib.InvalidFileException):
        continue
    platforms = info.get('CFBundleSupportedPlatforms', [])
    if 'iPhoneOS' in platforms:
        info['CFBundleSupportedPlatforms'] = ['iPhoneSimulator' if x == 'iPhoneOS' else x for x in platforms]
        path.write_bytes(plistlib.dumps(info, fmt=plistlib.FMT_BINARY))
entitlements = output.parent / (output.name + '.entitlements.plist')
entitlements.write_bytes(plistlib.dumps({}))
binaries = []
for path in output.rglob('*'):
    if path.is_file():
        with path.open('rb') as f:
            magic = f.read(4)
        if magic in (b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf', b'\xca\xfe\xba\xbe', b'\xca\xfe\xba\xbf'):
            binaries.append(path)
for path in binaries:
    subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', str(path)], check=True, capture_output=True)
bundles = sorted([x for x in output.rglob('*') if x.is_dir() and x.suffix in ('.framework', '.appex', '.xpc', '.app')],
                 key=lambda x: len(x.parts), reverse=True)
for bundle in bundles + [output]:
    subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', '--entitlements', str(entitlements), str(bundle)],
                   check=True, capture_output=True)
for path in binaries + [output]:
    subprocess.run(['/usr/bin/codesign', '--verify', '--strict', str(path)], check=True, capture_output=True)
print(json.dumps({'source': str(source), 'output': str(output), 'signed_mach_o': len(binaries),
                  'status': 'signed-not-runtime-validated'}))
