"""Bootstrap checks use a fixture archive; never install packages or services."""
import io
import os
from pathlib import Path
import subprocess
import tarfile
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'install.sh'

@pytest.mark.skipif(os.geteuid() != 0, reason='Installer requires root')
@pytest.mark.parametrize('case', ['complete', 'incomplete', 'traversal', 'download_error'])
def test_piped_bootstrap(tmp_path, case):
    archive = tmp_path / 'fixture.tar.gz'
    files = {'SSHBot-main/deploy/manager.py': 'import sys\nprint("MANAGER_OK", sys.argv)\n',
             'SSHBot-main/web/app.py': '', 'SSHBot-main/requirements.txt': ''}
    if case == 'incomplete':
        del files['SSHBot-main/web/app.py']
    if case == 'traversal':
        files['SSHBot-main/../../escaped'] = 'bad'
    with tarfile.open(archive, 'w:gz') as tar:
        for name, content in files.items():
            data = content.encode()
            entry = tarfile.TarInfo(name); entry.size = len(data)
            tar.addfile(entry, io.BytesIO(data))
    bindir = tmp_path / 'bin'; bindir.mkdir()
    curl = bindir / 'curl'
    curl.write_text('#!/bin/bash\n' + ('exit 22\n' if case == 'download_error' else
        'while [[ $# -gt 0 ]]; do if [[ $1 == --output ]]; then cp "$FIXTURE" "$2"; exit; fi; shift; done\nexit 1\n'))
    curl.chmod(0o755)
    env = {**os.environ, 'PATH': str(bindir) + ':' + os.environ['PATH'], 'FIXTURE': str(archive)}
    result = subprocess.run(['bash', '-s', '--', '--status'], input=SCRIPT.read_text(), text=True,
                            capture_output=True, env=env, cwd=tmp_path)
    if case == 'complete':
        assert result.returncode == 0, result.stderr
        assert 'MANAGER_OK' in result.stdout
        source = result.stdout.split('--source')[1].split("'")[2]
        assert not Path(source).exists(), 'Temporary source must be cleaned'
    else:
        assert result.returncode != 0
        assert 'MANAGER_OK' not in result.stdout
    assert not (tmp_path / 'escaped').exists()

@pytest.mark.skipif(os.geteuid() != 0, reason='Installer requires root')
def test_local_source_without_download(tmp_path):
    (tmp_path / 'deploy').mkdir(); (tmp_path / 'web').mkdir()
    (tmp_path / 'deploy/manager.py').write_text('print("LOCAL_OK")')
    (tmp_path / 'web/app.py').touch(); (tmp_path / 'requirements.txt').touch()
    script = tmp_path / 'install.sh'; script.write_text(SCRIPT.read_text())
    result = subprocess.run(['bash', str(script), '--status'], capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stdout.strip() == 'LOCAL_OK'
