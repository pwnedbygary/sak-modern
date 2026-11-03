# SAK Modern for Android

Native Android front-end for the SAK Modern core. It is **not** a Tkinter
repackage: Tkinter is unavailable on Android, so this app uses a native Java UI
and runs the same Python container/crypto code through Chaquopy.

## Features

- Import a merged XCI/NSP through Android's document picker.
- See base / update / DLC as a proportional partition map.
- Tap a partition or checkbox to select it.
- Extract selected packages or all packages.
- Bundle a base + update/DLC into XCI or NSP.
- Verify container structure and hashes.
- Load `prod.keys` / `keys.txt`.
- Finished files are copied to `Downloads/SAK-Output`.

Inputs are copied into the app's external files directory before Python reads
them, because the core needs random-access file paths. Large merges therefore
need roughly twice the source size in free space.

## Requirements

- Android 7.0+ (minSdk 24), arm64-v8a or x86_64.
- `prod.keys` for your own content.
- No keys or game content are bundled.

## Build

Prerequisites: JDK 17, Android SDK 35, Gradle 8.13, and a Python 3.12
interpreter for Chaquopy.

```bash
export ANDROID_HOME=/path/to/Android/Sdk
export BUILD_PYTHON=/path/to/python3.12
cd android
./gradlew assembleRelease
```

`app/build.gradle` uses `BUILD_PYTHON` when set, otherwise
`/tmp/py312/python/bin/python3`.

Sign the unsigned release APK:

```bash
keytool -genkeypair -v -keystore sak-release.jks \
  -storepass changeit -keypass changeit -alias sakmodern \
  -keyalg RSA -keysize 2048 -validity 10000 \
  -dname "CN=SAK Modern, O=SAK Modern, C=US"

$ANDROID_HOME/build-tools/35.0.0/apksigner sign \
  --ks sak-release.jks --ks-key-alias sakmodern \
  --ks-pass pass:changeit --key-pass pass:changeit \
  --out SAK-Modern.apk app/build/outputs/apk/release/app-release-unsigned.apk
```

The APK attached to the GitHub release is signed with a local
development key. Keep the keystore if you want to publish updates that install
over it.

## Tested build

- Device: Android 13, arm64-v8a (1080x1920).
- Merged Transiruby XCI: base/update/DLC map rendered, all three packages
  extracted, copied to `Downloads/SAK-Output`, pulled back to Linux, and verified.
- Signed APK: `SAK-Modern.apk` (GitHub release asset)
  SHA-256 `02424fe6384962e75059c50689b645fa922c0a8624a1bdcbabff41d84d336b23`.
