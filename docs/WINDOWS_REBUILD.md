# Rebuilding the Windows application

The application source and build scripts are available in the DeckLab repository.
Windows distributions retain library licenses, a dependency/version inventory,
file hashes and Paramiko's corresponding source archive. These sources and
scripts allow a recipient to replace that library and rebuild the application.
This guide describes the build mechanism; it is not a legal clearance statement.

## Requirements and checkout

Use Windows x64, Python 3.12, Git and Inno Setup 6.5 or later. Node.js runs the
frontend regression checks. Work in a source checkout, separate from the running
installation and its `data/` directory:

```powershell
git clone https://github.com/qianqiu-2020/decklab.git
cd decklab
```

For a released package, check out its corresponding source tag before building.
The replacement-source option was added after the initial 1.5.0 draft; that older
tag does not contain it. The default branch contains the current build mechanism.

## Replace Paramiko and build

Extract `third-party/sources/paramiko-source.tar.gz` from the installed package
into a separate working directory. Modify its Python source and retain/add the
appropriate license and change notices. From that extracted source directory,
produce a new source distribution:

```powershell
py -3.12 -m pip install build
py -3.12 -m build --sdist
```

Back in the DeckLab checkout, supply that `.tar.gz` archive explicitly:

```powershell
.\Build-seriald.cmd -ParamikoSource "D:\modified-paramiko\dist\paramiko-4.0.0.tar.gz"
```

The builder creates a fresh isolated interpreter, installs normal dependencies,
then installs this source archive with `--force-reinstall --no-deps` before tests
and freezing. The exact supplied archive and its hash are included in the new
bundle; it is marked as a local replacement rather than unmodified upstream
source. A changed version is recorded from installed distribution metadata.
Retain the API expected by the SSH adapter or adjust the application source.
New dependency requirements need a corresponding requirements update.

## Outputs and verification

`dist/` contains the Setup EXE, portable ZIP and checksum files. The installer
checks the complete file manifest and license/source hashes before compilation.
Run the new host from a separate test installation; do not connect another
collector to ports owned by a running production instance. A successful test
build does not automatically upgrade or restart the production application.

The normal build requires reviewed frozen native-provider hashes. Changing
cryptography, bcrypt or CFFI requires updating their attribution catalog too;
Paramiko replacement alone does not change those native providers.
