"""Project-configured dependency adapter, with immutable inputs and warm reuse."""
import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET

HERE = Path(__file__).resolve().parent


def run(args, **kw):
    return subprocess.run([str(a) for a in args], check=True, **kw)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def fingerprint(path):
    """Content-based key, not an existence-only or timestamp-only stamp."""
    path = Path(path)
    files = [path] if path.is_file() else sorted(p for p in path.rglob('*') if p.is_file())
    h = hashlib.sha256()
    for f in files:
        h.update(str(f.relative_to(path) if f != path else f.name).encode())
        h.update(digest(f).encode())
    return h.hexdigest()


def write_if_changed(path, data):
    path = Path(path)
    data = data.encode() if isinstance(data, str) else data
    if path.is_file() and path.read_bytes() == data:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_bytes(data)
    tmp.replace(path)


def discover(root, ruby=None, cache_roots=()):
    root = Path(root).resolve()
    entries = []
    for rel, purpose in [('Podfile', 'dependency generation'), ('Podfile.lock', 'locked versions'),
                         ('Pods/Manifest.lock', 'installed versions'), ('Gemfile', 'Ruby tools'),
                         ('Package.resolved', 'Swift package versions'),
                         ('Pods/Local Podspecs', 'source and vendor declarations'),
                         ('Pods/Pods.xcodeproj', 'actual source and cache build phases')]:
        p = root / rel
        if p.exists():
            entries.append({'path': str(p), 'resolved': str(p.resolve()), 'purpose': purpose})
    scripts = []
    for name in ['script', 'scripts', 'build']:
        folder = root / name
        if folder.is_dir():
            scripts.extend(str(p) for p in folder.rglob('*') if p.is_file() and p.suffix in {'.rb', '.py', '.sh'})
    rows = []
    for location in cache_roots:
        cache = root / location
        if not cache.is_dir():
            raise ValueError(f'confirmed cache directory missing: {cache}')
        for xc in sorted(cache.glob('*.xcframework')):
            info = plistlib.loads((xc / 'Info.plist').read_bytes())
            rows.append({'name': xc.stem, 'path': str(xc), 'slices': info.get('AvailableLibraries', [])})
    phases = None
    if ruby and (root / 'Pods/Pods.xcodeproj').exists():
        proc = run([ruby, HERE / 'cocoapods_project.rb', 'inspect', root], capture_output=True, text=True)
        phases = json.loads(proc.stdout)
    return {'root': str(root), 'workspaces': [str(p) for p in root.glob('*.xcworkspace')],
            'materials': entries, 'build_scripts': scripts, 'project_build_phases': phases, 'cache': rows,
            'lock_sha256': digest(root / 'Podfile.lock') if (root / 'Podfile.lock').is_file() else None,
            'next_step': 'Trace generation scripts, cache-copy phases and link inputs; configure only confirmed material paths'}


def config(path):
    cfg = json.loads(Path(path).read_text())
    root = (Path(path).resolve().parent / cfg['root']).resolve()
    if digest(root / 'Podfile.lock') != cfg['lock_sha256']:
        raise ValueError('Podfile.lock changed: recheck source fallbacks and cache selection before proceeding')
    if (root / 'Pods/Manifest.lock').read_bytes() != (root / 'Podfile.lock').read_bytes():
        raise ValueError('installed and locked dependency versions differ')
    cfg['root'] = str(root)
    for key in ('vendor_config', 'workspace'):
        if cfg.get(key):
            cfg[key] = str((root / cfg[key]).resolve())
    cfg['tool_inputs'] = [str((root / p).resolve()) for p in cfg.get('tool_inputs', [])]
    if cfg.get('conversion_command') and not cfg['tool_inputs']:
        raise ValueError('Declare conversion tool inputs so tool changes invalidate cached outputs')
    cfg.setdefault('ruby', shutil.which('ruby'))
    cfg.setdefault('deployment_target', '14.0')
    cfg.setdefault('source_fallbacks', [])
    if 'cache_roots' not in cfg:
        raise ValueError('Locate actual cache inputs from build scripts and configure cache_roots, use [] when absent')
    if cfg['source_fallbacks'] and not cfg.get('cache_phase_names'):
        raise ValueError('Identify the phases replacing source compilation and configure cache_phase_names')
    for entry in cfg['cache_roots']:
        relative = Path(entry['overlay_path'])
        if relative.is_absolute() or '..' in relative.parts or not relative.parts:
            raise ValueError('cache overlay_path must stay within the generated Pods directory')
        if not (root / entry['path']).is_dir():
            raise ValueError('configured cache input missing: ' + entry['path'])
    cfg.setdefault('vendor_output', 'BinaryDependencies/arm64')
    for relative in [cfg['vendor_output'], *cfg.get('generated_pods_paths', [])]:
        if Path(relative).is_absolute() or '..' in Path(relative).parts or not Path(relative).parts:
            raise ValueError('generated paths must stay within the generated Pods directory')
    return cfg


def framework_binary(path):
    path = Path(path)
    if not path.is_dir():
        return path
    metadata = next((p for p in [path / 'Info.plist', path / 'Resources/Info.plist'] if p.is_file()), None)
    info = plistlib.loads(metadata.read_bytes()) if metadata else {}
    candidates = [path / name for name in dict.fromkeys([info.get('CFBundleExecutable', path.stem), path.stem])]
    binary = next((p for p in candidates if p.is_file()), None)
    if binary is None:
        raise ValueError(f'framework executable missing: {path}')
    return binary


def conversion_output_check(source, output):
    original, changed = framework_binary(source), framework_binary(output)
    archs = run(['xcrun', 'lipo', '-archs', changed], capture_output=True, text=True).stdout.split()
    if archs != ['arm64']:
        raise ValueError('conversion output must contain only arm64')
    def defined_symbols(binary):
        data = run(['xcrun', 'nm', '-arch', 'arm64', '-j', '-g', '-U', binary], capture_output=True, text=True).stdout
        return sorted(s.strip() for s in data.splitlines() if s.strip() and not s.strip().endswith(':'))
    before, after = defined_symbols(original), defined_symbols(changed)
    if before != after:
        raise ValueError('conversion changed externally defined symbols')
    commands = run(['xcrun', 'otool', '-arch', 'arm64', '-l', changed], capture_output=True, text=True).stdout
    platforms = re.findall(r'^\s*platform\s+(\S+)\s*$', commands, re.M)
    if not platforms or any(p not in {'7', 'IOSSIMULATOR'} for p in platforms) or 'LC_VERSION_MIN_IPHONEOS' in commands:
        raise ValueError('conversion output lacks simulator-only platform declarations')
    if Path(output).is_dir():
        paths = re.findall(r'^\s*path (.+) \(offset \d+\)\s*$', commands, re.M)
        if len(paths) != len(set(paths)):
            raise ValueError('duplicate runtime library search paths remain')
        with changed.open('rb') as stream:
            archive = stream.read(8) == b'!<arch>\n'
        if not archive:
            run(['codesign', '--verify', '--strict', changed], capture_output=True, text=True)
    return {'platform': 'IOSSIMULATOR', 'defined_symbols': len(after)}


def conversion_command(cfg, source, output):
    command = cfg.get('conversion_command')
    if not isinstance(command, list) or not command or not all(isinstance(p, str) for p in command):
        raise ValueError('Locate or create a project-local conversion tool and configure its argument list')
    values = {'root': cfg['root'], 'source': str(source), 'output': str(output),
              'deployment_target': cfg['deployment_target']}
    rendered = []
    for arg in command:
        for name, value in values.items():
            arg = arg.replace('{' + name + '}', value)
        rendered.append(arg)
    return rendered


def convert_task(task, cfg, state):
    name, source, output = task['name'], Path(task['source']), Path(task['output'])
    stamp = output.with_name(output.name + '.conversion.json')
    key = hashlib.sha256((fingerprint(source) + state + str(task.get('native', False))).encode()).hexdigest()
    if stamp.exists() and output.exists():
        old = json.loads(stamp.read_text())
        if old.get('input_key') == key and old.get('output_sha256') == fingerprint(output):
            return {'name': name, 'status': 'reused'}
    # Build in a sibling temporary directory so interrupted conversion cannot
    # promote a partially written output or a success stamp
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.convert-', dir=output.parent) as tmp:
        candidate = Path(tmp) / output.name
        if task.get('native'):
            run(['/bin/cp', '-cR', source, candidate])
            binary = candidate / candidate.stem if candidate.is_dir() else candidate
            proc = run(['xcrun', 'otool', '-arch', 'arm64', '-l', binary], capture_output=True, text=True)
            if not re.search(r'^\s*platform\s+(?:7|IOSSIMULATOR)\s*$', proc.stdout, re.M):
                raise ValueError(f'{name}: declared native slice lacks arm64 simulator platform evidence')
            report = {'platform': 'native-simulator-input'}
        else:
            try:
                run(conversion_command(cfg, source, candidate), capture_output=True, text=True)
            except subprocess.CalledProcessError as error:
                raise ValueError(f'{name}: conversion failed\n{error.stderr}') from error
            report = conversion_output_check(source, candidate)
        if candidate.is_dir() and not task.get('native'):
            for module in candidate.glob('Modules/*.swiftmodule'):
                interfaces = list(module.glob('*.swiftinterface'))
                if not interfaces:
                    raise ValueError(f'{name}: missing Swift text interfaces, select local source or native simulator library')
                for interface in module.glob('arm64-apple-ios*.swiftinterface'):
                    if 'simulator' in interface.name:
                        continue
                    dest = interface.with_name(interface.name.replace('arm64-apple-ios', 'arm64-apple-ios-simulator', 1))
                    text = re.sub(r'-target arm64-apple-ios[0-9.]+(?:-simulator)?',
                                  '-target arm64-apple-ios' + cfg['deployment_target'] + '-simulator', interface.read_text())
                    write_if_changed(dest, text)
        output_hash = fingerprint(candidate)
        # A tool update may revalidate an identical result, preserve timestamps
        # so unchanged binary inputs do not invalidate an incremental build
        if not output.exists() or fingerprint(output) != output_hash:
            if output.is_dir():
                shutil.rmtree(output)
            elif output.exists():
                output.unlink()
            candidate.replace(output)
    write_if_changed(stamp, json.dumps({'input_key': key, 'output_sha256': output_hash, **report}, indent=2))
    return {'name': name, 'status': 'native' if task.get('native') else 'converted', **report}


def prepare(cfg, config_path):
    started = time.monotonic()
    root = Path(cfg['root'])
    work = root / '.arm64-simulator'
    overlay = work / 'Pods'
    work.mkdir(parents=True, exist_ok=True)
    overlay.mkdir(exist_ok=True)
    context = digest(__file__) + ''.join(digest(p) for p in cfg['tool_inputs']) + json.dumps(cfg.get('conversion_command', [])) + cfg['deployment_target']
    tasks = []
    fallback = set(cfg['source_fallbacks'])
    for cache in cfg['cache_roots']:
        for xc in sorted((root / cache['path']).glob('*.xcframework')):
            if xc.stem in fallback:
                continue
            info = plistlib.loads((xc / 'Info.plist').read_bytes())
            libs = info['AvailableLibraries']
            matching = [x for x in libs if x['SupportedPlatform'] == 'ios' and 'arm64' in x['SupportedArchitectures']]
            native = next((x for x in matching if x.get('SupportedPlatformVariant') == 'simulator'), None)
            device = next((x for x in matching if not x.get('SupportedPlatformVariant')), None)
            selected = native or device
            if not selected:
                raise ValueError(f'{xc.stem}: no arm64 input, locate source or supplier simulator library')
            target = overlay / cache['overlay_path'] / xc.name
            lib = selected['LibraryPath']
            tasks.append({'name': xc.stem, 'source': str(xc / selected['LibraryIdentifier'] / lib),
                          'output': str(target / 'ios-arm64-simulator' / lib), 'native': bool(native)})
            # This overlay is simulator-only, leaving all device entries untouched
            entry = dict(selected, LibraryIdentifier='ios-arm64-simulator', SupportedPlatformVariant='simulator', SupportedArchitectures=['arm64'])
            write_if_changed(target / 'Info.plist', plistlib.dumps(dict(info, AvailableLibraries=[entry])))

    vendor_rows = []
    if cfg.get('vendor_config'):
        proc = run([cfg['ruby'], HERE / 'cocoapods_project.rb', 'vendors', root,
                    overlay, cfg['vendor_config'], json.dumps(cfg.get('generated_pods_paths', []) +
                    [str(Path(c['path']).relative_to('Pods')) for c in cfg['cache_roots'] if Path(c['path']).parts[0] == 'Pods'])], capture_output=True, text=True)
        vendor_rows = json.loads(proc.stdout)
        allowed_missing = set(cfg.get('known_missing_vendors', []))
        for row in vendor_rows:
            if not row['source']:
                if row['name'] not in allowed_missing:
                    raise ValueError(f"vendor missing: {row['name']}")
                continue
            suffix = 'frameworks/' + row['name'] + '.framework' if row['kind'] == 'framework' else 'lib/lib' + row['name'] + '.a'
            tasks.append({'name': 'vendor:' + row['name'], 'source': row['source'],
                          'output': str(overlay / cfg['vendor_output'] / suffix)})
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=cfg.get('jobs', 6)) as pool:
        futures = [pool.submit(convert_task, task, cfg, context) for task in tasks]
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())

    # Copy only generated build configuration, sources continue at original paths
    base_key = hashlib.sha256((fingerprint(root / 'Pods/Pods.xcodeproj') +
                              fingerprint(root / 'Pods/Target Support Files') +
                              digest(HERE / 'cocoapods_project.rb') + json.dumps(cfg, sort_keys=True)).encode()).hexdigest()
    base_stamp = work / 'project-state.json'
    refreshed = not base_stamp.exists() or json.loads(base_stamp.read_text()).get('input_key') != base_key
    if refreshed:
        for name in ['Pods.xcodeproj', 'Target Support Files']:
            dest = overlay / name
            if dest.exists():
                shutil.rmtree(dest)
            run(['/bin/cp', '-cR', root / 'Pods' / name, dest])
        generated_roots = {Path(c['overlay_path']).parts[0] for c in cfg['cache_roots']}
        generated_roots.update(Path(p).parts[0] for p in cfg.get('generated_pods_paths', []))
        if cfg.get('vendor_config'):
            generated_roots.add(Path(cfg['vendor_output']).parts[0])
        for source in (root / 'Pods').iterdir():
            dest = overlay / source.name
            if source.name not in {'Pods.xcodeproj', 'Target Support Files'} | generated_roots and not dest.exists():
                dest.symlink_to(source, target_is_directory=source.is_dir())
        resolved_cfg = work / 'resolved-config.json'
        write_if_changed(resolved_cfg, json.dumps(cfg, indent=2))
        proc = run([cfg['ruby'], HERE / 'cocoapods_project.rb', 'project', root,
                    overlay, resolved_cfg], capture_output=True, text=True)
        write_if_changed(work / 'source-restoration.json', proc.stdout)
        write_if_changed(base_stamp, json.dumps({'input_key': base_key}))

    original = Path(cfg['workspace']) / 'contents.xcworkspacedata'
    tree = ET.parse(original)
    for ref in tree.findall('.//FileRef'):
        loc = ref.get('location', '')
        prefix, value = loc.split(':', 1)
        path = (root / value).resolve() if prefix == 'group' else Path(value)
        if path == (root / 'Pods/Pods.xcodeproj').resolve():
            path = overlay / 'Pods.xcodeproj'
        ref.set('location', 'absolute:' + str(path))
    workspace = work / 'Development.xcworkspace'
    write_if_changed(workspace / 'contents.xcworkspacedata', ET.tostring(tree.getroot(), encoding='utf-8', xml_declaration=True))
    # Preserve workspace settings and shared schemes without rewriting unchanged files
    for f in Path(cfg['workspace']).rglob('*'):
        if f.is_file() and f.name != 'contents.xcworkspacedata' and 'xcuserdata' not in f.parts:
            write_if_changed(workspace / f.relative_to(cfg['workspace']), f.read_bytes())
    summary = {'seconds': round(time.monotonic() - started, 2), 'project_refreshed': refreshed,
               'source_fallbacks': len(fallback), 'tasks': len(results),
               'statuses': {s: sum(r['status'] == s for r in results) for s in sorted({r['status'] for r in results})},
               'workspace': str(workspace), 'known_missing_vendors': [r['name'] for r in vendor_rows if not r['source']]}
    write_if_changed(work / 'last-prepare.json', json.dumps(summary, indent=2))
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return workspace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    inspection = sub.add_parser('discover')
    inspection.add_argument('--root', required=True)
    inspection.add_argument('--ruby')
    inspection.add_argument('--cache-root', action='append', default=[])
    for action in ('prepare', 'build'):
        p = sub.add_parser(action)
        p.add_argument('--config', required=True)
        if action == 'build':
            p.add_argument('--device', required=True)
            p.add_argument('--skip-swiftlint', action='store_true')
    args = parser.parse_args()
    if args.action == 'discover':
        print(json.dumps(discover(args.root, args.ruby, args.cache_root), ensure_ascii=False, indent=2))
        return
    cfg = config(args.config)
    workspace = prepare(cfg, args.config)
    if args.action == 'build':
        work = Path(cfg['root']) / '.arm64-simulator'
        logs = work / 'logs'
        logs.mkdir(exist_ok=True)
        log = logs / ('build-' + time.strftime('%Y%m%d-%H%M%S') + '.log')
        command = ['xcodebuild', '-workspace', str(workspace), '-scheme', cfg['scheme'],
                   '-configuration', 'Debug', '-sdk', 'iphonesimulator', '-destination',
                   'platform=iOS Simulator,id=' + args.device, '-derivedDataPath', str(work / 'DerivedData'),
                   '-disableAutomaticPackageResolution', '-skipPackageUpdates', '-jobs', str(cfg.get('jobs', 8)),
                   'ARCHS=arm64', 'ONLY_ACTIVE_ARCH=YES', 'CODE_SIGNING_ALLOWED=NO',
                   'PODS_ROOT=' + str(work / 'Pods'), 'IPHONEOS_DEPLOYMENT_TARGET=' + cfg['deployment_target'],
                   'SIMULATOR_SKIP_SWIFTLINT=' + ('1' if args.skip_swiftlint else '0'), 'build']
        start = time.monotonic()
        print(json.dumps({'build_log': str(log), 'command': command}), flush=True)
        with log.open('wb') as stream:
            proc = subprocess.run(command, cwd=cfg['root'], stdout=stream, stderr=subprocess.STDOUT)
        report = {'seconds': round(time.monotonic() - start, 2), 'exit_code': proc.returncode,
                  'swiftlint': 'NOT RUN' if args.skip_swiftlint else 'not skipped', 'log': str(log)}
        write_if_changed(log.with_suffix('.json'), json.dumps(report, indent=2))
        print(json.dumps(report), flush=True)
        raise SystemExit(proc.returncode)


if __name__ == '__main__':
    main()
