[app]
title = Wallbox
package.name = wallbox
package.domain = de.marodeur100
source.dir = .
source.include_exts = py
version = 1.1
requirements = python3,kivy==2.3.0
orientation = portrait
android.permissions = android.permission.INTERNET
android.api = 34
android.minapi = 26
android.ndk = 25b
android.archs = arm64-v8a, armeabi-v7a
android.allow_backup = False
android.theme = "@android:style/Theme.NoTitleBar"
android.accept_sdk_license = True

[buildozer]
log_level = 2
warn_on_root = 1
