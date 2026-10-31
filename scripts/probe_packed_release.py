"""Exercise a packed plugin in an isolated OpenCode server with a local fake provider."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--pause-node', action='store_true')
    parser.add_argument('--resume-after-pause', action='store_true')
    parser.add_argument('--specialist', action='store_true')
    parser.add_argument('--stop-during-specialist', action='store_true')
    parser.add_argument('--lost-report-response', action='store_true')
    parser.add_argument('--lost-kickoff-response', action='store_true')
    parser.add_argument('--cold-restart-node', action='store_true')
    parser.add_argument('--cold-restart-specialist', action='store_true')
    parser.add_argument('--warm-reload-specialist', action='store_true')
    args = parser.parse_args()
    if args.resume_after_pause and not args.pause_node:
        parser.error('--resume-after-pause requires --pause-node')
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='ari-packed-probe-') as temporary:
        temp = Path(temporary)
        build = temp / 'build'
        plugin = build / 'plugin'
        plugin.mkdir(parents=True)
        shutil.copytree(root / 'src', build / 'src', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        for name in ('index.ts', 'tui.tsx', 'rpc.ts', 'src', 'scripts', 'package.json', 'README.md'):
            source = root / 'plugin' / name
            target = plugin / name
            if source.is_dir():
                shutil.copytree(source, target, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            else:
                shutil.copy2(source, target)
        archive = temp / 'release.tgz'
        subprocess.run(['bun', 'pm', 'pack', '--filename', str(archive), '--quiet'], cwd=plugin,
                       check=True, capture_output=True, text=True, timeout=30)
        release = temp / 'release'
        release.mkdir()
        subprocess.run(['tar', '-xzf', str(archive), '-C', str(release)], check=True, timeout=10)
        shutil.rmtree(build)
        package = release / 'package'
        if args.lost_report_response or args.lost_kickoff_response:
            # Instrument only the temporary extracted artifact: accept the host
            # message, then simulate losing its response before ledger confirm.
            entry = package / 'index.ts'
            text = entry.read_text()
            needle = '        const sent = await ctx.session.synthetic({ sessionID, text, description: label, resume, ...(outboundID ? { id: outboundID } : {}) })'
            injection = "\n        if (process.env.ARI_PROBE_LOSE_REPORT && label.includes('收到节点消息') && !(globalThis as any).__ariLostReport) { (globalThis as any).__ariLostReport = true; throw new Error('injected lost report response') }" + "\n        if (process.env.ARI_PROBE_LOSE_KICKOFF && label.includes('开始节点') && !(globalThis as any).__ariLostKickoff) { (globalThis as any).__ariLostKickoff = true; throw new Error('injected lost kickoff response') }"
            if text.count(needle) != 1:
                raise RuntimeError('Cannot instrument the extracted test artifact')
            entry.write_text(text.replace(needle, needle + injection))
        # Dependencies are the locally installed pinned SDK, not repository source;
        # the packed Python module must resolve inside the extracted archive.
        (package / 'node_modules').symlink_to((root / 'plugin' / 'node_modules').resolve(), target_is_directory=True)
        env = dict(os.environ, PYTHONPATH=str(package / 'python'), PYTHONDONTWRITEBYTECODE='1')
        origin = subprocess.check_output([sys.executable, '-S', '-c',
            'import auto_research.service; print(auto_research.service.__file__)'], cwd=release, env=env, text=True).strip()
        if Path(origin).resolve() != (package / 'python' / 'auto_research' / 'service.py').resolve():
            raise RuntimeError('Packed backend did not load from the extracted artifact')
        command = [sys.executable, str(root / 'scripts' / 'probe_research_run.py'), '--plugin', str(package)]
        if args.pause_node:
            command.append('--pause-node')
        if args.resume_after_pause:
            command.append('--resume-after-pause')
        if args.specialist:
            command.append('--specialist')
        if args.stop_during_specialist:
            command.append('--stop-during-specialist')
        if args.lost_report_response:
            command.append('--lost-report-response')
        if args.lost_kickoff_response:
            command.append('--lost-kickoff-response')
        if args.cold_restart_node:
            command.append('--cold-restart-node')
        if args.cold_restart_specialist:
            command.append('--cold-restart-specialist')
        if args.warm_reload_specialist:
            command.append('--warm-reload-specialist')
        result = subprocess.run(command, capture_output=True, text=True, timeout=45)
        if result.returncode:
            sys.stderr.write(result.stdout + result.stderr)
            raise SystemExit(result.returncode)
        print(json.dumps({'backend_from_extracted_package': True, 'build_tree_deleted': True,
                          'temporary_fault_injection': args.lost_report_response or args.lost_kickoff_response,
                          'dependencies': 'local pinned SDK', 'host_result': json.loads(result.stdout)}))


if __name__ == '__main__':
    main()
