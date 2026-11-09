"""Reproducible recipe for the portable, unsigned Windows x64 bridge."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from symport.bridge import VERSION

out = ROOT / 'build/windows'
out.mkdir(parents=True, exist_ok=True)
version = tuple(map(int, VERSION.split('.'))) + (0,)
resource = out / 'version.txt'
resource.write_text(f'''VSVersionInfo(ffi=FixedFileInfo(filevers={version!r}, prodvers={version!r}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0,0)), kids=[StringFileInfo([StringTable('040904B0', [StringStruct('CompanyName', 'SymPort contributors'), StringStruct('FileDescription', 'SymPort Nokia S60 Bridge'), StringStruct('FileVersion', '{VERSION}'), StringStruct('ProductName', 'SymPort Bridge'), StringStruct('ProductVersion', '{VERSION}')])]), VarFileInfo([VarStruct('Translation', [1033,1200])])])''', encoding='utf-8')
subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile', '--windowed',
    '--name', 'SymPortBridge', '--distpath', str(out / 'portable'), '--workpath', str(out / 'work'),
    '--specpath', str(out), '--paths', str(ROOT), '--version-file', str(resource),
    '--icon', str(ROOT / 'symport/web_assets/brand/appmark-192.png'),
    '--add-data', str(ROOT / 'symport/web_assets/brand/appmark-192.png') + ';symport/web_assets/brand',
    '--add-data', str(ROOT / 'symport/phone/firmware_reference.json') + ';symport/phone',
    '--add-data', str(ROOT / 'symport/phone/device_models.json') + ';symport/phone',
    '--collect-all', 'gammu', '--hidden-import', 'pystray._win32', '--hidden-import', 'serial.tools.list_ports_windows',
    '--exclude-module', 'cryptography', '--exclude-module', 'symport.web',
    str(ROOT / 'tools/windows/bridge_entry.py')], check=True, cwd=ROOT)
subprocess.run([str(out / 'portable/SymPortBridge.exe'), '--self-test', '--report', str(out / 'self-test.json')], check=True, timeout=120)
report = json.loads((out / 'self-test.json').read_text())
assert report['gammu'].get('gammu'), report
assert report['credential_protection'] == 'passed', report
assert report['ca_pinning'] == 'passed' and report['doctor'] == 'passed', report  # no `cryptography` in the exe
assert all(report['components'][n] for n in ('pyserial', 'Pillow', 'pystray', 'Tk')), report
print(json.dumps(report))
print('Frozen bridge worker, DPAPI and authenticated HTTP checks passed')
