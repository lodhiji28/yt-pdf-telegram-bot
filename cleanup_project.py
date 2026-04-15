import os

# List of files to keep (main project files)
keep_files = {
    'main.py',
    'run.py',
    'requirements.txt',
    'pyproject.toml',
    'users.json',
    'users.xlsx',
    'README.md',
    'data.json',
    'extract_and_merge_users.py',
    'cookies.txt',
}

# Remove unnecessary files in root
for fname in os.listdir('.'):
    if os.path.isfile(fname) and fname not in keep_files:
        print(f"Deleting {fname}")
        os.remove(fname)

# Remove unnecessary scripts
for fname in ['print_numbered_users.py', 'users.xlsx.csv', 'users.xlsx.csv.py', 'users_to_excel.py', 'uv.lock']:
    if os.path.exists(fname):
        print(f"Deleting {fname}")
        os.remove(fname)

# Remove __pycache__ and attached_assets folders
import shutil
for folder in ['__pycache__', 'attached_assets']:
    if os.path.exists(folder):
        print(f"Deleting folder {folder}")
        shutil.rmtree(folder)

print("Cleanup complete.")
