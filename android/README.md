# The Saint for Android

An offline Android development app for personal Markdown/plain-text notes and
TikTok JSON exports. It packages the shared web interface inside a native Android
host. The Java adapter parses selected text and holds the session in memory.

## Build and install

Requirements: JDK 17, Python 3, Android SDK Platform 36, Build Tools 35.0.0.
No Gradle, Python runtime on the phone, or third-party Android libraries are needed.

```sh
python android/build.py
```

Output: `android/build/the-saint-debug.apk`.

Connect the phone, enable USB debugging in Developer options, unlock it, and accept
the computer's debugging fingerprint prompt. Then:

```sh
adb devices -l
adb install -r android/build/the-saint-debug.apk
adb shell am start -n local.thesaint.app/.MainActivity
```

Or rebuild and install in one command: `python android/build.py --install`.
Pass `--sdk /path/to/sdk` if needed. The default is `$ANDROID_HOME` or `~/Android/Sdk`.
Use `adb -s SERIAL ...` when more than one device is connected.

The app supports Android 8.0/API 26 and newer, targets API 36, and contains no native
CPU-specific libraries. It requests **no permissions**, including no INTERNET or
broad storage permission. Android's document picker grants access to selected files.
Pick locally stored files for an entirely offline import; a cloud document provider
may use its own network connection to retrieve a file you select.

## What works on the phone

- Personal Markdown and text notes, with passage and heading provenance.
- TikTok expressed text, assigned interest labels, and watch-timestamp counts.
- A sample journal, recurring word counts, passage search, and incremental display.
- Clear session. A failed import preserves the existing session.
- Responsive layout that respects system bars and the keyboard.

Browser database imports and the Python semantic model remain desktop features.
They are not silently approximated on Android. The phone app has no background
collection, analytics, server connection, or persistent personal-data store.
Sessions can survive opening the file picker, but Android can discard them when
it terminates the app process. A force-stop always clears the in-memory session.

## Development signing

The builder generates `android/build/development.keystore` once and reuses it so
reinstalls can update the app. Keep that file locally; it is ignored by Git.
This is a debug-signed development build with WebView debugging enabled, not a
Play Store release. A release needs a private release key, debugging disabled,
and the corresponding distribution configuration.

The packaged interface loads only bundled content through a fixed HTTPS-style
asset origin; it does not fetch that hostname. External navigation is blocked,
and personal text is rendered as text rather than HTML.

Reference: [Android local-content guidance](https://developer.android.com/develop/ui/views/layout/webapps/load-local-content)
and [AAPT2 command-line packaging](https://developer.android.com/tools/aapt2).

## On-device checks

Validated on the connected Pixel 10a: installation and launch, the native bridge,
sample/search flows, note and TikTok parsing, failed-import recovery, text escaping,
session clearing, and system-bar spacing. A synthetic text file was also imported
through the real Android document picker and removed afterward.

To repeat the automated part, launch the app, obtain its PID with
`adb shell pidof local.thesaint.app`, forward
`adb forward tcp:9224 localabstract:webview_devtools_remote_PID`, then run
`node tests/android_smoke.mjs`. The test uses synthetic data and clears the current
session. Close the forward afterward with `adb forward --remove tcp:9224`.
