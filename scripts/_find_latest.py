"""Print the latest recording subdirectory path (POSIX slashes) or 'output'."""
import glob, pathlib
dirs = sorted(glob.glob("output/*/*/"), reverse=True)
if dirs:
    print(pathlib.Path(dirs[0]).as_posix())
else:
    print("output")
