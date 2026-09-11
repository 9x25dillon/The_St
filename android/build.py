#!/usr/bin/env python3
"""Build a development APK with the installed Android SDK and JDK; no Gradle downloads."""
from pathlib import Path
import argparse
import os
import shutil
import subprocess
import tempfile
import zipfile

root = Path(__file__).resolve().parent
parser = argparse.ArgumentParser(description='Build The Saint Android development APK')
parser.add_argument('--sdk', default=os.environ.get('ANDROID_HOME', str(Path.home() / 'Android/Sdk')))
parser.add_argument('--install', action='store_true', help='install on the single authorized ADB device')
args = parser.parse_args()
sdk = Path(args.sdk)
android_jar = sdk / 'platforms/android-36/android.jar'
tools = sdk / 'build-tools/35.0.0'
out = root / 'build'
out.mkdir(exist_ok=True)

def run(*command):
    subprocess.run([str(part) for part in command], check=True)

try:
    if shutil.which('node'):
        run('node', '--check', root.parent/'web/app.js')
    if not android_jar.exists() or not tools.exists():
        raise ValueError('Install Android SDK platform 36 and Build Tools 35.0.0, or pass --sdk PATH.')
    with tempfile.TemporaryDirectory(prefix='compile-', dir=out) as temporary:
        work = Path(temporary)
        classes, dex = work/'classes', work/'dex'
        classes.mkdir(); dex.mkdir()
        resources = work/'resources.zip'
        unsigned = work/'unsigned.apk'
        run(tools/'aapt2', 'compile', '--dir', root/'res', '-o', resources)
        run(tools/'aapt2', 'link', '-o', unsigned, '-I', android_jar, '--manifest', root/'AndroidManifest.xml',
            '-A', root.parent/'web', '--auto-add-overlay', resources)
        run('javac', '-source', '8', '-target', '8', '-encoding', 'UTF-8', '-classpath', android_jar,
            '-d', classes, *sorted((root/'src').rglob('*.java')))
        run(tools/'d8', '--lib', android_jar, '--min-api', '26', '--output', dex, *sorted(classes.rglob('*.class')))
        with zipfile.ZipFile(unsigned, 'a', compression=zipfile.ZIP_DEFLATED) as archive:
            for file in dex.glob('*.dex'): archive.write(file, file.name)
        aligned = work/'aligned.apk'
        run(tools/'zipalign', '-f', '-p', '4', unsigned, aligned)
        key = out/'development.keystore'
        if not key.exists():
            run('keytool', '-genkeypair', '-keystore', key, '-storepass', 'android', '-keypass', 'android',
                '-alias', 'androiddebugkey', '-keyalg', 'RSA', '-keysize', '2048', '-validity', '10000',
                '-dname', 'CN=The Saint Development,O=Local,C=US')
            key.chmod(0o600)
        apk = out/'the-saint-debug.apk'
        run(tools/'apksigner', 'sign', '--ks', key, '--ks-pass', 'pass:android', '--key-pass', 'pass:android', '--out', apk, aligned)
        run(tools/'apksigner', 'verify', '--verbose', apk)
        print(f'Built {apk} ({apk.stat().st_size:,} bytes)')
        if args.install:
            run('adb', 'install', '-r', apk)
            run('adb', 'shell', 'am', 'start', '-n', 'local.thesaint.app/.MainActivity')
except (ValueError, OSError, subprocess.CalledProcessError) as error:
    parser.exit(1, f'Android build failed: {error}\n')
