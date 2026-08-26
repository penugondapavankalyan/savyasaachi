## Step 1 — Delete old package and zip
rmdir /s /q package
del kirana-agent.zip


## Step 2 — Create fresh package folder
mkdir package


## Step 3 — Build dependencies targeting Lambda's Linux x86_64 platform
pip install --platform mlinux_2_17_x86_64 --target package/ --implementation cp --python-version 3.12 --only-binary=:all: --upgrade -r requirements.txt


## Step 4 — Copy source code into package
xcopy /E /I /Y src package\src


## Step 5 — Zip everything
python -c "import shutil; shutil.make_archive('savyasaachi-v2', 'zip', 'package')"


## Step 6 — Check zip size
dir kirana-agent.zip

