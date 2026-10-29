import re
from glob import glob

from setuptools import setup

APP_NAME = "Ember"
APP = ["ember.py"]
VERSION = re.search(r'^VERSION = "(.+?)"', open("ember.py").read(), re.M).group(1)
DATA_FILES = [
    ("assets", ["assets/AppIcon.png"]),
    ("assets/icons", glob("assets/icons/*.png")),
]
OPTIONS = {
    "argv_emulation": False,
    "iconfile": "assets/AppIcon.icns",
    "packages": ["pyhiveapi", "apyhiveapi", "rumps", "keyring", "boto3", "botocore", "PIL"],
    "includes": ["dial", "_math_integer"],  # new in Python 3.15; py2app misses it
    "plist": {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": "uk.co.madestudio.ember",
        "LSUIElement": True,  # menu bar only, no Dock icon
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
    },
}

setup(
    name=APP_NAME,
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
