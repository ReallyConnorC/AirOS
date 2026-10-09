"""Build a signed, self-contained APK with installed official Android tools; no Gradle dependencies."""
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SDK = Path(os.environ.get('ANDROID_HOME', Path.home() / 'AppData/Local/Android/Sdk'))
JDK = Path(os.environ.get('AIR_JDK', Path.home() / 'devtools/jdk-21.0.12.1+1'))
TOOLS = SDK / 'build-tools/35.0.0'
ANDROID = SDK / 'platforms/android-35/android.jar'
PRIVATE = Path.home() / '.config/airos/android-signing'
PRIVATE.mkdir(parents=True, exist_ok=True)
CONFIG = PRIVATE / 'signing.json'
if CONFIG.exists():
    password = json.loads(CONFIG.read_text())['password']
else:
    password = secrets.token_urlsafe(32)
    CONFIG.write_text(json.dumps({'password': password}))
    CONFIG.chmod(0o600)
ENV = dict(os.environ, JAVA_HOME=str(JDK), AIR_KEY_PASSWORD=password)
ENV['PATH'] = str(JDK / 'bin') + os.pathsep + ENV.get('PATH', '')

def run(*args):
    command = list(map(str, args))
    if command[0].endswith('.bat'):
        command = ['cmd.exe', '/d', '/c', *command]
    subprocess.run(command, check=True, env=ENV)

keystore = PRIVATE / 'air-kitchen.jks'
if not keystore.exists():
    run(JDK/'bin/keytool.exe', '-genkeypair', '-keystore', keystore, '-storepass:env', 'AIR_KEY_PASSWORD',
        '-keypass:env', 'AIR_KEY_PASSWORD', '-alias', 'air-kitchen', '-keyalg', 'RSA', '-keysize', '3072',
        '-validity', '10000', '-dname', 'CN=Air Kitchen')
with tempfile.TemporaryDirectory(prefix='air-apk-') as folder:
    build = Path(folder)
    for name in ('assets', 'gen', 'classes', 'dex'):
        (build/name).mkdir()
    for name in ('index.html', 'app.js', 'api.js', 'config.js', 'style.css', 'icon.svg', 'profile.js', 'app.webmanifest', 'app-icon.png'):
        shutil.copy2(ROOT/'docs/family'/name, build/'assets'/name)
    run(TOOLS/'aapt.exe', 'package', '-f', '-M', ROOT/'kitchen-android/AndroidManifest.xml',
        '-S', ROOT/'kitchen-android/res', '-A', build/'assets', '-I', ANDROID,
        '-F', build/'base.apk', '-J', build/'gen')
    source = [*ROOT.joinpath('kitchen-android/src').rglob('*.java'), *build.joinpath('gen').rglob('*.java')]
    run(JDK/'bin/javac.exe', '--release', '8', '-classpath', ANDROID, '-d', build/'classes', *source)
    with zipfile.ZipFile(build/'classes.jar', 'w') as jar:
        for path in (build/'classes').rglob('*.class'):
            jar.write(path, path.relative_to(build/'classes').as_posix())
    run(TOOLS/'d8.bat', '--min-api', '24', '--lib', ANDROID, '--output', build/'dex', build/'classes.jar')
    with zipfile.ZipFile(build/'base.apk', 'a', zipfile.ZIP_DEFLATED) as apk:
        apk.write(build/'dex/classes.dex', 'classes.dex')
    run(TOOLS/'zipalign.exe', '-f', '4', build/'base.apk', build/'aligned.apk')
    output = ROOT/'docs/family/downloads/Air-Kitchen.apk'
    output.parent.mkdir(parents=True, exist_ok=True)
    run(TOOLS/'apksigner.bat', 'sign', '--ks', keystore, '--ks-key-alias', 'air-kitchen',
        '--ks-pass', 'env:AIR_KEY_PASSWORD', '--key-pass', 'env:AIR_KEY_PASSWORD', '--out', output, build/'aligned.apk')
    run(TOOLS/'apksigner.bat', 'verify', '--verbose', output)
    print('Created:', output)
